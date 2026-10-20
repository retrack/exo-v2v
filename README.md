# exo-v2v — warm migration of VMware vSphere VMs to Exoscale

> [!WARNING]
> **Experimental.** exo-v2v 0.1.0 is an early release, validated in one lab (ESXi 7.0 U3 to Exoscale ch-gva-2). Try it on non-critical VMs first; commands and behaviour may change.

`exo-v2v` moves a running VMware VM to an Exoscale Compute instance with a
downtime of minutes, regardless of disk size:

1. **Replicate while the VM runs.** The VM's disks are copied to their final
   Exoscale disks, then kept in sync with VMware Changed Block Tracking (CBT):
   each cycle copies only what changed since the previous one.
2. **Cut over.** The VM is shut down, the last changes are copied, the guest is
   converted for KVM *on the target instance itself* (`virt-v2v-in-place`), and
   the instance boots the migrated OS from its own disks.

No VDDK, no vSphere plugin, no agent inside the guest, no staging storage.

![exo-v2v architecture](docs/diagrams/architecture.svg)

## Documentation

Read it at **https://retrack.github.io/exo-v2v/**, or in [`docs/`](docs/):

| Page | What it covers |
|---|---|
| [How it works](docs/concepts.md) | The replication appliance, sync cycles, cutover, guest adaptation, security model |
| [Prerequisites](docs/prerequisites.md) | vSphere privileges, the appliance, network flows, Exoscale account |
| [IAM role](docs/iam.md) | A least-privilege Exoscale role for the appliance's API key |
| [Receiver templates](docs/receiver-templates.md) | Templates that boot straight into the receiver: faster `create`, no mirror access |
| [Supported operating systems](docs/supported-os.md) | Tested and expected-to-work guests |
| [Limitations](docs/limitations.md) | Known limits and their workarounds |
| [Operations runbook](docs/OPERATIONS.md) | Step-by-step migration, rollback, troubleshooting |
| [Architecture](docs/ARCHITECTURE.md) | Design details for contributors |

## Requirements

| Where | What |
|---|---|
| vSphere | vCenter or standalone ESXi 6.7+ (tested: ESXi 7.0 U3). An account allowed to snapshot the source VM and reconfigure the appliance VM (add/remove disk). |
| Replication appliance | A Linux VM **in the same vSphere environment**, on a host that sees the source VM's datastores (hot-add transport, the same mechanism backup products use). Python ≥ 3.10, `ssh`, the [`exo` CLI](https://github.com/exoscale/cli) ≥ 1.102, a free paravirtual SCSI slot. Runs `sync`/`cutover` as **root**. Outbound TCP 22 to Exoscale, 443 to vCenter/ESXi. |
| Exoscale | An API key restricted to Compute (instances, Block Storage, security groups) in the `exo` CLI config (or `EXOSCALE_API_KEY`/`EXOSCALE_API_SECRET`); see [IAM role](docs/iam.md). |
| Source VM | Linux or Windows Server ([supported operating systems](docs/supported-os.md)); BIOS or UEFI, with or without Secure Boot; ≤ 6 disks (1 root + 5 volumes); no independent/RDM disks. CBT is enabled automatically. |

## Install

```bash
pipx install https://github.com/retrack/exo-v2v/releases/download/v0.1.0/exo_v2v-0.1.0-py3-none-any.whl
# or from source: pipx install git+https://github.com/retrack/exo-v2v@v0.1.0   (pulls pyvmomi)
exo config                          # Exoscale credentials for the exo CLI
export VSPHERE_HOST=vcenter.example VSPHERE_USER=migration@vsphere.local VSPHERE_PASSWORD=…
```

## Quickstart

```bash
exo-v2v plan    web01 --vm web01 --zone ch-gva-2 --security-group web
exo-v2v create  web01                   # receiver instance + volumes: ~1 min with a receiver template, 5-15 min without
sudo -E exo-v2v sync web01              # first cycle = full copy (VM keeps running)
sudo -E exo-v2v sync web01 --every 900  # keep it fresh until the maintenance window (Ctrl-C)
sudo -E exo-v2v cutover web01           # downtime starts here
exo-v2v status  web01
exo-v2v cleanup web01                   # after you validated the workload
```

`plan` prints everything it will do and refuses unsupported VMs; nothing is
created before `create`. Every step is idempotent and resumable; state lives in
`~/.exo-v2v/<name>/` (`EXO_V2V_HOME` to move it).

## What cutover does

1. Shuts the VM down (guest shutdown via VMware Tools, hard power-off after
   `--shutdown-timeout`), copies the final changes. *(`--verify` additionally
   hash-compares source and target disks.)*
2. Windows: uses the virtio-win drivers that `create` staged on the receiver.
3. `virt-v2v-in-place` on the receiver: virtio drivers, initramfs, removal of
   VMware Tools, bootloader fixes — using nested KVM on the receiver.
4. **Exoscale-specific fix-ups** (see below), relayout of incompatible volumes.
5. Restarts the instance from its own disks, scales it to the target type,
   attaches your security groups, waits for SSH (Linux) / RDP (Windows).
   Windows then finishes driver installation on its own for a few minutes (it may
   reboot); data disks come online at the end of that.

`rollback` powers the source VM back on and stops the instance; the source is
never modified by the migration (snapshots are removed after every cycle).

## Exoscale-specific fix-ups (automatic)

| Issue | Why | What exo-v2v does |
|---|---|---|
| Network config bound to VMware NIC names/MACs (`ens192`, `ens224`, HWADDR) | Debian/Ubuntu/RHEL configs name the vmxnet3 interface; on Exoscale the NIC is `ens3`/`eth0` | Installs `v2v-netfix.service` (first boot, before networking): maps stale names to present NICs in ifupdown/netplan/NetworkManager/ifcfg, drops VMware MAC pins, DHCP fallback |
| **Block Storage volumes have 4096-byte sectors** (the root disk has 512) | Partition tables written for 512-byte sectors are invisible on 4Kn; XFS with `sectsz=512` (the RHEL default on VMware) and ext 1K-block filesystems refuse to mount; Windows shows 512-byte-sector NTFS volumes as RAW | GPT/MBR are rescaled in place (the Windows MSR at LBA 34 is realigned). **NTFS**: the boot sector geometry is rewritten for 4096-byte sectors in place — no data moves (needs ≥ 4 KiB clusters, the default); original boot sector kept for revert. **XFS-512 / ext-1K**: rebuilt onto a fresh 4K volume with the **same UUIDs, labels and partition GUIDs** (`xfsdump`/`rsync`), so `fstab` keeps working; the original volume is kept, detached, as rollback copy (adds cutover time proportional to the data). |
| SELinux guests | injected files need labels | labelled at injection; no relabel reboot |
| Windows: data disks come up **Offline** | Windows Server's SAN policy (`OfflineShared`) keeps "new" disks offline — after the move to virtio every data volume is new | A first-boot script (in virt-v2v's own firstboot framework) registers a SYSTEM task that sets the policy to `OnlineAll` and brings the disks online, retrying until the storage services answer, then removes itself |

## Validation

Live migrations from ESXi 7.0 U3 to ch-gva-2 (VM running during replication):

| Guests | Result | Downtime |
|---|---|---|
| Ubuntu 24.04, AlmaLinux 9, Debian 13, BIOS and UEFI (6 migrations, run concurrently from one appliance) | 6/6 | 120–347 s |
| Windows Server 2022, BIOS and UEFI, NTFS data volume | 2/2 | 420–471 s |
| Ubuntu 24.04 and Windows Server 2022, UEFI with Secure Boot (Secure Boot on after migration) | 2/2 | 133–455 s |
| With receiver templates: the 6 Linux guests above, Windows Server 2022 and 2025 (BIOS and UEFI), up to 8 concurrent | 10/10 | 136–513 s |
| Debian 13 under continuous write load (3 delta cycles) | pass | 298 s |

![Downtime at cutover per guest and firmware](docs/diagrams/downtime.svg)

## Limits

- 1 root disk + up to 5 data volumes per instance; root disk ≥ 10 GB.
- One public NIC; extra VMware NICs → attach Exoscale Private Networks afterwards.
- Data volumes holding XFS-512/ext-1K filesystems inside **LVM** are reported
  but not rebuilt automatically (move them to the root disk, or recreate the LV).
- Windows data volumes must use NTFS clusters ≥ 4 KiB (the default); smaller
  clusters cannot be expressed in 4096-byte sectors.
- The receiver keeps the replica in its RAM-booted agent's state: if it reboots
  during the sync window it must be recreated (`cleanup --delete-instance`, `create`)
  and the next sync is a full copy.

The complete list is in [Limitations](docs/limitations.md). See the
[operations runbook](docs/OPERATIONS.md) for troubleshooting.

## License

Apache License 2.0, the license of the [Exoscale CLI](https://github.com/exoscale/cli). See [LICENSE](LICENSE).
