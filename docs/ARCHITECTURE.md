# exo-v2v architecture

> **Experimental.** exo-v2v 0.3.0 is an early release, validated in one lab (ESXi 7.0 U3 to Exoscale ch-gva-2). Try it on non-critical VMs first; commands and behaviour may change.

## Components

| Component | Runs on | Code |
|---|---|---|
| CLI / orchestrator | replication appliance (Linux VM in vSphere) | `exo_v2v/cli.py`, `plan.py`, `state.py`, `exo.py`, `tunnel.py` |
| Group orchestrator | appliance; members run as child processes | `exo_v2v/group.py` |
| Web status page (read-only) | appliance | `exo_v2v/web.py`, `web.html` |
| vSphere replication (sender) | appliance, as root | `exo_v2v/vsphere.py` |
| Receiver agent | the target Exoscale instance, booted **from RAM** | `exo_v2v/receiver/agent.py`, `nbdserve.py`, `guestfix.py` |
| Receiver bootstrap (stock template) | target instance, first boot | `receiver/bootstrap-kexec.sh`; `userdata.py` (key-only user-data, payload tarball) |
| Receiver template image | built once, registered as an Exoscale template | `template/build-image.sh`, `template/v2v-userdata-key` |

## Why the target instance is the receiver

There is no staging copy: the bytes land on the disks that will run the
workload. The instance keeps its boot mode (and Secure Boot / TPM, set with
`--secureboot` / `--tpm` from the source's settings) for life, so it is created
in the source's firmware mode. Its receiver system must run from RAM, so that
`/dev/vda` can be overwritten. There are two ways to get there.

**Receiver template** (`plan --receiver-template`, see
[receiver-templates.md](receiver-templates.md)). The template's disk holds a
BIOS boot partition and an EFI system partition; Canonical-signed shim and GRUB
load the kernel and a ~220 MB initramfs that *is* the root filesystem
(mmdebstrap Ubuntu 24.04 with virt-v2v, libguestfs, QEMU, filesystem tools).
At boot, `v2v-userdata-key` installs the SSH key from the instance user-data for
user `v2v`, `v2v-hostkeys` generates host keys, and on the Windows variant
`v2v-winsupport` copies virtio-win and rhsrvany from the EFI partition into RAM
(kept out of the initramfs: GRUB loading 400+ MB through firmware I/O did not
always finish). `create` waits for SSH before attaching volumes (a disk
hot-plugged while the firmware loads the initramfs can hang the boot), then
streams the agent and restarts `v2v-agent`.

**Stock template** (default). The instance is created from Debian 13 (BIOS) or
Ubuntu 24.04 (UEFI). Cloud-init only installs the migration's SSH key;
`exo-v2v create` then streams the agent payload (tar.gz) over SSH and starts the
bootstrap detached (Exoscale caps user-data at 32 KiB base64, which the payload
outgrew). The bootstrap waits for cloud-init and the dpkg lock, installs
`virt-v2v`/libguestfs/xfsprogs, copies the running OS into a RAM
filesystem, packs it as an initramfs and `kexec`s into it with the same kernel.
From then on nothing uses `/dev/vda` or the volumes: the agent exports them
over NBD on `127.0.0.1` only.

Consequence (documented limit): if the receiver reboots during the sync window,
it boots the half-written disk. `exo-v2v` detects a receiver that left its RAM
root and asks for recreation; a restarted agent (lost `/meta`) triggers a full
cycle automatically.

## Data path

```
appliance                                              receiver (RAM root)
vsphere.sync ─ NBD client ─ ssh -L ─ (TCP 22) ─ sshd ─ nbdserve 127.0.0.1:10809 ─ /dev/vdX
cli          ─ HTTP       ─ ssh -L ─ (TCP 22) ─ sshd ─ agent API 127.0.0.1:8787
```

- **Authentication**: a per-migration ed25519 key, injected through cloud-init
  user-data; a per-migration security group opens TCP 22 to the appliance's
  egress address only. No other port is exposed during the sync window.
- **NBD**: fixed-newstyle, simple replies, 32 MiB max payload, WRITE_ZEROES
  implemented with `BLKZEROOUT` (offloadable) instead of writing zeros.

## Replication without VDDK

Per cycle and per disk:

1. `CreateSnapshot` (no memory, no quiesce) → the snapshot's disk backing is
   the frozen base `.vmdk` + its CBT `changeId`.
2. `QueryChangedDiskAreas(snapshot, disk, changeId)`; `"*"` on the first cycle
   (allocated areas; the rest is zeroed on the target).
3. **Hot-add**: `ReconfigVM` on the appliance adds the base `.vmdk` as
   `independent_nonpersistent` — the source is opened read-only, appliance
   writes would go to a throw-away redo log. The block device is located by
   SCSI target = unit number and size; ranges are read with `pread`
   (parallel workers) and written to NBD; zero chunks become WRITE_ZEROES.
4. Detach (no file operation — never deletes), `POST /meta {change_id}` on
   the receiver, `RemoveSnapshot`.

A VM on a chain of snapshot deltas needs nothing special: the snapshot's
backing is the top of the chain, and hot-add opens the whole chain read-only.
A `changeId` that vSphere no longer accepts (a snapshot revert resets CBT: new
change-tracking UUID, the old IDs raise `FileFault`) makes that disk's cycle a
copy of its allocated areas (`"*"`), the rest zeroed.

### Disks changing between cycles

`reconcile_disks` compares the VM's disks with the plan before every cycle
(and before the cutover's downtime):

- **grown Block Storage disk**: detach, `update --size` (Exoscale resizes
  detached volumes only), attach, find the device by serial, `POST /exports`
  (the NBD server restarts with the new device; the export's change ID is
  dropped, so the next cycle copies it whole);
- **grown system disk or folded disk** beyond the root disk: the receiver is
  rebuilt with `reset-instance` (stop, re-image the root disk from the template
  at the new size, start, agent again). Volumes are detached first and their
  change IDs saved and restored, so only the root disk is copied again. The
  state records `rebuild_pending` until it completes: an interrupted rebuild is
  resumed by the next `sync`/`cutover`;
- **new disk**: a new volume and export;
- **removed disk**: stop.

The agent maps volumes to exports by virtio serial (Exoscale: the volume ID's
first 20 characters, `volumes.json` in the payload), not by device order.

### Folded disks

A disk listed in `folds.json` (export, byte offset, size) is a region of the
root disk after the system disk (1 MiB aligned, 1 MiB kept free at the end for
a backup GPT). The agent exports it through a loop device (`--direct-io`). At
`/convert`, before `virt-v2v`, the loop devices are released and
`guestfix.fold` adds the region's partitions (or the whole region when it has
no table) to the root disk's MBR or GPT, keeping the partition types and
GPT GUIDs. `virt-v2v` then sees the guest's volume groups complete, on
512-byte sectors.

Why not the datastore HTTP file service: hostd needs an exclusive lock to
serve `*-flat.vmdk`; a running VM's base disk is locked (`Failed to lock file`,
HTTP 500). `--transport http` remains for powered-off VMs.

Robustness: a host-wide `flock` serializes hot-add attach **and detach** on one
appliance (concurrent migrations otherwise collide in vSphere with
`TaskInProgress`, and attach retries it); a
per-migration lock forbids two writers; at start the sweep removes **this
source VM's** leftover `v2v-sync-*` snapshots and hot-added disks (never other
VMs'); vSphere task methods wait for the VM to be idle and retry
`TaskInProgress`. CBT is enabled VM-wide **and per disk** (`scsiX:Y.ctkEnabled`)
with a snapshot stun cycle; a disk whose CBT is inactive is copied whole for
that cycle.

The changeId is stored on the receiver (RAM), not on the appliance: it is only
valid together with the exact bytes on the receiver's disks.

## Cutover and conversion

1. Final cycle after guest shutdown (Tools) / power-off.
2. `POST /convert`: `virt-v2v-in-place -i disk /dev/vda /dev/vdb …` with
   nested KVM (`LIBGUESTFS_BACKEND=direct`); Windows gets `VIRTIO_WIN` +
   rhsrvany (staged by `win-support.sh`, or built into the Windows receiver
   template).
3. `guestfix.adapt` on each data volume, LVM analysis across all disks
   (`lvm_report`), `guestfix.netfix` on the root disk.
   On Windows, `netfix` instead queues a script in virt-v2v's firstboot
   framework that registers a SYSTEM scheduled task: it sets the SAN policy to
   `OnlineAll` and onlines the data disks, retries until the storage services
   answer, and deletes itself. The script re-queues itself first, because
   firstboot can run it while Windows is shutting down for a driver reboot.
4. `POST /relayout {"spares": {export: device}}` per *relayout group* (one
   volume, or the volumes a volume group spans), onto spare volumes the CLI
   creates and attaches; the CLI then detaches the originals (kept, labelled
   `exo-v2v-rollback`). Groups run smallest first; each needs a free volume slot
   per member.
5. `stop` → `scale` (conversion needs ≥ 8 GiB RAM: receivers run as
   `standard.large` at least) → `start` → attach the user's security groups →
   wait for 22/3389.

## 4Kn Block Storage

Exoscale volumes expose 4096-byte logical sectors; the instance root disk
exposes 512. VMware disks are 512. `guestfix`:

- **analyze**: partition table kind and sector unit (GPT header at LBA 1 of 512
  or 4096; for MBR the unit is inferred from where a filesystem signature
  sits — MBR does not record it), filesystems per partition: XFS `sectsz`,
  ext block size, NTFS bytes/sector, LVM PV, swap.
- **adapt**: rewrite GPT (pMBR, header, entries, backup, CRC32s) or MBR in 4K
  units in place; requires 4 KiB-aligned partitions and ≥ 1 MiB slack at the
  end of the volume (the CLI provisions volumes with that slack).
- **NTFS**: the boot sector's bytes-per-sector (and the derived geometry) is
  rewritten for 4096 in place (`ntfs_rebps`); clusters must be ≥ 4 KiB. The
  original boot sector is kept for revert.
- **relayout**: copy table region + tail, `adapt` the spare, block-copy
  compatible partitions, recreate XFS-512 / ext-1K with 4K geometry and the
  same UUID/label, fill with `xfsdump | xfsrestore` (XFS) or `rsync -aAXHS`,
  reading the source through a read-only `losetup --sector-size 512` view.
- **LVM** (`lvmlayout.py`): the receiver never activates the guest's volume
  groups. It reads the PV labels, PV headers and the current text metadata from
  the disks, maps each logical volume to byte ranges, and probes the
  filesystem at its start. A linear or striped LV with a 512-only filesystem on
  4Kn volumes is rebuilt: the PV headers and metadata areas, and the extents of
  the other LVs, are copied byte for byte onto the spares (same PV/VG/LV UUIDs,
  same extents); the LV is assembled twice with `dmsetup` (old: over
  read-only 512-sector loop views of the sources; new: over the spares) and
  `rebuild_fs` runs between the two. A volume group with a PV on the system
  disk is not rebuilt: its other disks must be folded.
- **Windows dynamic disks / Storage Spaces** (GPT partition types for LDM
  metadata and data, MBR type `0x42`, Storage Spaces): their own metadata
  counts 512-byte sectors; they are left untouched and reported.

## Network fix-up

virt-v2v converts drivers but not interface naming. `v2v-netfix.service`
(oneshot, `Before=network-pre.target`, every boot, idempotent) maps configured
names that no longer exist to NICs that are not configured, in order, across
ifupdown, netplan, NetworkManager keyfiles and ifcfg; turns `allow-hotplug`
into `auto` (the hotplug event may have fired before the fix); removes VMware
OUI MAC pins and persistent-net rules; runs `netplan generate`; falls back to
DHCP on the first NIC. Log: `/var/log/v2v-netfix.log`.

With a plan that maps NICs (groups), `POST /convert {"netmap": [...]}` carries
the instance's NICs by MAC (`exo x get-instance`: the CLI's `instance show`
has no MACs), public first. The receiver writes `/etc/v2v-netfix.map`
(`MAC static ADDR/LEN nogw` or `MAC dhcp`) and the service applies it first:
the configuration holding each static address is renamed to the NIC with that
MAC (in two phases through temporary names, so `eth0` → `eth1` swaps work), its
gateway is removed (`gateway`, `gateway4`, default routes, `GATEWAY=` with
`DEFROUTE=no`, NetworkManager `never-default=true`), and a DHCP configuration
is written in the guest's own stack for a mapped NIC left without one. A
profile bound only by MAC gets an `interface-name`. Windows: a first-boot
scheduled task (same pattern as the disk task) sets the addresses with
`New-NetIPAddress` on the adapters with those MACs.

## Groups

`group plan` builds each member's plan (`plan.build` with the NIC map from
`plan.network_map`) and stores `groups/NAME/group.json` next to the member
states (`GROUP-VM`, `state.data["group"]`). `group create` makes the private
networks (managed, range = the subnet minus the top address), anti-affinity
groups (create-time only on Exoscale, so they go into `instance create`) and
security groups, then runs `create` per member; `create` attaches the private
networks with `--ip` (static leases) and records the NIC MACs.

Members' `sync` and `cutover` run as child processes (`python -m exo_v2v ...
--in-group`) in their own sessions, each writing its log file: a member keeps
going if the group process dies, and a group command run again finds it by the
pid in the member's lock file. The per-VM cutover is split into recorded steps
(`cutover_steps`: `down_at`, `source_off`, `final_cycle`, `convert_started`,
`converted`, `flushed`, `stopped`, `scaled`, `started`) so that it resumes
anywhere; `cutover --no-start` stops after the flush. The group cutover is a
state machine in `group.json` (`preflight`, `warm`, `pre_hook`, `stop_started`,
`tierN_off`, `converted`, `tierN_up`, `post_hook`): it starts the member
cutovers tier by tier from the front, waits for each tier's `source_off`
before shutting down the next, waits for every conversion, stops and scales
all instances, then starts tier by tier from the back with health checks.
`group rollback` stops the converted instances front first and calls the
per-VM rollback back end first; a member whose conversion had not started
keeps its receiver and returns to `syncing`.

## Web status page

`exo-v2v web` is a `ThreadingHTTPServer` serving `web.html` (inline CSS and
JavaScript, no external requests) and a JSON API over the state files. It
answers `GET`/`HEAD` only, never opens a state file for writing and never takes
a lock: running commands are found from the pid each lock file records (checked
against `/proc/PID/cmdline`). Member logs are served by member name only, never
by a path from the URL. Off loopback it requires a token (bearer header or an
HTTP-only cookie set by `/login`).
