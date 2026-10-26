# Project status

> **Experimental.** exo-v2v 0.5.0 is an early release, validated in one lab (ESXi 7.0 U3 to Exoscale ch-gva-2). Try it on non-critical VMs first; commands and behaviour may change.

exo-v2v is labelled **experimental**: it works end to end in our lab, but it
has not yet moved anyone's production. This page says what was tested, what
was not, and what it takes to drop the label.

## Tested

| Area | How |
|---|---|
| Source | Standalone ESXi 7.0 Update 3, VMFS datastore on HDDs, one replication appliance (4 vCPU) |
| Target | Exoscale ch-gva-2, receiver templates and stock templates |
| Guests | Ubuntu 24.04, Debian 13, AlmaLinux 9, Windows Server 2022 and 2025, BIOS and UEFI ([details](supported-os.md)); LVM across disks, VMDK snapshot chains, 512-byte-sector XFS, NTFS data volumes |
| Single VMs | Plan, replication while the VM runs, cutover, rollback, cleanup; disks that grow or appear during replication |
| Groups | A 5-VM application (load balancer, two application servers, PostgreSQL, Windows) cut over, rolled back, killed and resumed; private networks with the VMs' addresses; 10 VMs on 18 disks (55 GB) replicated by one appliance and cut over in one 803-second window, with a rehearsal of 8 of them |
| Rehearsals | Airgapped copies of the 5-VM application and of single VMs, followed by real cutovers |
| Safety | Target instances restarted mid-replication (BIOS, UEFI, Windows): nothing boots, `recover` copies the system disk only; targets created by 0.4.0 upgraded in place |
| vCenter | Inventory paths, duplicate names, folders, tags, the privilege check and DRS settings, against govmomi's vCenter simulator (in CI) |
| Unattended | systemd timers, webhook alerts for failing and lagging replication, a target that went down, cutovers |
| CI | Unit tests on Python 3.10, 3.12 and 3.13; the receiver's disk handling on loop devices; the simulator tests; a strict docs build |

## Not tested yet

- A real vCenter: vMotion, Storage vMotion and DRS moves during a replication,
  privilege enforcement, vSAN and vVols datastores.
- ESXi 8 (our lab's CPU cannot run it), zones other than ch-gva-2.
- Disks of several terabytes, and more than ten VMs in one group.
- Encrypted disks, Windows dynamic disks and Storage Spaces on data disks (not
  supported, see [Limitations](limitations.md)).

## Dropping "experimental"

The label goes when all of these hold:

1. A full run on a real vCenter cluster (DRS, shared datastores): plan,
   replication, a rehearsal, a group cutover, a rollback.
2. Migrations outside our lab by at least one team, and their findings fixed.
3. No open issue that can lose or corrupt data, or let a copy run next to its
   source (0.4.1 fixed the last one known: a target instance that restarted
   booted the replica on its disk).
4. CI green on the release.

Found a problem, or ran exo-v2v somewhere new? Open an issue on
[GitHub](https://github.com/retrack/exo-v2v/issues).
