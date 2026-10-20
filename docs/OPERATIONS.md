# exo-v2v operations runbook

> **Experimental.** exo-v2v 0.1.0 is an early release, validated in one lab (ESXi 7.0 U3 to Exoscale ch-gva-2). Try it on non-critical VMs first; commands and behaviour may change.

## 1. Replication appliance (once per vSphere environment)

A small Linux VM on a host that sees the source datastores (one appliance can
serve several migrations; hot-adds are serialized on it):

- Ubuntu 24.04 / Debian 12+, 2 vCPU, 4 GiB RAM, 20 GB disk, **paravirtual SCSI**
  controller (free units are used for hot-added disks; add a second pvscsi
  controller if you run many migrations in parallel).
- Network: TCP 443 to vCenter/ESXi, TCP 22 to the Exoscale zone(s).
- Software: `python3 -m pip install exo_v2v-*.whl` (or pipx), the `exo` CLI
  configured with an API key limited to the **compute** service.
- Run `sync`/`cutover` as root (`sudo -E`, so `VSPHERE_*` / `EXOSCALE_*` pass).

vSphere permissions for the migration account: on the source VMs
`VirtualMachine.State.CreateSnapshot/RemoveSnapshot`, `VirtualMachine.Config.ChangeTracking`,
`VirtualMachine.Interact.PowerOff/PowerOn` (cutover/rollback), `VirtualMachine.Provisioning.DiskRandomRead`;
on the appliance VM `VirtualMachine.Config.AddExistingDisk/RemoveDisk`; `Datastore.FileManagement` (read).

## 2. A migration, end to end

| Step | Command | Duration | Source VM |
|---|---|---|---|
| Plan | `exo-v2v plan NAME --vm VM --zone Z [--instance-type T] [--security-group SG] [--receiver-template TPL]` | seconds | untouched |
| Receiver | `exo-v2v create NAME` | ~1 min with a [receiver template](receiver-templates.md), 5–15 min without | untouched |
| Initial copy | `sudo -E exo-v2v sync NAME` | allocated data / throughput | running (snapshot during the cycle) |
| Keep fresh | `sudo -E exo-v2v sync NAME --every 900` | per cycle: changes since last cycle | running |
| Cutover | `sudo -E exo-v2v cutover NAME` | final delta + convert (2–5 min) + relayout (if any) + boot | **shut down** |
| Validate | test the application on the instance | | off |
| Finish | `exo-v2v cleanup NAME [--delete-rollback-volumes]` | | off (keep it until you are sure) |

Downtime ≈ shutdown + last delta + conversion + boot. Run a sync cycle shortly
before the cutover so that the final delta is small.

`exo-v2v status [-v] [NAME]` shows the phase, the last cycle and its age.

## 3. Rollback

Before cleanup the source VM is intact (only its CBT setting was enabled):
`exo-v2v rollback NAME` powers it back on and stops the Exoscale instance.
After a cutover, data written on Exoscale is **not** copied back.

## 4. Troubleshooting

| Symptom | Cause / action |
|---|---|
| `create` times out | Console of the instance in the portal; `/var/log/v2v-bootstrap.log` on it (apt mirror reachable? 2+ disks attached?). `cleanup --delete-instance` and `create` again. |
| `receiver is not in its RAM root any more` | The receiver rebooted during the sync window and now runs a half-written disk. `cleanup --delete-instance`, `create`, then `sync` (full). |
| `Change tracking is not active on disk …` in logs | Per-disk CBT was off; that disk is copied whole once, CBT is enabled automatically for the next cycle. |
| `hot-added … never appeared as a block device` | The appliance has no free SCSI unit or a non-paravirtual controller only; add a pvscsi controller to the appliance. |
| `Another task is already in progress` | Handled (waits/retries). If persistent: a snapshot consolidation is running on the source; wait. |
| Cutover: `conversion failed` | `exo-v2v status -v NAME`; the receiver's `/var/log/v2v-convert.log` (via `ssh -i ~/.exo-v2v/NAME/id_ed25519 USER@IP`: `v2v` on a receiver template, `debian` or `ubuntu` (UEFI) on a stock one). The source stays off: fix and re-run `cutover`, or `rollback`. |
| Guest has no network after cutover | `/var/log/v2v-netfix.log` on the guest (portal console). Static IP configs need the new Exoscale address. |
| Data volume not mounted after cutover | `journalctl -b -u <mount>`; a 512-only filesystem that was not relaid (inside LVM). See README *Limits*. |
| Windows: D: (data) missing after cutover | First boot runs several driver-install reboots (5–10 min) before the `exo-v2v-online-disks` scheduled task onlines the disks; check `C:\ProgramData\exo-v2v\online-disks.log` and `C:\Program Files\Guestfs\Firstboot\log.txt`. Manually: `Set-Disk -Number 1 -IsOffline $false`. |
| Windows volume shows RAW | It was formatted with < 4 KiB clusters (cannot be expressed in 4 KiB sectors); move the data or reformat with 4 KiB clusters before migrating. |

## 5. Clean-up of Exoscale resources

Everything exo-v2v creates carries the label `exo-v2v=<NAME>`: the instance,
its volumes, `<volume>-4k` relayout volumes and the security group
`exo-v2v-<NAME>`. Rollback copies of relaid volumes additionally carry
`exo-v2v-rollback=<export>`.
