#!/usr/bin/env python3
"""vSphere side of exo-v2v: VDDK-free CBT replication (snapshot + QueryChangedDiskAreas + hot-add).

Reads changed blocks with nothing but the vSphere API (works against vCenter or a
standalone ESXi host):

  1. CreateSnapshot (no memory, no quiesce by default) — the base -flat.vmdk of
     every disk is now frozen at a crash-consistent point in time.
  2. QueryChangedDiskAreas(snapshot, disk, changeId) — CBT extents since the
     previous cycle ("*" = all allocated areas, for the first/full cycle).
  3. Read exactly those extents from the frozen base disk:
     - transport "hotadd" (default; the sender runs as a VM on the same vSphere,
       a "replication appliance"): the base disk is attached to the sender VM as
       independent_nonpersistent (writes go to a throw-away redo log, the source
       is never modified), read with pread at local SCSI speed, then detached.
     - transport "http": Range GETs of -flat.vmdk via the datastore file service.
       hostd needs an exclusive lock for that, so it only works for powered-off
       VMs (a running VM's snapshot base is locked: "Failed to lock file").
  4. NBD writes to the receiver (zero runs as WRITE_ZEROES), then flush.
  5. POST /meta {change_id, ...} to the receiver per disk, RemoveSnapshot.

The changeId lives on the receiver (/meta, RAM): if the receiver restarts, the
copy is invalid anyway and the next cycle must be a full one (--full).

  vsync.py inventory   --vm NAME
  vsync.py enable-cbt  --vm NAME
  vsync.py sync        --vm NAME --map DISK=EXPORT[,...] [--full] [--final]
  vsync.py verify      --vm NAME --map DISK=EXPORT[,...]

DISK = scsiN:M / sataN:M / nvmeN:M / ideN:M (as in `inventory`), EXPORT = sda/sdb...
Connection: --vc HOST (or $VSYNC_HOST) --user ($VSYNC_USER) --password-env VAR
(default VSYNC_PASSWORD), --nbd 127.0.0.1:10809, --api http://127.0.0.1:8787.
--final powers the VM off (guest shutdown via Tools, hard power-off after
--shutdown-timeout) before the last cycle: that cycle's data is the cutover state.
hotadd needs: --proxy-vm (default: this VM, found by BIOS UUID), root (block
device reads), a paravirtual/LSI SCSI controller with free units on the proxy.
Crash safety: leftover v2v-sync-* snapshots on the source and hot-added
nonpersistent disks on the proxy are cleaned up at the start of every run.
"""
import concurrent.futures as cf
import contextlib
import fcntl
import glob
import hashlib
import http.client
import json
import os
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import zlib

from .receiver import nbdproto as P

try:
    from pyVim.connect import Disconnect, SmartConnect
    from pyVim.task import WaitForTask
    from pyVmomi import vim, vmodl
except ImportError:  # pragma: no cover
    sys.exit("exo-v2v needs pyvmomi >= 8: pip install pyvmomi")

CHUNK = 8 << 20          # NBD write unit
MAX_RANGE = 64 << 20     # largest single HTTP Range request
SNAP_PREFIX = "v2v-sync"
KEEPALIVE_S = 600         # vSphere sessions expire after 30 min idle


from .util import log


# ---------------- vSphere ----------------

class VC:
    """A vCenter or standalone ESXi session. `host` may carry a port (vcenter:8443)."""

    def __init__(self, host, user, password, insecure=True):
        ctx = ssl._create_unverified_context() if insecure else None
        self.host, _, port = host.partition(":")
        self.port = int(port or 443)
        self.si = SmartConnect(host=self.host, port=self.port, user=user, pwd=password, sslContext=ctx)
        self.content = self.si.RetrieveContent()
        self.ssl = ctx or ssl.create_default_context()
        self.user, self.password, self.insecure = user, password, insecure
        # vCenter and ESXi drop a session idle for 30 minutes; a hot-add copy of a large disk
        # makes no API call for hours, and the detach after it would fail.
        self._stop = threading.Event()
        threading.Thread(target=self._keepalive, daemon=True).start()

    def _keepalive(self, every=KEEPALIVE_S):
        while not self._stop.wait(every):
            try:
                self.si.CurrentTime()
            except Exception as e:                # the next real call reports it
                log(f"vSphere keepalive: {e}")

    def close(self):
        self._stop.set()
        Disconnect(self.si)

    def about(self):
        a = self.content.about
        return {"product": a.fullName, "api_type": a.apiType, "version": a.apiVersion,
                "vcenter": a.apiType == "VirtualCenter"}

    def vms(self, props=("name", "config.template"), root=None):
        """[(vm, {prop: value})] for every VM under `root` (default: the whole
        inventory), in one PropertyCollector round trip per page - a vCenter
        with thousands of VMs answers in seconds, not one call per VM."""
        view = self.content.viewManager.CreateContainerView(root or self.content.rootFolder, [vim.VirtualMachine], True)
        pc = vmodl.query.PropertyCollector
        try:
            spec = pc.FilterSpec(
                objectSet=[pc.ObjectSpec(obj=view, skip=True, selectSet=[
                    pc.TraversalSpec(name="view", path="view", skip=False, type=vim.view.ContainerView)])],
                propSet=[pc.PropertySpec(type=vim.VirtualMachine, pathSet=list(props))])
            out, res = [], self.content.propertyCollector.RetrievePropertiesEx([spec], pc.RetrieveOptions())
            while res:
                out += [(o.obj, {x.name: x.val for x in (o.propSet or [])}) for o in res.objects]
                res = self.content.propertyCollector.ContinueRetrievePropertiesEx(res.token) if res.token else None
            return out
        finally:
            view.Destroy()

    def path(self, obj):
        """Inventory path, e.g. DC1/vm/shop/web01 (what FindByInventoryPath takes)."""
        parts = []
        while obj is not None and obj != self.content.rootFolder:
            parts.append(obj.name)
            obj = obj.parent
        return "/".join(reversed(parts))

    def vm(self, ref):
        """A VM by name, by inventory path (DC/vm/folder/name) or by instance
        UUID (uuid:...). Templates never match; a name that several VMs share
        (several datacenters or folders) is refused with their paths."""
        if ref.startswith("uuid:"):
            v = self.content.searchIndex.FindByUuid(uuid=ref[5:], vmSearch=True, instanceUuid=True)
            if v is None:
                sys.exit(f"no VM with instance UUID {ref[5:]}")
            return v
        if "/" in ref:
            v = self.content.searchIndex.FindByInventoryPath(ref.strip("/"))
            if not isinstance(v, vim.VirtualMachine):
                sys.exit(f"{ref!r}: no VM at that inventory path (e.g. Datacenter/vm/folder/name)")
            return v
        hits = [v for v, pr in self.vms() if pr.get("name") == ref and not pr.get("config.template")]
        if not hits:
            sys.exit(f"VM {ref!r} not found")
        if len(hits) > 1:
            sys.exit(f"VM {ref!r} is ambiguous ({len(hits)} VMs have that name): use its inventory path, "
                     + " or ".join(repr(self.path(v)) for v in hits))
        return hits[0]

    def vm_of(self, plan):
        """The VM a plan was made for: by its instance UUID (survives renames and
        folder moves), else by the name or path given to `plan`."""
        uuid = (plan.get("inventory") or {}).get("instance_uuid")
        if uuid:
            v = self.content.searchIndex.FindByUuid(uuid=uuid, vmSearch=True, instanceUuid=True)
            if v is not None:
                return v
        return self.vm(plan["vm"])

    def by_moid(self, moid):
        return vim.VirtualMachine(moid, self.si._stub)

    def folder_vms(self, path):
        """Every VM (not templates) in an inventory folder and its subfolders."""
        f = self.content.searchIndex.FindByInventoryPath(path.strip("/"))
        if not isinstance(f, (vim.Folder, vim.Datacenter, vim.ResourcePool, vim.ComputeResource)):
            sys.exit(f"{path!r}: no folder at that inventory path (e.g. Datacenter/vm/shop)")
        return [v for v, pr in self.vms(root=f) if not pr.get("config.template")]

    def cookie(self):
        # pyvmomi keeps 'vmware_soap_session="..."; Path=/; ...' on the stub
        raw = self.si._stub.cookie
        return raw.split(";")[0] if raw else ""

    def datacenter_of(self, obj):
        while obj and not isinstance(obj, vim.Datacenter):
            obj = obj.parent
        return obj


def disk_id(dev, controllers):
    c = controllers[dev.controllerKey]
    kind = ("scsi" if isinstance(c, vim.vm.device.VirtualSCSIController) else
            "sata" if isinstance(c, vim.vm.device.VirtualSATAController) else
            "nvme" if isinstance(c, vim.vm.device.VirtualNVMEController) else
            "ide" if isinstance(c, vim.vm.device.VirtualIDEController) else "ctl")
    return f"{kind}{c.busNumber}:{dev.unitNumber}"


def disks_of(hardware):
    devs = hardware.device
    ctl = {d.key: d for d in devs if isinstance(d, vim.vm.device.VirtualController)}
    out = {}
    for d in devs:
        if isinstance(d, vim.vm.device.VirtualDisk):
            out[disk_id(d, ctl)] = d
    return out


def flat_path(backing_file):
    """'[ds] dir/name.vmdk' -> ('ds', 'dir/name-flat.vmdk')."""
    ds, path = backing_file[1:].split("] ", 1)
    if not path.endswith(".vmdk"):
        raise ValueError(f"unexpected backing {backing_file}")
    return ds, path[:-5] + "-flat.vmdk"


CTL_ORDER = ("scsi", "sata", "nvme", "ide")   # last-resort guess of the boot disk


def guest_mounts(vm, disks):
    """Guest filesystems -> the virtual disks behind them (VMware Tools disk
    mappings; empty when Tools do not run or are too old). A filesystem Tools
    list without a mapping keeps an empty list (Windows: a dynamic volume)."""
    by_key = {d.key: k for k, d in disks.items()}
    out = []
    for g in getattr(vm.guest, "disk", None) or []:
        ids = [by_key[m.key] for m in (getattr(g, "mappings", None) or []) if m.key in by_key]
        out.append({"path": g.diskPath, "disks": ids})
    return out if any(m["disks"] for m in out) else []


def boot_disk(cfg, disks, mounts):
    """(disk id, how it was found): BIOS/EFI boot order, then the disk holding the
    guest's boot files, then bios.hddOrder, then controller order (a guess)."""
    by_key = {d.key: k for k, d in disks.items()}
    for b in getattr(cfg.bootOptions, "bootOrder", None) or []:
        if isinstance(b, vim.vm.BootOptions.BootableDiskDevice) and b.deviceKey in by_key:
            return by_key[b.deviceKey], "boot order"
    norm = lambda p: p.rstrip("\\").lower()     # "C:\" -> "c:"
    for want in ("/boot/efi", "/boot", "/", "C:\\"):
        hit = next((m["disks"] for m in mounts if norm(m["path"]) == norm(want)), None)
        if hit and len(hit) == 1:
            return hit[0], f"guest {want}"
    ex = {o.key.lower(): str(o.value) for o in cfg.extraConfig or []}
    for d in (x.strip() for x in ex.get("bios.hddorder", "").split(",")):
        if d in disks:
            return d, "bios.hddOrder"
    key = lambda k: (CTL_ORDER.index(k.rstrip("0123456789:")) if k.rstrip("0123456789:") in CTL_ORDER
                     else len(CTL_ORDER), [int(x) for x in k.lstrip("abcdefghijklmnopqrstuvwxyz").split(":")])
    return (min(disks, key=key), "controller order") if disks else (None, "")


def inventory(vc, vm):
    cfg = vm.config
    disks = disks_of(cfg.hardware)
    mounts = guest_mounts(vm, disks)
    boot, boot_how = boot_disk(cfg, disks, mounts)
    return {
        "boot_disk": boot, "boot_disk_source": boot_how, "guest_mounts": mounts,
        "name": vm.name, "power": str(vm.runtime.powerState), "guest_id": cfg.guestId,
        "instance_uuid": cfg.instanceUuid, "path": vc.path(vm), "server": vc.about(),
        "host": vm.runtime.host.name if vm.runtime.host else None,
        "datastores": sorted(ds.name for ds in vm.datastore),
        "guest_full": cfg.guestFullName, "firmware": cfg.firmware,
        "secure_boot": bool(getattr(cfg.bootOptions, "efiSecureBootEnabled", False)),
        "vtpm": any(type(d).__name__.endswith("VirtualTPM") for d in cfg.hardware.device),
        "cpus": cfg.hardware.numCPU, "memory_mb": cfg.hardware.memoryMB,
        "cbt_enabled": bool(cfg.changeTrackingEnabled),
        "cbt_disks": disk_ctk(vm),
        "snapshots": [s.name for s in _walk_snaps(vm.snapshot.rootSnapshotList)] if vm.snapshot else [],
        "tools": str(vm.guest.toolsRunningStatus),
        "nics": nics_of(vm),
        "disks": [{"id": k, "label": d.deviceInfo.label, "capacity": d.capacityInBytes,
                   "file": d.backing.fileName, "thin": bool(getattr(d.backing, "thinProvisioned", False)),
                   "change_id": getattr(d.backing, "changeId", None),
                   "mode": getattr(d.backing, "diskMode", "")} for k, d in sorted(disks.items())],
    }


# What exo-v2v does with the account (docs/prerequisites.md), checked by `plan`
PRIVILEGES = {
    "source VM": ["VirtualMachine.State.CreateSnapshot", "VirtualMachine.State.RemoveSnapshot",
                  "VirtualMachine.Config.ChangeTracking", "VirtualMachine.Interact.PowerOff",
                  "VirtualMachine.Interact.PowerOn", "VirtualMachine.Provisioning.DiskRandomRead"],
    "appliance VM": ["VirtualMachine.Config.AddExistingDisk", "VirtualMachine.Config.RemoveDisk"],
    "datastore": ["Datastore.FileManagement"],
}


def missing_privileges(vc, vm, proxy=None):
    """["PRIV on KIND NAME"] the session lacks (HasPrivilegeOnEntities: it follows
    roles granted on parent folders, clusters and datacenters). None when the
    server cannot tell."""
    targets = [("source VM", vm)] + ([("appliance VM", proxy)] if proxy is not None else []) + \
        [("datastore", ds) for ds in vm.datastore]
    am = vc.content.authorizationManager
    try:
        key = vc.content.sessionManager.currentSession.key
        out = []
        for kind, ent in targets:
            for r in am.HasPrivilegeOnEntities(entity=[ent], sessionId=key, privId=PRIVILEGES[kind]):
                out += [f"{x.privId} on {kind} {ent.name}" for x in (r.privAvailability or []) if not x.isGranted]
        return out
    except (vmodl.MethodFault, AttributeError, TypeError):
        return None


def appliance_checks(vc, vm, proxy):
    """(errors, warnings) about the appliance and a source VM: hot-add needs the
    appliance's host to mount every datastore of the VM; DRS must not move the
    appliance (vMotion) while snapshot disks are attached to it."""
    errors, warnings = [], []
    host = proxy.runtime.host
    seen = set(host.datastore) if host else set()
    for ds in vm.datastore:
        if host and ds not in seen:
            errors.append(f"the appliance runs on host {host.name}, which does not mount datastore {ds.name} "
                          f"of {vm.name}: hot-add needs it (run the appliance on a host that sees it, or "
                          "use --transport http, slower)")
    cluster = host.parent if host and isinstance(host.parent, vim.ClusterComputeResource) else None
    if cluster is not None:
        drs = cluster.configurationEx.drsConfig
        if drs and drs.enabled:
            behavior, on = str(drs.defaultVmBehavior), True
            for c in cluster.configurationEx.drsVmConfig or []:
                if c.key == proxy:
                    behavior, on = str(c.behavior or behavior), c.enabled is not False
            if on and behavior == "fullyAutomated":
                warnings.append(f"DRS of cluster {cluster.name} may move the appliance {proxy.name} while "
                                "snapshot disks are attached to it: add a VM override for it (automation "
                                "level: Disabled, or Partially automated)")
    return errors, warnings


def nics_of(vm):
    """The VM's NICs in device order: port group, and (VMware Tools) the guest's
    IPv4 addresses with their origin (dhcp/manual) and the default gateway."""
    cfg, guest = vm.config, vm.guest
    pg_by_key = {getattr(n, "key", None): n.name for n in vm.network or []}
    gnet = list(getattr(guest, "net", None) or [])
    gw = {}
    for st in getattr(guest, "ipStack", None) or []:
        for r in getattr(getattr(st, "ipRouteConfig", None), "ipRoute", None) or []:
            if r.network == "0.0.0.0" and r.prefixLength == 0 and r.gateway and r.gateway.ipAddress:
                try:
                    gw[gnet[int(r.gateway.device)].macAddress.lower()] = r.gateway.ipAddress
                except (ValueError, IndexError, AttributeError):
                    pass
    dns = [a for st in getattr(guest, "ipStack", None) or []
           for a in getattr(getattr(st, "dnsConfig", None), "ipAddress", None) or []]
    out = []
    for d in cfg.hardware.device:
        if not isinstance(d, vim.vm.device.VirtualEthernetCard):
            continue
        b = d.backing
        pg = getattr(b, "deviceName", None)
        if pg is None and getattr(b, "port", None) is not None:          # distributed port group
            pg = pg_by_key.get(b.port.portgroupKey, b.port.portgroupKey)
        mac = (d.macAddress or "").lower()
        g = next((n for n in gnet if (n.macAddress or "").lower() == mac), None)
        ips = []
        for a in (getattr(getattr(g, "ipConfig", None), "ipAddress", None) or []) if g else []:
            if ":" not in a.ipAddress and not a.ipAddress.startswith("169.254."):
                ips.append({"ip": a.ipAddress, "prefix": a.prefixLength, "origin": str(a.origin or "")})
        out.append({"label": d.deviceInfo.label, "type": type(d).__name__.replace("vim.vm.device.", ""),
                    "key": d.key, "mac": mac, "port_group": pg,
                    "connected": bool(getattr(d.connectable, "startConnected", True)),
                    "ips": ips, "gateway": gw.get(mac), "dns": dns})
    return out


def power_on(vm, tools_timeout=600):
    """Power the VM on and wait for VMware Tools (the guest is up), or the timeout."""
    if vm.runtime.powerState != vim.VirtualMachinePowerState.poweredOn:
        task(vm.PowerOnVM_Task)
    t0 = time.time()
    while time.time() - t0 < tools_timeout:
        if str(vm.guest.toolsRunningStatus) == "guestToolsRunning":
            return round(time.time() - t0)
        time.sleep(5)
    return None


def _walk_snaps(lst):
    for s in lst or []:
        yield s
        yield from _walk_snaps(s.childSnapshotList)


def disk_ctk(vm):
    """Per-disk CBT flags from the VMX (scsiX:Y.ctkEnabled)."""
    ex = {o.key.lower(): str(o.value).lower() for o in vm.config.extraConfig or []}
    return {d: ex.get(f"{d}.ctkenabled") == "true" for d in disks_of(vm.config.hardware)}


def enable_cbt(vc, vm):
    """VM-level changeTrackingEnabled + <disk>.ctkEnabled for every disk (a disk
    without its own flag reports 'Change tracking is not active'). On a running
    VM a snapshot create/remove (stun/unstun) activates it - no downtime."""
    extra = [vim.option.OptionValue(key=f"{d}.ctkEnabled", value="TRUE") for d in disks_of(vm.config.hardware)]
    spec = vim.vm.ConfigSpec(changeTrackingEnabled=True, extraConfig=extra)
    wait_idle(vm)
    task(vm.ReconfigVM_Task, spec)
    if vm.runtime.powerState == vim.VirtualMachinePowerState.poweredOn:
        wait_idle(vm)
        task(vm.CreateSnapshot_Task, f"{SNAP_PREFIX}-cbt-activate", "", False, False)
        snap = [s for s in _walk_snaps(vm.snapshot.rootSnapshotList) if s.name == f"{SNAP_PREFIX}-cbt-activate"][0]
        task(snap.snapshot.RemoveSnapshot_Task, False)
    return {"cbt_enabled": bool(vm.config.changeTrackingEnabled), "disks": disk_ctk(vm)}


class CbtInactive(Exception):
    pass


def changed_areas(vm, snap, disk, change_id):
    """All CBT extents of a disk (paged API) as [(start, length)]."""
    out, off, cap = [], 0, disk.capacityInBytes
    while off < cap:
        try:
            r = vm.QueryChangedDiskAreas(snapshot=snap, deviceKey=disk.key, startOffset=off, changeId=change_id)
        except vim.fault.FileFault as e:
            # hostd: "Change tracking is not active on disk ..." (per-disk ctk off)
            raise CbtInactive(f"{disk.backing.fileName}: change tracking not active") from e
        out += [(a.start, a.length) for a in r.changedArea]
        nxt = r.startOffset + r.length
        if nxt <= off:
            break
        off = nxt
    return out


def coalesce(extents, gap=1 << 20, cap=MAX_RANGE):
    """Merge near-adjacent extents (fewer HTTP requests) and split to <= cap."""
    merged = []
    for s, n in sorted(extents):
        if merged and s <= merged[-1][0] + merged[-1][1] + gap and \
                (s + n) - merged[-1][0] <= cap:
            ms, mn = merged[-1]
            merged[-1] = (ms, max(mn, s + n - ms))
        else:
            merged.append((s, n))
    out = []
    for s, n in merged:
        while n > cap:
            out.append((s, cap)); s += cap; n -= cap
        out.append((s, n))
    return out


class FlatReader:
    """Range reads of a (frozen) -flat.vmdk through the datastore file service."""

    def __init__(self, vc, vm, backing_file):
        ds, path = flat_path(backing_file)
        dc = vc.datacenter_of(vm)
        q = urllib.parse.urlencode({"dcPath": dc.name, "dsName": ds})
        self.path = f"/folder/{urllib.parse.quote(path)}?{q}"
        self.host, self.port, self.cookie, self.ssl = vc.host, vc.port, vc.cookie(), vc.ssl
        self.conn = None

    def read(self, start, length, tries=4):
        for attempt in range(tries):
            try:
                if self.conn is None:
                    self.conn = http.client.HTTPSConnection(self.host, self.port, context=self.ssl, timeout=120)
                self.conn.request("GET", self.path, headers={"Cookie": self.cookie,
                                                             "Range": f"bytes={start}-{start + length - 1}"})
                r = self.conn.getresponse()
                body = r.read()
                if r.status not in (200, 206):
                    raise IOError(f"HTTP {r.status} for {self.path}: {body[:200]!r}")
                if r.status == 200:  # server ignored Range: slice (small files only)
                    body = body[start:start + length]
                if len(body) != length:
                    raise IOError(f"short read {len(body)}/{length} at {start}")
                return body
            except (OSError, http.client.HTTPException) as e:
                self.conn = None
                if attempt == tries - 1:
                    raise
                log(f"  read retry {attempt + 1} at {start}: {e}")
                time.sleep(2 * (attempt + 1))


class DevReader:
    """pread from a local block device (thread-safe; one fd shared)."""

    def __init__(self, dev):
        self.dev = dev
        self.fd = os.open(dev, os.O_RDONLY)

    def clone(self):
        return self

    def read(self, start, length):
        buf = os.pread(self.fd, length, start)
        if len(buf) != length:
            raise IOError(f"short read {len(buf)}/{length} at {start} on {self.dev}")
        return buf

    def close(self):
        os.close(self.fd)


def self_vm(vc):
    """The VM this process runs in, by SMBIOS UUID (needs root)."""
    try:
        uuid = open("/sys/class/dmi/id/product_uuid").read().strip().lower()
    except OSError:
        return None
    hits = vc.content.searchIndex.FindAllByUuid(uuid=uuid, vmSearch=True, instanceUuid=False)
    return hits[0] if len(hits) == 1 else None


SKIP_CTL = (vim.vm.device.VirtualIDEController, vim.vm.device.VirtualPCIController,
            vim.vm.device.VirtualSIOController, vim.vm.device.VirtualPS2Controller)


class HotAdd:
    """Attach a frozen base disk to the proxy VM (read-only use), find its Linux
    block device, detach afterwards. One instance per disk."""

    def __init__(self, proxy, backing_file, capacity):
        self.proxy, self.file, self.capacity = proxy, backing_file, capacity
        self.device = None
        self.dev = None

    def _free_slot(self):
        devs = self.proxy.config.hardware.device
        used = {(d.controllerKey, d.unitNumber) for d in devs if getattr(d, "unitNumber", None) is not None}
        for c in devs:
            if isinstance(c, vim.vm.device.VirtualSCSIController):
                for u in range(16):
                    if u != 7 and (c.key, u) not in used:  # 7 = the controller itself
                        return c, u
        raise RuntimeError(f"proxy VM {self.proxy.name}: no free SCSI unit (add a paravirtual SCSI controller)")

    LOCK = "/run/lock/exo-v2v-hotadd.lock"

    @contextlib.contextmanager
    def _appliance_lock(self):
        # one appliance reconfiguration at a time: concurrent migrations on this
        # host would otherwise collide in vSphere (TaskInProgress) and interleave
        # slot choice + device discovery
        if getattr(self, "_locked", False):     # re-entered (detach after a failed attach)
            yield
            return
        os.makedirs(os.path.dirname(self.LOCK), exist_ok=True)
        with open(self.LOCK, "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            self._locked = True
            try:
                yield
            finally:
                self._locked = False
                fcntl.flock(lk, fcntl.LOCK_UN)

    def __enter__(self):
        with self._appliance_lock():
            return self._attach()

    def _attach(self):
        ctl, unit = self._free_slot()
        before = set(os.listdir("/sys/block"))
        disk = vim.vm.device.VirtualDisk(
            key=-1, controllerKey=ctl.key, unitNumber=unit, capacityInBytes=self.capacity,
            backing=vim.vm.device.VirtualDisk.FlatVer2BackingInfo(
                fileName=self.file, diskMode="independent_nonpersistent"))
        task(self.proxy.ReconfigVM_Task, vim.vm.ConfigSpec(
            deviceChange=[vim.vm.device.VirtualDeviceSpec(operation="add", device=disk)]))
        self.device = next(d for d in self.proxy.config.hardware.device
                           if isinstance(d, vim.vm.device.VirtualDisk) and d.controllerKey == ctl.key
                           and d.unitNumber == unit)
        for _ in range(60):
            for n in set(os.listdir("/sys/block")) - before:
                sd = glob.glob(f"/sys/block/{n}/device/scsi_device/*")
                if sd and os.path.basename(sd[0]).split(":")[2] == str(unit) and \
                        int(open(f"/sys/block/{n}/size").read()) * 512 == self.capacity:
                    self.dev = "/dev/" + n
            if self.dev:
                break
            for h in glob.glob("/sys/class/scsi_host/host*/scan"):
                try:
                    open(h, "w").write("- - -")
                except OSError:
                    pass
            time.sleep(1)
        if not self.dev:
            self.__exit__(None, None, None)
            raise RuntimeError(f"hot-added {self.file} never appeared as a block device")
        return DevReader(self.dev)

    def __exit__(self, *exc):
        if self.dev:
            subprocess.call(["blockdev", "--flushbufs", self.dev], stderr=subprocess.DEVNULL)
            try:
                open(f"/sys/block/{os.path.basename(self.dev)}/device/delete", "w").write("1")
            except OSError:
                pass
        if self.device is not None:
            # still attached? (an operator or crash cleanup may have removed it)
            cur = next((d for d in self.proxy.config.hardware.device
                        if isinstance(d, vim.vm.device.VirtualDisk) and d.key == self.device.key
                        and d.backing.fileName == self.file), None)
            if cur is not None:
                with self._appliance_lock():
                    task(self.proxy.ReconfigVM_Task, vim.vm.ConfigSpec(deviceChange=[
                        vim.vm.device.VirtualDeviceSpec(operation="remove", device=cur)]))  # no fileOperation: never deletes
            self.device = None


def wait_idle(obj, timeout=1800):
    """Wait until no task is queued/running on a VM (e.g. a snapshot removal
    from an interrupted earlier run: new snapshot ops would fail with
    TaskInProgress / 'Another task is already in progress')."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        busy = [t for t in (obj.recentTask or [])
                if t.info.state in (vim.TaskInfo.State.running, vim.TaskInfo.State.queued)]
        if not busy:
            return
        log(f"waiting for {len(busy)} running task(s) on {obj.name}: {busy[0].info.descriptionId}")
        time.sleep(10)
    raise TimeoutError(f"{obj.name}: tasks still running after {timeout}s")


def task(fn, *args, retries=6, **kw):
    """Run a vSphere task method, retrying while the object is busy."""
    for i in range(retries):
        try:
            return WaitForTask(fn(*args, **kw))
        except (vim.fault.TaskInProgress, vim.fault.InvalidState) as e:
            if i == retries - 1:
                raise
            log(f"busy ({type(e).__name__}); retry in {10 * (i + 1)}s")
            time.sleep(10 * (i + 1))


def vm_disk_files(vm):
    """Every vmdk in the source VM's disk chains (base + snapshot deltas)."""
    files = set()
    for layout in (vm.layoutEx.disk if vm.layoutEx else []):
        for chain in layout.chain or []:
            files.update(f for k in chain.fileKey for f in
                         [x.name for x in vm.layoutEx.file if x.key == k])
    for d in disks_of(vm.config.hardware).values():
        b = d.backing
        while b is not None:
            files.add(b.fileName)
            b = getattr(b, "parent", None)
    return {f for f in files if f.endswith(".vmdk")}


def sweep(vc, vm, proxy, keep=None):
    """Crash recovery for THIS source VM only: our leftover snapshots on it (but
    `keep`, a group's consistent point this cycle copies from) and its disks still
    hot-added to the proxy. Disks of other source VMs on the same appliance belong
    to other (possibly running) migrations: never touched."""
    done = []
    wait_idle(vm)
    if proxy is not None:
        wait_idle(proxy)
        mine = vm_disk_files(vm)
        for d in proxy.config.hardware.device:
            if isinstance(d, vim.vm.device.VirtualDisk) and \
                    getattr(d.backing, "diskMode", "") == "independent_nonpersistent" and \
                    d.backing.fileName in mine:
                task(proxy.ReconfigVM_Task, vim.vm.ConfigSpec(deviceChange=[
                    vim.vm.device.VirtualDeviceSpec(operation="remove", device=d)]))
                done.append(f"detached stale {d.backing.fileName}")
    if vm.snapshot:
        for snap in _walk_snaps(vm.snapshot.rootSnapshotList):
            if snap.name.startswith(SNAP_PREFIX) and snap.name != keep:
                task(snap.snapshot.RemoveSnapshot_Task, False)
                done.append(f"removed stale snapshot {snap.name}")
    return done


# ---------------- receiver ----------------

def api(base, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.load(r)


def _clone(reader):
    if isinstance(reader, FlatReader):
        r = FlatReader.__new__(FlatReader)
        r.__dict__.update(reader.__dict__)
        r.conn = None
        return r
    return reader.clone()


def _prepare(buf, level, probe):
    """[(offset in buf, length, kind, payload)] per CHUNK: kind z (all zeros: no
    payload), c (zlib stream) or r (raw). `probe` is a shared {"miss": n} counter:
    after 8 chunks in a row that did not shrink, only one in 8 is tried."""
    out = []
    for o in range(0, len(buf), CHUNK):
        piece = buf[o:o + CHUNK]
        if piece.count(0) == len(piece):
            out.append((o, len(piece), "z", None))
            continue
        if level is not None and (probe["miss"] < 8 or probe["miss"] % 8 == 0):
            z = zlib.compress(piece, level)
            if len(z) < 0.9 * len(piece):
                probe["miss"] = 0
                out.append((o, len(piece), "c", z))
                continue
            probe["miss"] += 1
        elif level is not None:
            probe["miss"] += 1
        out.append((o, len(piece), "r", piece))
    return out


def push_extents(reader, nbd, extents, workers=4, level=None):
    """Read extents with `workers` parallel readers (which also compress, with zlib
    `level`, when the receiver takes compressed writes); write to NBD as they
    complete. Returns (data bytes, zero bytes, bytes of payload sent)."""
    sent = zeros = wire = 0
    readers = [reader] + [_clone(reader) for _ in range(workers - 1)]
    pool = cf.ThreadPoolExecutor(max_workers=workers)
    free = list(range(workers))
    pending = {}
    it = iter(extents)
    probe = {"miss": 0}
    level = level if getattr(nbd, "zlib", False) else None

    def work(k, s, n):
        return _prepare(readers[k].read(s, n), level, probe)

    def submit():
        try:
            s, n = next(it)
        except StopIteration:
            return False
        k = free.pop()
        pending[pool.submit(work, k, s, n)] = (k, s, n)
        return True

    while free and submit():
        pass
    while pending:
        done, _ = cf.wait(pending, return_when=cf.FIRST_COMPLETED)
        for fut in done:
            k, s, n = pending.pop(fut)
            pieces = fut.result()
            free.append(k)
            for o, ln, kind, payload in pieces:
                if kind == "z":
                    nbd.write_zeroes(s + o, ln); zeros += ln
                elif kind == "c":
                    nbd.zwrite(s + o, ln, payload); sent += ln; wire += len(payload) + 4
                else:
                    nbd.write(s + o, payload); sent += ln; wire += ln
            submit()
    pool.shutdown()
    return sent, zeros, wire


def parse_map(s):
    m = {}
    for item in s.split(","):
        d, e = item.split("=")
        m[d.strip()] = e.strip()
    return m


def power_off(vm, timeout):
    if vm.runtime.powerState == vim.VirtualMachinePowerState.poweredOff:
        return "already off"
    try:
        vm.ShutdownGuest()
        t0 = time.time()
        while time.time() - t0 < timeout:
            if vm.runtime.powerState == vim.VirtualMachinePowerState.poweredOff:
                return f"guest shutdown in {int(time.time() - t0)}s"
            time.sleep(3)
    except vim.fault.ToolsUnavailable:
        pass
    WaitForTask(vm.PowerOffVM_Task())
    return "hard power-off"


def open_reader(vc, vm, transport, proxy, sd):
    if transport == "hotadd":
        return HotAdd(proxy, sd.backing.fileName, sd.capacityInBytes)
    return contextlib.nullcontext(FlatReader(vc, vm, sd.backing.fileName))


def snapshot_many(vms, name, quiesce=False, strict=False):
    """One snapshot `name` on every VM, as close together as vSphere allows: every
    CreateSnapshot is issued before any is waited for. quiesce: through VMware Tools
    (VSS, fsfreeze); a VM that cannot quiesce gets a crash-consistent snapshot and
    the reason, unless strict (then every snapshot is removed and the fault raised).
    Returns ({vm name: {"quiesced", "reason", "start", "end"}}, spread in seconds:
    first start to last completion, vCenter's clock)."""
    for vm in vms:
        wait_idle(vm)
    tasks = {vm: vm.CreateSnapshot_Task(name, "exo-v2v consistent point", False, quiesce) for vm in vms}
    out, failed = {}, {}
    for vm, t in tasks.items():
        try:
            WaitForTask(t)
            out[vm.name] = {"quiesced": quiesce, "reason": None, "task": t}
        except vmodl.MethodFault as e:
            failed[vm] = getattr(e, "msg", None) or type(e).__name__
    for vm, why in failed.items():
        if not quiesce or strict:
            break
        log(f"{vm.name}: no quiesced snapshot ({why}): crash-consistent instead")
        t = vm.CreateSnapshot_Task(name, "exo-v2v consistent point", False, False)
        WaitForTask(t)
        out[vm.name] = {"quiesced": False, "reason": why, "task": t}
    if failed and (not quiesce or strict):
        for vm in tasks:
            remove_named(vm, name)
        raise RuntimeError("snapshot failed: " + "; ".join(f"{vm.name}: {w}" for vm, w in failed.items()))
    starts, ends = [], []
    for v in out.values():
        info = v.pop("task").info
        v["start"], v["end"] = info.startTime.timestamp(), info.completeTime.timestamp()
        starts.append(v["start"]); ends.append(v["end"])
    return out, round(max(ends) - min(starts), 1)


def remove_named(vm, name):
    """Remove every snapshot called `name` on vm (crash cleanup of a consistent point)."""
    n = 0
    if vm.snapshot:
        for s in _walk_snaps(vm.snapshot.rootSnapshotList):
            if s.name == name:
                task(s.snapshot.RemoveSnapshot_Task, False)
                n += 1
    return n


def sync(vc, vm, mapping, nbd_addr, api_base, full=False, final=False, shutdown_timeout=300, workers=4,
         transport="hotadd", proxy=None, pacer=None, snapshot_name=None):
    """One cycle. pacer(final) -> a wan.Pacer per disk (the WAN limit and compression), or None.
    snapshot_name: copy from that existing snapshot (a group's consistent point) and remove it."""
    cycle = {"vm": vm.name, "final": final, "transport": transport, "disks": {}}
    t_cycle = time.time()
    if final:
        cycle["power"] = power_off(vm, shutdown_timeout)
        log(f"final cycle: {cycle['power']}")
    disks = disks_of(vm.config.hardware)
    for d in mapping:
        if d not in disks:
            sys.exit(f"disk {d} not on VM (have {sorted(disks)})")
    wait_idle(vm)
    cur = disks_of(vm.config.hardware)
    # Powered-off VM: its disks are not changing and not locked, and
    # QueryChangedDiskAreas accepts snapshot=None ("current state") - skip the
    # snapshot round trip, which is pure downtime in the final cycle.
    # (Current-disk backings carry no changeId; only the final cycle may skip the
    # snapshot: it needs the previous changeId, not a new one.)
    snapless = final and vm.runtime.powerState == vim.VirtualMachinePowerState.poweredOff \
        and not vm.snapshot
    snap = None
    if snapshot_name and not final:
        hits = [s.snapshot for s in _walk_snaps(vm.snapshot.rootSnapshotList) if s.name == snapshot_name] \
            if vm.snapshot else []
        if not hits:
            sys.exit(f"snapshot {snapshot_name} is not on {vm.name}")
        snap = hits[0]
        cycle.update(snapshot_name=snapshot_name, consistent=True)
    elif not snapless:
        name = f"{SNAP_PREFIX}-{int(time.time())}"
        task(vm.CreateSnapshot_Task, name, "exo-v2v replication point", False, False)
        snap = [s.snapshot for s in _walk_snaps(vm.snapshot.rootSnapshotList) if s.name == name][0]
    cycle["snapshot"] = not snapless
    try:
        # frozen view: base backing + its changeId (or the powered-off VM's current disks)
        sdisks = cur if snapless else disks_of(snap.config.hardware)
        host, port = nbd_addr.split(":")
        for d, export in mapping.items():
            sd = sdisks[d]
            meta = {} if full else api(api_base, "GET", f"/meta?export={export}")
            prev = meta.get("change_id") or "*"
            if prev != "*" and meta.get("capacity") != sd.capacityInBytes:
                log(f"{d}: capacity changed -> full copy"); prev = "*"
            t0 = time.time()
            try:
                ext = changed_areas(vm, snap, sd, prev)
            except CbtInactive as e:
                ext = None
                if prev != "*":
                    # the change ID is not valid any more: CBT was reset (a snapshot
                    # revert, a disk extended, ...). Full copy of the allocated areas.
                    try:
                        ext = changed_areas(vm, snap, sd, "*")
                        log(f"{d}: change ID {prev} no longer valid (snapshot revert? CBT reset) -> full copy")
                        cycle.setdefault("cbt_resets", []).append(d)
                    except CbtInactive:
                        pass
                if ext is None:
                    # this cycle: whole-disk copy (correct, slower); CBT gets enabled for the next one
                    log(f"{d}: {e} -> whole-disk copy this cycle; enabling CBT for the next")
                    cycle.setdefault("cbt_fixups", []).append(d)
                    ext = [(0, sd.capacityInBytes)]
                prev = "*"
            covered = sum(n for _s, n in ext)
            ranges = coalesce(ext)
            pc = pacer(final) if pacer else None
            nbd = P.Client(host, int(port), export, window=16,
                           compress=bool(pc and pc.compress()), pacer=pc)
            if nbd.size < sd.capacityInBytes:
                sys.exit(f"{export}: receiver disk {nbd.size} < source {sd.capacityInBytes}")
            if prev == "*":
                # first cycle: the receiver disk is not blank -> zero what CBT calls unallocated
                gaps, pos = [], 0
                for s, n in sorted(ext):
                    if s > pos: gaps.append((pos, s - pos))
                    pos = max(pos, s + n)
                if pos < sd.capacityInBytes: gaps.append((pos, sd.capacityInBytes - pos))
                for s, n in gaps:   # <= 32 MiB per request: some servers cap every command
                    for o in range(0, n, P.MAX_REQUEST):
                        nbd.write_zeroes(s + o, min(P.MAX_REQUEST, n - o))
            with open_reader(vc, vm, transport, proxy, sd) as reader:
                sent, zeros, wire = push_extents(reader, nbd, ranges, workers,
                                                 level=pc.cfg["level"] if pc else None)
            nbd.flush(); nbd.close()
            new_id = getattr(sd.backing, "changeId", None) or prev
            api(api_base, "POST", f"/meta?export={export}",
                {"change_id": new_id, "capacity": sd.capacityInBytes, "vm": vm.name, "disk": d,
                 "synced_at": time.time(), "final": final})
            dt = time.time() - t0
            info = cycle["disks"][d] = {
                "export": export, "mode": "full" if prev == "*" else "delta",
                "extents": len(ext), "covered": covered, "sent": sent, "zeros": zeros,
                "seconds": round(dt, 1), "mib_s": round(covered / dt / 2**20, 1) if dt else 0,
                "wire": wire, "wire_mib_s": round(wire / dt / 2**20, 1) if dt else 0,
                "compression": ("zlib%d" % pc.cfg["level"] if nbd.zlib else
                                "unsupported" if pc and pc.compress() else "off")}
            if pc:
                info.update(pc.stats())
            log(f"{d} -> {export}: {info['mode']} {len(ext)} extents, "
                f"{covered / 2**20:.0f} MiB covered, {sent / 2**20:.0f} MiB data"
                + (f", {wire / 2**20:.0f} MiB sent compressed" if nbd.zlib else "")
                + (f", throttled {info['throttled_s']:.0f}s at {info['limit_bps'] * 8 / 1e6:.0f} Mbit/s"
                   if pc and pc.bps else "")
                + f", {dt:.1f}s")
    finally:
        if snap is not None:
            task(snap.RemoveSnapshot_Task, False)
    if cycle.get("cbt_fixups") and not final:
        enable_cbt(vc, vm)
    cycle["seconds"] = round(time.time() - t_cycle, 1)
    return cycle


def verify(vc, vm, mapping, api_base, chunk=64 << 20, transport="hotadd", proxy=None):
    """Hash the frozen source (snapshot) per chunk and compare with GET /hashmap."""
    name = f"{SNAP_PREFIX}-verify-{int(time.time())}"
    wait_idle(vm)
    task(vm.CreateSnapshot_Task, name, "exo-v2v verify", False, False)
    snap = [s.snapshot for s in _walk_snaps(vm.snapshot.rootSnapshotList) if s.name == name][0]
    res = {}
    try:
        for d, export in mapping.items():
            sd = disks_of(snap.config.hardware)[d]
            remote = api(api_base, "GET", f"/hashmap?export={export}&chunk={chunk >> 20}M")
            bad = []
            with open_reader(vc, vm, transport, proxy, sd) as rd:
                for i, off in enumerate(range(0, sd.capacityInBytes, chunk)):
                    n = min(chunk, sd.capacityInBytes - off)
                    if hashlib.sha256(rd.read(off, n)).hexdigest() != remote["sha256"][i]:
                        bad.append(off)
            res[d] = {"export": export, "chunks": i + 1, "mismatched": len(bad), "first_bad": bad[:3]}
            log(f"verify {d} -> {export}: {'IDENTICAL' if not bad else f'{len(bad)} MISMATCHED chunks'}")
    finally:
        task(snap.RemoveSnapshot_Task, False)
    return res
