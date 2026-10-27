# Changelog

## Unreleased

- **Trademarks and licences**: a `NOTICE` file (shipped in the wheel too), a
  [docs page](docs/legal.md), the docs site footer and the README say that the
  VMware product names are trademarks of Broadcom, that exo-v2v is not
  affiliated with Broadcom and contains no VMware software, that users need
  their own VMware licences and stay responsible for the licences of the
  systems they migrate, and that exo-v2v comes without warranty.

## 0.5.0 — 2026-10-26

- **vCenter** (tested against govmomi's vcsim; vMotion, Storage vMotion, DRS and
  privilege enforcement need a real vCenter and are untested):
  - A VM is named by name, by inventory path (`DC1/vm/shop/web01`) or by
    instance UUID (`uuid:...`); a name that several VMs share is refused with
    their paths, templates never match. A migration finds its VM by instance
    UUID afterwards, so renaming or moving it in vCenter is harmless. The name
    lookup is one PropertyCollector query instead of one call per VM.
  - `group discover --folder DC/vm/shop` (recursive) and `--tag CATEGORY:TAG`
    (vSphere Automation API, `/api` or `/rest`), with `--vm`; the draft
    manifest uses a path when a name is ambiguous.
  - `plan` reports the privileges the vSphere account lacks
    (HasPrivilegeOnEntities) and, run on the appliance, a source datastore its
    host does not mount and a fully automated DRS on the appliance; `sync`
    refuses the first and warns about the second.
  - `VSPHERE_HOST` may carry a port (`vcenter.example.com:8443`).
- **Unattended replication.** `exo-v2v service install NAME | --group G |
  --watch [--every 1h]` writes systemd timers on the appliance (one cycle per
  run, the next `--every` after the previous one ended; `--write-env` keeps the
  credentials in a root-only file); `service list / remove`. A cutover removes
  the timers of what it moves once the sources stop; `cleanup` too.
- **Alerts** to a webhook (`$EXO_V2V_HOME/config.yaml`: `alerts: {webhook, lag,
  failures}`; JSON with a `text` field for Slack, Mattermost or Teams): cycles
  failing N times in a row and back, a migration lagging past its threshold and
  back (`exo-v2v watch`, which also destroys expired rehearsals), a receiver
  found down (once per outage), cutovers done or stopped, a rehearsal destroyed
  at its TTL. `exo-v2v alert test`. `status` and the web page show failed
  cycles, the last error and the lag.
- **Preflight through the boot mask** (regression in 0.4.1): the sync-time
  preflight read the system disk directly, whose boot sectors the mask keeps
  in RAM, so it missed a volume group spanning the system disk and a data disk
  (which needs `--fold`): the group cutover went ahead and the conversion
  failed. It now reads a device-mapper view with the boot sectors put back.
  Rehearsals refuse such members too (`--vm` leaves them out).
- **Receiver agents follow the appliance**: `sync` upgrades a receiver whose
  agent is another version in place, between two cycles (same boot, change IDs
  kept), so fixes like the one above reach existing migrations.
- **Python 3.10 and 3.11** (Debian 12): exo-v2v could not even start since
  0.1.0, because one f-string used syntax only Python 3.12 accepts. Fixed.
- **CI** on GitHub Actions: unit tests on Python 3.10, 3.12 and 3.13, the
  vCenter tests against vcsim, the receiver's disk handling on loop devices
  (GPT/MBR, NTFS, LVM, XFS rebuilds), a strict docs build, the wheel.

## 0.4.1 — 2026-10-26

Safety fix: a target instance that restarted during the replication booted the
copy of the source VM on its disk.

- **The problem (every release up to 0.4.0).** The target instance runs its agent
  from RAM while its disks receive the replica. If it restarted before the
  cutover (by hand, or on the platform's side), it booted that replica:
  unconverted, on the instance's public IP, next to the running source. In our
  lab an Ubuntu replica came up in 8 seconds with a default route and internet
  access; a copy of a production server would start its services, and could
  write to its data volumes without VMware's change tracking seeing it (later
  delta cycles would then miss those blocks). Windows replicas do not boot
  (no virtio drivers before the conversion).
- **Boot mask.** The agent keeps the first and the last MiB of the system disk
  (MBR or GPT, boot loader, backup GPT) off the disk until the conversion; the
  disk holds zeros there, so a restarted instance boots nothing. Reads, `cutover
  --verify` and the rehearsal's root copy go through the mask; the conversion
  writes the boot sectors back first. Tested on BIOS and UEFI Linux and on UEFI
  Windows: restarted, nothing answered for five minutes; recovered; rehearsed;
  cut over.
- **No egress before the cutover** (receiver templates). The migration's
  security group gets an egress rule that matches nothing, so the receiver can
  send nothing (the appliance's SSH is inbound). The cutover removes it before
  it stops the source; a rehearsal allows SSH from the production receivers to
  its relay for the seed and closes it on `destroy`.
- **`exo-v2v recover NAME`.** `sync` now tells a receiver that stopped answering
  (it restarted), was stopped, or booted its disk (a receiver created before
  0.4.1: `sync` stops it) from a network problem. `recover` resets the root disk
  to the receiver template and restarts the agent; the next cycle copies the
  system disk again and the volumes only for their changes (in full when the
  replica may have booted, or with `--full`).
- **Change IDs kept on the appliance** after every cycle: an agent that restarts
  in the same boot gets them back instead of forcing a full cycle.
- Receivers created by 0.4.0 or earlier get the new agent in place at their next
  `sync` (same boot, change IDs kept, mask on from then) and their egress
  closed.
- IAM role: `delete-rule-from-security-group`. Update an existing role with the
  new `docs/iam/exo-v2v-appliance-policy.json`; without it the cutover stops
  before anything is stopped and says so.
- Groups whose members only use the public interface (no private network)
  could not cut over since 0.3.0: their NICs were never recorded, so the group
  preflight and the conversion refused them. Fixed, also for receivers that
  already exist.
- `exo` errors show the CLI's error line, not its progress output.

## 0.4.0 — 2026-10-26

Experimental release: rehearsals.

- **Rehearsal (`exo-v2v group rehearse`, `exo-v2v rehearse`).** Copies of the
  VMs, seeded from their replicas while the sources keep running, converted with
  the cutover's own code and started in the cutover's tier order with the
  health checks, for people to check the application before the real move.
  The copies are **airgapped**: created without a public IP (Exoscale sets it at
  creation), on isolated copies of the private networks plus a front network
  standing in for the public interface, with no router. They keep the
  production addresses without meeting production. People reach them only
  through a **relay**: SSH from the appliance and the addresses listed in the
  manifest, keys only, forwarding only to the copies, no routing, no NAT. Its
  DNS answers the names you list, refuses the rest at once and logs every
  query: the report lists the outside names the application tried to reach.
  An airgap check through the API runs before any copy boots its converted
  system. Seeding takes Block Storage snapshots of the replicas' volumes and
  streams each replica's root disk from its production receiver through the
  relay (zero blocks skipped), the migration's lock held for minutes; the next
  sync is an ordinary delta. `rehearsal status / connect (ssh config, port
  forwards) / report (times, predicted cutover window, DNS log) / destroy`,
  a TTL (8 h by default) and `exo-v2v gc` for cron. Resumable. The web page
  shows rehearsals. Windows copies (crash-consistent NTFS) get their system
  journal reset before conversion and a `chkdsk` at first boot. `cutover` refuses while a rehearsal of the same VMs is up
  (`--allow-rehearsal`).
- **Receiver templates read the NoCloud drive.** An instance without a public
  IP cannot reach the metadata server; Exoscale gives it a read-only NoCloud
  drive instead. `v2v-userdata-key` reads either; the agent never exports that
  drive and finds the root disk as the first disk without a serial. Rebuild
  your receiver templates to rehearse.
- SSH prefers AES-GCM (faster with AES-NI); `Tunnel` can go through a relay.
- IAM role: `create-block-storage-snapshot` (matched on the snapshot's name)
  and `delete-block-storage-snapshot`. Update an existing role with the new
  `docs/iam/exo-v2v-appliance-policy.json`.

## 0.3.0 — 2026-10-23

Experimental release: applications moved as a whole, private networks, a
read-only status page.

- **Groups (`exo-v2v group`).** Several VMs, for example a load balancer,
  application servers and databases, replicated together and cut over in one
  window. A YAML manifest (`group discover` drafts it from vSphere) sets the
  tiers, the network mapping, security groups, anti-affinity groups, health
  checks and hooks. The cutover stops the tiers front to back, starts each
  member's final copy and conversion as soon as it is off, converts all members
  in parallel, then starts the tiers back to front with health checks (TCP,
  HTTP, command) between them. `pre_cutover` / `post_cutover` hooks,
  `max_window`, `rollback_on_failure`. Resumable: the group and each member
  record their steps; members run as child processes that survive the group
  process. `group rollback` stops the Exoscale instances and powers the sources
  on, back end first; members not converted yet keep syncing. `group cleanup`
  renames instances to their final names.
- **Private networks with the VMs' addresses.** A vSphere port group maps to
  the public interface or an Exoscale managed private network. `create`
  attaches each private network with the VM's address as a static lease and
  records the NIC MACs; the conversion passes a map by MAC to the guest's
  first-boot network fix, which moves each static configuration to its new NIC
  (ifupdown, netplan, NetworkManager, ifcfg; `eth0` → `eth1` swaps included),
  drops its gateway (the default route goes through the public interface), and
  writes DHCP configurations for mapped NICs left without one. Windows: a
  first-boot task sets the static addresses on the adapters by MAC.
- **`exo-v2v web`**: a read-only status page (and JSON API) of every migration
  and group: phases, cycles, cutover steps, downtime, member logs. GET only, no
  credentials, no external assets; loopback by default, a token otherwise.
- **Resumable single cutover.** `cutover` records its steps (source off, final
  cycle, conversion, rebuilds, flush, restart) and continues after an
  interruption without converting twice; `cutover --no-start` stops after the
  conversion. `rollback` before the conversion keeps the receiver, and `sync`
  continues.
- `cleanup --rename NEW`; the VM inventory records port groups, guest
  addresses and default gateways.
- IAM role: private networks, anti-affinity groups, `update-instance` (rename)
  and `v2v-*` security groups. Update an existing role with the new
  `docs/iam/exo-v2v-appliance-policy.json`.
- New dependency: PyYAML.

## 0.2.0 — 2026-10-21

Experimental release: multi-disk VMs, LVM and VMDK chains.

- **System disk detection.** `plan` takes the system disk from the VM's boot
  order, then from VMware Tools (the disk holding `/boot` or `C:\`), then from
  `bios.hddOrder`, and warns when it has to guess. 0.1.0 took the first disk in
  alphabetical order: a VM with an NVMe, SATA or IDE disk next to its SCSI
  system disk got the wrong root disk.
- **Disks carried inside the root disk (`--fold`).** A disk that shares a
  filesystem with the system disk (a root volume group extended onto another
  VMDK) is replicated into the root disk and added as a partition at cutover,
  so its filesystem keeps 512-byte sectors. Automatic for Linux guests when
  VMware Tools show the shared filesystem; `--fold DISK` forces it, `--no-fold`
  turns it off. Folded disks do not count against the 5 Block Storage volumes.
- **LVM on Block Storage.** The receiver reads LVM metadata itself (no LVM
  activation on the receiver). Logical volumes holding XFS with 512-byte
  sectors or ext with 1 KiB blocks are rebuilt with 4 KiB sectors at cutover,
  across as many new volumes as the volume group spans, keeping PV, VG, LV and
  filesystem identifiers. Linear and striped logical volumes.
- **VMDK chains.** VMs with snapshots replicate through their delta chain.
  A snapshot revert during replication (CBT reset) makes the next cycle a full
  copy of the allocated areas instead of a whole-disk copy.
- **Disk changes during replication.** A grown data disk gets its volume
  resized (detach, resize, attach); a grown system or folded disk resets the
  target instance with a bigger root disk, keeping its volumes and their change
  IDs (resumable if interrupted); a new disk gets a new volume; a removed disk
  stops `sync`.
- The receiver maps volumes by serial, not by device order.
- Windows dynamic disks and Storage Spaces disks are detected, left untouched
  and reported (their metadata counts 512-byte sectors); `plan` warns when a
  Windows volume spans several disks.
- IAM role: `resize-block-storage-volume` (volume resize) and `reset-instance`
  (receiver reset) added. Update an existing `exo-v2v-appliance` role with the
  new `docs/iam/exo-v2v-appliance-policy.json`.
- A volume left detached by an interrupted resize is attached again by the
  next `sync`.
- Receivers install `dmsetup` (logical volume rebuild); receiver templates
  built with 0.1.0 install it on demand.
- Docs: what exo-v2v handles (capabilities), disk mapping diagram, VMDK chains.

## 0.1.0 — 2026-10-20

First, experimental release.

- `exo-v2v` CLI: plan / create / sync / cutover / status / rollback / cleanup / list,
  resumable per-migration state in `~/.exo-v2v/<name>/`.
- VDDK-free replication from vCenter or standalone ESXi: snapshot +
  QueryChangedDiskAreas + hot-add of the frozen base disk to the replication
  appliance (`independent_nonpersistent`); `http` transport for powered-off VMs.
  CBT enabled VM-wide and per disk automatically; whole-disk fallback when a
  disk's CBT is inactive.
- Receiver: stock Exoscale templates (Debian 13 for BIOS, Ubuntu 24.04 for
  UEFI) booted into a RAM root via kexec; NBD + control API over an SSH tunnel
  with a per-migration key and security group.
- Agent payload streamed over SSH by `create` (user-data carries only the key;
  Exoscale caps user-data at 32 KiB).
- Conversion on the target with `virt-v2v-in-place` (nested KVM); Windows via
  virtio-win + rhsrvany staged on the receiver at `create` (outside the downtime).
- Windows: NTFS volumes with 512-byte sectors rewritten in place for 4Kn volumes
  (boot sector only), MSR partition realigned, data disks onlined at first boot
  (SAN policy `OnlineAll`).
- Exoscale fix-ups: first-boot network re-pointing (`v2v-netfix`), 4Kn Block
  Storage adaptation (GPT/MBR rescale, rebuild of XFS-512/ext-1K volumes onto a
  4K volume with identical UUIDs), cutover scaling to the target instance type.

- Documentation: concepts with diagrams, prerequisites, a least-privilege IAM
  role for the appliance (`docs/iam/`), supported operating systems,
  limitations.
- Volumes are named `v2v-<migration>-d<N>` whatever `--instance-name` is, so
  that the IAM role can scope them by name. The role matches names only:
  Exoscale IAM refused creates under rules on `parameters.labels`.
- A failed instance create no longer leaves the `exo` CLI's temporary SSH key
  (`<instance>-<epoch>`) behind.
- Secure Boot and vTPM carried over: `create` passes `--secureboot` (UEFI
  sources with Secure Boot) and `--tpm` (sources with a vTPM). The vTPM path is
  not tested yet.
- Receiver templates (`plan --receiver-template`): Exoscale templates that boot
  straight into the RAM-root receiver, built by `template/build-image.sh`
  (Linux and Windows flavours; one image boots BIOS and UEFI, with or without
  Secure Boot). `create` takes about a minute instead of 5–15 and the receiver
  needs no mirror access; a BIOS Windows guest keeps BIOS. See
  `docs/receiver-templates.md`.
- Packer recipe `template/receiver.pkr.hcl` (Exoscale Packer plugin
  `exoscale-import`): builds a receiver image and registers its UEFI and
  legacy templates.
- Documentation site (MkDocs Material, `mkdocs.yml`), published on GitHub Pages.
- Windows: data disks are brought online by a SYSTEM scheduled task that the
  first-boot script registers (retried until the storage services answer, then
  removed). Running the commands in virt-v2v's first-boot window failed on
  Windows Server 2025.
- Concurrent migrations on one appliance: hot-add attach and detach are
  serialized and retried when vSphere reports `TaskInProgress`.
- Licensed under the Apache License 2.0.

Validated (ch-gva-2, ESXi 7.0 U3), live migrations through `exo-v2v`:
Ubuntu 24.04, AlmaLinux 9.8, Debian 13, BIOS and UEFI: 6/6, run concurrently,
downtime 120–347 s (REG1); Windows Server 2022 BIOS and UEFI: 2/2, downtime
420–471 s (T4); Debian 13 under continuous write load: downtime 298 s (T5); Ubuntu 24.04 and
Windows Server 2022 from UEFI with Secure Boot: 2/2, Secure Boot on after
migration, downtime 133–455 s. Receiver templates: 6 Linux guests, Windows
Server 2022 and 2025, BIOS and UEFI: 10/10, downtime 136–513 s.
