# Changelog

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
