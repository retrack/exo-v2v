# Limitations

> **Experimental.** exo-v2v 0.6.0 is an early release, validated in one lab (ESXi 7.0 U3 to Exoscale ch-gva-2). Try it on non-critical VMs first; commands and behaviour may change.

Known limits of exo-v2v 0.6.0. `exo-v2v plan` reports the ones it can detect
before anything is created.

## Source VM

| Limit | Details |
|---|---|
| At most 5 data disks as volumes | An Exoscale instance attaches up to 5 Block Storage volumes next to its root disk. Disks carried inside the root disk (`--fold`, automatic for a disk that shares a filesystem with the system disk) do not count. A volume rebuild at cutover needs a free slot per rebuilt volume. |
| System disk | Found from the boot order or VMware Tools; `plan` warns when it had to guess (run it with the VM on and Tools running, or pass `--system-disk`) |
| Snapshot chains | Supported. A revert to a snapshot during replication makes the next cycle a full copy of the allocated areas |
| Disk changes during replication | A grown disk is followed (volume resized; a grown system disk resets the target instance, whose root disk is then copied again), a new disk gets a new volume, a removed disk stops `sync` |
| Windows dynamic disks and Storage Spaces | Not supported on data disks: their metadata counts 512-byte sectors. The disks are detected and left untouched; their volumes stay unreadable on Exoscale. Convert them to basic disks first (a spanned or striped volume must be copied to a basic volume). `plan` warns when VMware Tools show a volume spread over several disks. |
| No independent disks or RDMs | Independent disks are left out of snapshots, so changes cannot be tracked. Raw device mappings are not VMDK files. |
| Root disk of 10 GB or more | Smaller disks are rounded up to 10 GB |
| Disk sizes rounded up | Each disk becomes the next whole GB; volumes get at least 1 MiB extra |
| Network interfaces | A single migration gets one public interface (DHCP). In a [group](groups.md), each port group maps to the public interface or a managed private network, and the guest's configuration follows. |
| Static IP addresses | In a group, a static address on a private port group is kept on the Exoscale private network. Otherwise the guest keeps its old static configuration: switch it to DHCP before the cutover, or update it on the instance. |
| Secure Boot and vTPM | Carried over: the instance is created with Secure Boot and a TPM when the source has them (Exoscale sets both at creation). Secrets sealed to the VMware vTPM do not move. Secure Boot is tested; the TPM is not. A guest that only boots because Secure Boot is off at the source cannot gain it later: Exoscale cannot turn it on after creation. |
| Encrypted disks and encrypted VMs | Not supported (LUKS, BitLocker, vSphere VM encryption) |

## Block Storage sector size

Exoscale Block Storage volumes use 4096-byte sectors; VMware disks use 512.
exo-v2v adapts data volumes at cutover, with these limits:

| Case | Behaviour |
|---|---|
| Partition tables (GPT, MBR) | Rewritten in place. Partitions must start on a 4 KiB boundary, which is the default for every current installer. |
| NTFS (Windows) | Adapted in place, no data moved. The volume must use clusters of 4 KiB or more (the default). |
| XFS created with 512-byte sectors (RHEL-family default on VMware), ext with 1 KiB blocks | Rebuilt on a new volume with the same identifiers. The cutover takes longer in proportion to the data on these volumes. The original volume is kept, detached, as a rollback copy until `cleanup --delete-rollback-volumes`. |
| The same filesystems inside LVM logical volumes | Rebuilt the same way, logical volume by logical volume. A volume group spread over several volumes is rebuilt across as many new volumes, keeping the PV, VG and LV identifiers. Linear and striped logical volumes only: thin, RAID, mirror, cache and snapshot logical volumes with such a filesystem are reported, not rebuilt. |
| A volume group that spans the system disk and other disks | Those disks are carried inside the root disk (512-byte sectors), so nothing is rebuilt. Detected by `plan` through VMware Tools; otherwise the preflight at the first sync reports it: plan again with `--fold DISK`. |
| Root disk | Not affected: the instance's root disk uses 512-byte sectors |

## Replication

| Limit | Details |
|---|---|
| The appliance must run in the source vSphere | It reads snapshot disks by attaching them to itself, so it must be on a host that sees the VM's datastores. |
| Copy speed | Limited by the datastore and the appliance's CPU. In our lab (HDD datastore) a full copy ran at 7–38 MiB/s per VM, about 60 MiB/s for a 4 vCPU appliance copying 5 VMs at once ([sizing](OPERATIONS.md#1-replication-appliance-once-per-vsphere-environment)). Later cycles copy only changes. |
| Snapshot per cycle | Each cycle creates and removes a snapshot, which briefly pauses the VM, as backups do |
| Target instance restarts during replication | The target instance runs its agent from memory and keeps the boot sectors of the system disk off the disk until the conversion: if it restarts, nothing boots. Run `exo-v2v recover NAME`: the next sync copies the system disk again, and the volumes only for their changes. |
| Egress of the target instance | None until the cutover (receiver templates): its security group gets a rule that matches nothing. The cutover removes it before it stops the source; the IAM role needs `delete-rule-from-security-group` (0.4.1). |
| Instance type during replication | The target instance runs as `standard.large` or larger until the cutover, then is scaled to its final type |
| Datastore types | Tested on VMFS. vSAN and vVols are untested. |
| vCenter | Tested against govmomi's vCenter simulator only: vMotion, Storage vMotion and DRS moves during a replication, and privilege enforcement, are untested on a real vCenter ([vCenter](vcenter.md)). The appliance's host must mount every datastore of the VMs it replicates |

## Cutover and after

| Limit | Details |
|---|---|
| Downtime | Shutdown, last delta, conversion (2–5 min) and boot: 2 to 9 minutes in our tests, longer if a volume must be rebuilt |
| Rollback | `exo-v2v rollback` powers the source VM back on. Data written on Exoscale after the cutover is not copied back. |
| Windows first boot | Windows finishes installing drivers for a few minutes after RDP opens and may reboot. Data disks come online when it finishes. |
| Windows licensing | Contact Exoscale before migrating Windows workloads (see [Supported operating systems](supported-os.md#windows-licensing)) |
| Zones | Tested in ch-gva-2. Conversion needs nested virtualization on the instance type. |

## Groups

| Limit | Details |
|---|---|
| One appliance per group | It replicates every member, so it must see all their datastores |
| Selecting members | `group discover --vm` (name or inventory path), `--folder`, `--tag` (vCenter): see [vCenter](vcenter.md) |
| One zone | Every member of a group goes to the same zone |
| Private networks | Managed private networks only (no VPC subnets). The top host address of the subnet (`.254` of a /24) belongs to Exoscale's DHCP server: a VM using it must be renumbered. The appliance role only attaches `v2v-*` private networks |
| Default route | Moves to the public interface: a private network has no router. Routes to other networks through the old gateway are lost |
| DNS servers | Static DNS servers in the guests' configurations are kept; change on-premises ones before or during the cutover (`post_cutover` hook) |
| Elastic IPs | Manual Elastic IPs ([Public IPs and DNS](public.md)); managed (health-checked) Elastic IPs are not handled |
| DNS | Only A records of Exoscale DNS zones are switched; other providers: the `post_cutover` hook |
| Health checks | Run from the appliance against the members' public IPs (the ports are opened to the appliance only) |
| Rollback after conversion | Converted members must be created again before another attempt (a full copy) |

## Rehearsal

| Limit | Details |
|---|---|
| Receiver templates | A rehearsal needs receiver templates built with exo-v2v 0.4 or later (copies without a public IP read their metadata from a drive, not from the metadata server) |
| Consistency | Each copy is crash-consistent at its own seed time. `group rehearse --consistent` puts every member at one point (snapshots within 13 s for 5 VMs in our lab, 44 s quiesced); vSphere has no snapshot across VMs, so "one point" is within the spread the report shows |
| What it cannot test | The public side (public IP, DNS switch, security groups on the public interface) and anything that needs the internet or on-premises systems: those fail by design; the report lists the names the copies tried to resolve |
| Windows | Activation and domain servers are unreachable unless they are members of the group |
| Metadata drive | The copies see a small extra disk (1 MiB) as their second disk: mount data disks by UUID or label, not by device name |
| Windows copies | Crash-consistent NTFS: the copy's system volume journal is reset before conversion and checked by Windows at first boot |
| Cost | One instance per copy plus the relay, and the copies' volumes, for the rehearsal's lifetime (TTL 8 h by default with `exo-v2v gc`) |

