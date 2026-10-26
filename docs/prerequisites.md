# Prerequisites

> **Experimental.** exo-v2v 0.4.0 is an early release, validated in one lab (ESXi 7.0 U3 to Exoscale ch-gva-2). Try it on non-critical VMs first; commands and behaviour may change.

Check these before your first migration. `exo-v2v plan` checks the source VM
for you and refuses VMs it cannot migrate.

## vSphere

| Item | Requirement |
|---|---|
| Version | vCenter Server or standalone ESXi, 6.7 or later. Tested on ESXi 7.0 Update 3. |
| Account | A vSphere account with the privileges below, passed to exo-v2v as `VSPHERE_HOST`, `VSPHERE_USER` and `VSPHERE_PASSWORD` |
| Datastore space | Room for a short-lived snapshot of each VM during each sync cycle (the changes written while a cycle runs) |

Privileges for the migration account:

| On | Privileges |
|---|---|
| Source VMs | `VirtualMachine.State.CreateSnapshot`, `VirtualMachine.State.RemoveSnapshot`, `VirtualMachine.Config.ChangeTracking`, `VirtualMachine.Interact.PowerOff`, `VirtualMachine.Interact.PowerOn`, `VirtualMachine.Provisioning.DiskRandomRead` |
| Replication appliance VM | `VirtualMachine.Config.AddExistingDisk`, `VirtualMachine.Config.RemoveDisk` |
| Datastores of the source VMs | `Datastore.FileManagement` (read) |

## Replication appliance

The appliance is a Linux VM that runs exo-v2v. It must run **inside the same
vSphere environment** as the VMs you migrate, on a host that can see their
datastores: exo-v2v attaches the frozen snapshot disks to it read-only. One
appliance can serve many migrations.

| Item | Requirement |
|---|---|
| Operating system | Ubuntu 24.04 or Debian 12 or later (any Linux with Python 3.10+ works) |
| Size | 2 vCPU, 4 GiB RAM, 20 GB disk. Copy throughput is limited by the appliance's CPU; give it 4 vCPU for several migrations in parallel. |
| SCSI controller | A **VMware Paravirtual (pvscsi)** controller with free slots, used to attach the snapshot disks. Add a second one if you run many migrations at once. |
| Software | Python 3.10 or later, `ssh`, the [`exo` CLI](https://github.com/exoscale/cli) 1.102 or later, and exo-v2v (`pip install exo_v2v-*.whl`) |
| Privileges | `sync`, `cutover` and `rollback` read block devices and must run as root (`sudo -E` keeps the environment variables) |

Network access from the appliance:

| Destination | Port | Why |
|---|---|---|
| vCenter or ESXi | TCP 443 | vSphere API |
| Exoscale API (`api-<zone>.exoscale.com`) | TCP 443 | Create and manage the target instance |
| Target instances in the Exoscale zone | TCP 22 | Data stream and agent control, inside SSH |
| `api.ipify.org` or `ifconfig.me` | TCP 443 | Detects the appliance's public address once per migration. Not needed if you pass `create --sender-cidr`. |
| A rehearsal's relay (rehearsals only) | TCP 22 | The appliance drives the copies through it; the production receivers stream the root disks through it |
| Group members' public IPs (groups only) | the health check ports | `group cutover` checks each tier from the appliance; the member's own security group opens those ports to the appliance only |

The appliance's public address must stay the same during a migration: the
migration's security group admits SSH from that address only.

The [web status page](web.md) listens on the appliance's loopback interface by
default; reach it through an SSH port forward.

## Exoscale

| Item | Requirement |
|---|---|
| Account | An organization with quota for the instance types you target, Block Storage volumes and one security group per running migration; for a group, one private network per private port group and every member's receiver at once |
| API key | A key restricted to the Compute service, configured in the `exo` CLI (`exo config`) or in `EXOSCALE_API_KEY` and `EXOSCALE_API_SECRET`. See [IAM role](iam.md) for a ready-made role. |
| Instance type during migration | Conversion needs at least 8 GiB of RAM and nested virtualization, so the target instance runs as `standard.large` (or larger) until cutover, then is scaled to its final type. Tested in ch-gva-2. |
| Internet access from the target instance | Not needed with a [receiver template](receiver-templates.md). Without one, the target instance installs packages from Debian or Ubuntu mirrors, and for Windows guests downloads the virtio drivers from `fedorapeople.org`. |

## Source VM

| Item | Requirement |
|---|---|
| Operating system | See [Supported operating systems](supported-os.md) |
| Disks | At most 6 virtual disks (1 root disk and up to 5 Block Storage volumes). No independent disks, no raw device mappings (RDM). |
| Firmware | BIOS or UEFI. The target instance uses the same boot mode, and Secure Boot and a TPM when the source has them. |
| Changed Block Tracking | Enabled automatically by exo-v2v, without downtime |
| VMware Tools | Recommended, running when you run `plan`: it tells exo-v2v which disk holds the system and which disks share a filesystem with it. At cutover it shuts the guest down cleanly; without it, exo-v2v powers the VM off after `--shutdown-timeout`. |
| Network configuration | For a single migration, DHCP is recommended: a static IP configuration keeps the old address and must be updated for the Exoscale instance's address. In a [group](groups.md), static addresses on private port groups are kept on Exoscale private networks. |
| Snapshots | Existing snapshots are allowed but consolidate them first if you can |
