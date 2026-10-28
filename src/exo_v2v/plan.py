"""Migration plan: compatibility checks + Exoscale sizing from a vSphere inventory."""
import ipaddress

from .exo import BIOS_TEMPLATE, UEFI_TEMPLATE
from .util import gib_ceil

# Exoscale "standard" family: (name, vCPU, RAM GiB)
STANDARD_TYPES = [("tiny", 1, 1), ("small", 1, 2), ("medium", 2, 4), ("large", 4, 8),
                  ("extra-large", 4, 16), ("huge", 8, 32), ("mega", 12, 64), ("titan", 16, 128),
                  ("jumbo", 24, 225)]
# Conversion runs virt-v2v (nested KVM + libguestfs appliance) inside the RAM
# root: needs >= 8 GiB RAM and nested virtualization (verified on standard.large).
CONVERT_MIN = "standard.large"
MAX_VOLUMES = 5          # Block Storage volumes per instance (+ the root disk)
MIN_DISK_GB = 10
MiB = 1 << 20
CTL_ORDER = ("scsi", "sata", "nvme", "ide")


def disk_key(d):
    """scsi0:1 -> (0, 0, 1): controller type, bus, unit."""
    kind = d.rstrip("0123456789:")
    bus, unit = (int(x) for x in d[len(kind):].split(":"))
    return (CTL_ORDER.index(kind) if kind in CTL_ORDER else len(CTL_ORDER), bus, unit)


def align(n, a=MiB):
    return -(-n // a) * a


def fold_set(sysd, mounts, explicit=()):
    """Disks to carry inside the system disk: every disk that shares a guest
    filesystem with it (transitively), e.g. a root volume group extended onto a
    second VMDK, plus the explicit ones."""
    fold = set(explicit) - {sysd}
    changed = True
    while changed:
        changed = False
        for m in mounts:
            ds = set(m["disks"])
            if len(ds) > 1 and ds & ({sysd} | fold) and not ds - {sysd} <= fold:
                fold |= ds - {sysd}
                changed = True
    return fold


def fold_layout(root_cap, folds):
    """[(disk, offset)] after the system disk, 1 MiB aligned, and the root disk
    size in GB (1 MiB left at the end for a backup GPT)."""
    if not folds:
        return [], max(MIN_DISK_GB, gib_ceil(root_cap))
    pos, out = align(root_cap), []
    for d, cap in folds:
        out.append((d, pos))
        pos = align(pos + cap)
    return out, max(MIN_DISK_GB, gib_ceil(pos + MiB))


def privnet_range(cidr):
    """(start, end, netmask) of an Exoscale managed private network for `cidr`:
    every host address except the top one, which the platform keeps (a /24
    may end at .253, not .254)."""
    n = ipaddress.ip_network(cidr, strict=False)
    return str(n.network_address + 1), str(n.broadcast_address - 2), str(n.netmask)


def network_map(nics, networks, ips=None):
    """Target NICs of a VM, in the order the instance gets them: the public
    interface (DHCP) first, then one private network per source NIC.
    `networks`: {name: {"source": port group, "type": "public"|"private",
    "cidr", "keep_addresses", "exoscale_name"}}; `ips`: {network name: address}
    overrides. Returns (entries, errors)."""
    ips = ips or {}
    by_pg = {n["source"]: (name, n) for name, n in networks.items()}
    out, errors, public = [], [], None
    for nic in nics:
        hit = by_pg.get(nic.get("port_group"))
        where = f"{nic['label']} ({nic['mac']}, port group {nic.get('port_group')!r})"
        if not hit:
            errors.append(f"{where}: port group not mapped in the manifest's networks")
            continue
        name, n = hit
        if n["type"] == "public":
            if public:
                errors.append(f"{where}: a second NIC on the public interface ({public['name']})")
                continue
            public = {"network": "public", "name": name, "source_mac": nic["mac"], "mode": "dhcp"}
            continue
        e = {"network": n["exoscale_name"], "name": name, "source_mac": nic["mac"], "mode": "dhcp",
             "port_group": nic.get("port_group")}
        src = ipaddress.ip_network(n["cidr"], strict=False)
        net = ipaddress.ip_network(n.get("renumber") or n["cidr"], strict=False)
        if n.get("keep_addresses", True):
            cand = [a for a in nic.get("ips", []) if ipaddress.ip_address(a["ip"]) in src]
            old = cand[0]["ip"] if cand else None
            want, origin = ips.get(name), "manifest"
            if not want and old:
                want, origin = old, cand[0].get("origin") or "guest"
                if net != src:          # renumber: same host part in the new network
                    want = str(net.network_address + (int(ipaddress.ip_address(old)) - int(src.network_address)))
            if want and old and old != want:
                e["from"] = old         # the guest's configuration is rewritten (v2v-netfix)
            if not want:
                errors.append(f"{where}: address on {name} unknown (VMware Tools not running?): "
                              f"set `ip: {{{name}: A.B.C.D}}` for this VM")
            else:
                a = ipaddress.ip_address(want)
                if a not in net or a in (net.network_address, net.broadcast_address):
                    errors.append(f"{where}: {want} is not a host address of {name} ({net})")
                elif a == net.broadcast_address - 1:
                    errors.append(f"{where}: {want} is the address Exoscale keeps for its DHCP server on "
                                  f"{net}: renumber this VM, or give {name} a larger cidr")
                # a guest on DHCP gets the same address from the static lease
                e.update(ip=want, prefix=net.prefixlen, origin=origin,
                         mode="dhcp" if origin == "dhcp" else "static", nogw=True)
        out.append(e)
    return [public or {"network": "public", "name": None, "source_mac": None, "mode": "dhcp"}] + out, errors


def instance_type_for(vcpus, mem_mb):
    gib = mem_mb / 1024
    for name, c, m in STANDARD_TYPES:
        if c >= vcpus and m >= gib:
            return f"standard.{name}"
    return f"standard.{STANDARD_TYPES[-1][0]}"


def _rank(itype):
    names = [n for n, _c, _m in STANDARD_TYPES]
    return names.index(itype.split(".", 1)[1]) if itype.startswith("standard.") and \
        itype.split(".", 1)[1] in names else len(names)


def os_family(guest_id):
    g = (guest_id or "").lower()
    if g.startswith("win"):
        return "windows"
    return "linux" if g else "unknown"


def build(inv, zone, target_type=None, system_disk=None, security_groups=(), receiver_template=None,
          receiver_template_visibility="public", receiver_template_boot_mode=None, fold=(), auto_fold=True,
          networks=None, anti_affinity_groups=()):
    errors, warnings, notes = [], [], []
    disks = sorted(inv["disks"], key=lambda d: disk_key(d["id"]))
    if not disks:
        errors.append("VM has no virtual disks")
    for d in disks:
        if d["mode"].startswith("independent"):
            errors.append(f"{d['id']} is {d['mode']}: independent disks are excluded from snapshots, "
                          "so CBT cannot replicate them (switch to dependent/persistent)")
        if not d["file"] or not d["file"].endswith(".vmdk"):
            errors.append(f"{d['id']}: unsupported backing {d['file']!r} (RDM?)")
    sysd = system_disk or inv.get("boot_disk") or (disks[0]["id"] if disks else None)
    if disks and sysd not in [d["id"] for d in disks]:
        errors.append(f"system disk {sysd} not on the VM")
    elif not system_disk and len(disks) > 1:
        how = inv.get("boot_disk_source") or "first disk"
        if how in ("controller order", "first disk"):
            warnings.append(f"system disk {sysd} is a guess ({how}: no boot order and no VMware Tools "
                            "disk mapping): check it, or pass --system-disk")
        else:
            notes.append(f"system disk {sysd} (from the {how})")
    mounts = inv.get("guest_mounts") or []
    fam = os_family(inv["guest_id"])
    if fam == "windows":
        for m in mounts:
            if len(m["disks"]) > 1 or not m["disks"]:
                where = f"spans {', '.join(m['disks'])}" if m["disks"] else "is not on a basic disk VMware Tools can map"
                warnings.append(f"{m['path']} {where}: a Windows dynamic volume (spanned, striped or mirrored) or "
                                "a Storage Spaces volume? Such disks cannot move to 4Kn Block Storage volumes and "
                                "will not be readable on Exoscale: convert to basic disks first")
        auto_fold = False             # a dynamic disk must stay a whole disk
    unknown = [f for f in fold if f not in [d["id"] for d in disks]]
    if unknown:
        errors.append(f"--fold {', '.join(unknown)}: not a disk of the VM")
    folded = fold_set(sysd, mounts if auto_fold else [], [f for f in fold if f not in unknown])
    for m in mounts:
        if len(m["disks"]) > 1 and set(m["disks"]) & folded:
            notes.append(f"{m['path']} spans {', '.join(m['disks'])}: "
                         f"{', '.join(sorted(set(m['disks']) & folded))} carried inside the root disk "
                         "(a 4Kn volume cannot hold part of a 512-sector volume group)")
    nvol = len(disks) - 1 - len(folded)
    if nvol > MAX_VOLUMES:
        errors.append(f"{nvol} data disks: Exoscale attaches at most {MAX_VOLUMES} Block Storage volumes "
                      "(--fold DISK carries a disk inside the root disk instead)")
    if inv["firmware"] not in ("bios", "efi"):
        errors.append(f"unknown firmware {inv['firmware']}")
    secure_boot = bool(inv.get("secure_boot")) and inv["firmware"] == "efi"
    if secure_boot:
        notes.append("Secure Boot is on at the source: the instance is created with Secure Boot "
                     "(Exoscale sets it at creation only)")
    if inv.get("vtpm"):
        notes.append("the source has a vTPM: the instance is created with a TPM; secrets sealed to "
                     "the VMware vTPM (BitLocker, LUKS TPM unlock) do not move with it")
    if not inv["cbt_enabled"]:
        notes.append("CBT is off: `exo-v2v sync` enables it (a snapshot cycle activates it without downtime)")
    foreign = [n for n in inv["snapshots"] if not n.startswith("v2v-sync")]   # ours are transient
    if foreign:
        warnings.append(f"existing snapshots {foreign}: replication works, but consolidate them first if you can")
    if networks is None and len(inv["nics"]) > 1:
        warnings.append(f"{len(inv['nics'])} NICs: the instance gets one public interface; "
                        "additional networks must be attached as Exoscale Private Networks afterwards "
                        "(or migrate it in a group, whose manifest maps every port group)")
    if fam == "windows":
        notes.append("Windows: `create` stages virtio-win + rhsrvany on the receiver (needs internet egress from the receiver)")
    if fam == "unknown":
        warnings.append(f"guest OS unknown ({inv['guest_id']}); conversion relies on virt-v2v inspection")

    target = target_type or instance_type_for(inv["cpus"], inv["memory_mb"])
    convert_type = target if _rank(target) >= _rank(CONVERT_MIN) else CONVERT_MIN
    if convert_type != target:
        notes.append(f"receiver runs as {convert_type} for conversion; scaled to {target} at cutover")

    if receiver_template:
        # exo-v2v receiver template: boots straight into the RAM-root receiver
        receiver_mode, template, visibility, receiver_user = \
            "template", receiver_template, receiver_template_visibility, "v2v"
        want = "uefi" if inv["firmware"] == "efi" else "legacy"
        if receiver_template_boot_mode and receiver_template_boot_mode != want:
            errors.append(f"receiver template {receiver_template!r} boots {receiver_template_boot_mode}, "
                          f"the VM needs {want}: use the {want} variant")
    else:
        # stock template + bootstrap that rebuilds itself into RAM and kexecs
        receiver_mode, visibility = "kexec", "public"
        template = UEFI_TEMPLATE if inv["firmware"] == "efi" else BIOS_TEMPLATE
        receiver_user = "ubuntu" if inv["firmware"] == "efi" else "debian"

    root = [d for d in disks if d["id"] == sysd]
    vols = [d for d in disks if d["id"] != sysd and d["id"] not in folded]
    folds = [d for d in disks if d["id"] in folded]
    fold_at, root_gb = fold_layout(root[0]["capacity"] if root else 0, [(d["id"], d["capacity"]) for d in folds])
    fold_at = dict(fold_at)
    mapping, volumes = [], []
    for i, d in enumerate(root + vols + folds):
        export = "sd" + chr(ord("a") + i)
        e = {"disk": d["id"], "export": export, "capacity": d["capacity"], "label": d["label"]}
        if i == 0:
            e.update(role="root", size_gb=root_gb)
        elif d["id"] in folded:
            e.update(role="fold", offset=fold_at[d["id"]], size_gb=0)
        else:
            # volumes get >= 1 MiB slack: a GPT rescaled to 4096-byte sectors needs
            # 20 KiB at the end for its backup (16.5 KiB at 512), and a last partition
            # that reaches the last 512-byte usable LBA would otherwise overlap it
            e.update(role="volume", size_gb=max(MIN_DISK_GB, gib_ceil(d["capacity"] + MiB)))
            volumes.append(e["size_gb"])
        mapping.append(e)
    return {
        "vm": inv["name"], "zone": zone, "os_family": fam, "guest_id": inv["guest_id"],
        "firmware": inv["firmware"], "secure_boot": secure_boot, "tpm": bool(inv.get("vtpm")),
        "receiver_mode": receiver_mode, "template": template, "template_visibility": visibility,
        "receiver_user": receiver_user,
        "target_type": target, "convert_type": convert_type,
        "root_gb": mapping[0]["size_gb"] if mapping else 0, "volumes_gb": volumes,
        "folds": [{"export": e["export"], "disk": e["disk"], "offset": e["offset"], "size": e["capacity"]}
                  for e in mapping if e["role"] == "fold"],
        "disks": mapping, "security_groups": list(security_groups),
        "networks": networks or [], "anti_affinity_groups": list(anti_affinity_groups),
        "errors": errors, "warnings": warnings, "notes": notes,
    }
