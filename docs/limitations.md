# Limitations

> **Experimental.** exo-v2v 0.2.0 is an early release, validated in one lab (ESXi 7.0 U3 to Exoscale ch-gva-2). Try it on non-critical VMs first; commands and behaviour may change.

Known limits of exo-v2v 0.2.0. `exo-v2v plan` reports the ones it can detect
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
| One network interface | The instance gets one public interface. Recreate other networks as Exoscale Private Networks after the cutover. |
| Static IP addresses | The guest keeps its old static configuration. Switch it to DHCP before the cutover, or update it on the instance. |
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
| Copy speed | Limited by the datastore and the appliance's CPU. In our lab (HDD datastore, 2 vCPU appliance) a full copy ran at about 30 MiB/s per VM. Later cycles copy only changes. |
| Snapshot per cycle | Each cycle creates and removes a snapshot, which briefly pauses the VM, as backups do |
| Target instance restarts during replication | The target instance runs its agent from memory. If it restarts before the cutover, it boots a partly copied disk: run `cleanup --delete-instance`, then `create` and `sync` again (a full copy). |
| Instance type during replication | The target instance runs as `standard.large` or larger until the cutover, then is scaled to its final type |
| Datastore types | Tested on VMFS. vSAN and vVols are untested. |

## Cutover and after

| Limit | Details |
|---|---|
| Downtime | Shutdown, last delta, conversion (2–5 min) and boot: 2 to 9 minutes in our tests, longer if a volume must be rebuilt |
| Rollback | `exo-v2v rollback` powers the source VM back on. Data written on Exoscale after the cutover is not copied back. |
| Windows first boot | Windows finishes installing drivers for a few minutes after RDP opens and may reboot. Data disks come online when it finishes. |
| Windows licensing | Contact Exoscale before migrating Windows workloads (see [Supported operating systems](supported-os.md#windows-licensing)) |
| Zones | Tested in ch-gva-2. Conversion needs nested virtualization on the instance type. |
