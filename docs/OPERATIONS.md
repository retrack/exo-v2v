# exo-v2v operations runbook

> **Experimental.** exo-v2v 0.5.0 is an early release, validated in one lab (ESXi 7.0 U3 to Exoscale ch-gva-2). Try it on non-critical VMs first; commands and behaviour may change.

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

Sizing, measured in our lab (VMFS on hard disks, the appliance with 4 vCPU and
8 GiB RAM, a group of 10 VMs with 55 GB of data on 18 disks, 5 in parallel):

| | Measured |
|---|---|
| `group create` (10 targets from receiver templates) | 3 min 26 s |
| First full copy, 5 VMs at a time | 15 min 6 s: 62 MiB/s for the appliance, 7–38 MiB/s per VM (more for large contiguous data) |
| Appliance CPU during the full copy | 40 % on average (plus 14 % I/O wait), peaks at 100 % |
| A delta round of the 10 VMs with no changes | 116 s (snapshot, change query and removal: 10–77 s per VM, longer with more disks) |
| `group cutover` of the 10 VMs (3 tiers) | window 803 s: the sources off in 3 min, 8 VMs converted 5 min later; one VM's three XFS-512 volume rebuilds set the end |

The datastore set the pace here; on faster storage the appliance's CPU does
(the copy reads, compares zeros and encrypts). Plan 2 vCPU for one or two
concurrent copies, 4 vCPU for about five; `group sync` runs `sync.parallel` at
a time (default 4).

vSphere permissions for the migration account: on the source VMs
`VirtualMachine.State.CreateSnapshot/RemoveSnapshot`, `VirtualMachine.Config.ChangeTracking`,
`VirtualMachine.Interact.PowerOff/PowerOn` (cutover/rollback), `VirtualMachine.Provisioning.DiskRandomRead`;
on the appliance VM `VirtualMachine.Config.AddExistingDisk/RemoveDisk`; `Datastore.FileManagement` (read).
On a vCenter, see [vCenter](vcenter.md) for the role, where the appliance runs, and DRS.

## 2. A migration, end to end

| Step | Command | Duration | Source VM |
|---|---|---|---|
| Plan | `exo-v2v plan NAME --vm VM --zone Z [--instance-type T] [--security-group SG] [--receiver-template TPL]` | seconds | untouched |
| Receiver | `exo-v2v create NAME` | ~1 min with a [receiver template](receiver-templates.md), 5–15 min without | untouched |
| Initial copy | `sudo -E exo-v2v sync NAME` | allocated data / throughput | running (snapshot during the cycle) |
| Keep fresh | `sudo -E exo-v2v service install NAME --every 1h` (a timer, below), or `sudo -E exo-v2v sync NAME --every 900` in a terminal | per cycle: changes since last cycle | running |
| Cutover | `sudo -E exo-v2v cutover NAME` | final delta + convert (2–5 min) + relayout (if any) + boot | **shut down** |
| Validate | test the application on the instance | | off |
| Finish | `exo-v2v cleanup NAME [--delete-rollback-volumes]` | | off (keep it until you are sure) |

Downtime ≈ shutdown + last delta + conversion + boot. Run a sync cycle shortly
before the cutover so that the final delta is small.

`exo-v2v status [-v] [NAME]` shows the phase, the last cycle and its age.

### An application (group)

| Step | Command | Source VMs |
|---|---|---|
| Draft | `exo-v2v group discover NAME --vm A --vm B ... --zone Z` (or `--folder DC/vm/FOLDER`, `--tag CATEGORY:TAG` on a [vCenter](vcenter.md)) then edit `NAME.yaml` | untouched |
| Plan | `exo-v2v group plan NAME -f NAME.yaml` | untouched |
| Networks + receivers | `exo-v2v group create NAME` | untouched |
| Copy and keep fresh | `sudo -E exo-v2v group sync NAME [--every 900]` | running |
| Window | `sudo -E exo-v2v group cutover NAME` | **shut down**, front to back |
| Follow | `exo-v2v group status NAME`, the [web page](web.md), `~/.exo-v2v/groups/NAME/logs/VM.log` | |
| Finish | `exo-v2v group cleanup NAME` | off |

Before the window: lower the TTL of the DNS names that will move (the load
balancer's new address is its receiver's IP, shown by `group status`), and
check that `group sync` ran recently (`cutover.max_sync_age`).

### A rehearsal

| Step | Command |
|---|---|
| Start | `sudo -E exo-v2v group rehearse NAME` (or `exo-v2v rehearse MIGRATION --tcp 22`) |
| Look | `exo-v2v rehearsal connect NAME` (SSH config through the relay), `--forward VM:PORT` |
| Report | `exo-v2v rehearsal report NAME`: times, predicted window, names the copies tried to resolve |
| Throw away | `exo-v2v rehearsal destroy NAME` (or `exo-v2v watch`, below, after the TTL) |

### Unattended: timers and alerts

Replication runs for days; the appliance can run it without a terminal and
call for a person when something needs one.

```bash
sudo -E exo-v2v service install web01 --every 1h --write-env   # a cycle every hour (systemd timer)
sudo -E exo-v2v service install --group shop --every 1h
sudo -E exo-v2v service install --watch                          # every 15 min: lag alerts, expired rehearsals
exo-v2v service list
sudo exo-v2v service remove web01
```

A timer starts the next cycle `--every` after the previous one ended, so cycles
never overlap. The units run as root with the variables of
`/etc/exo-v2v/exo-v2v.env` (`--write-env` writes it, mode 600, from the current
`VSPHERE_*` and `EXOSCALE_*`); the `exo` CLI found in your `PATH` at install time
is the one they use. A cutover removes the timers of what it moves once the
sources stop; `cleanup` too.

Alerts go to a webhook, set in `$EXO_V2V_HOME/config.yaml` (`/var/lib/exo-v2v`
or `~/.exo-v2v`):

```yaml
alerts:
  webhook: https://hooks.slack.com/services/T000/B000/XXXX   # Slack, Mattermost, Teams: JSON with "text"
  lag: 4h          # a syncing migration whose last good cycle is older
  failures: 2      # failed cycles in a row
```

| Event | When |
|---|---|
| `sync-failing` / `sync-recovered` | `failures` cycles failed in a row (with the last error) / the next good cycle |
| `lag` / `lag-recovered` | `exo-v2v watch` finds a migration whose last good cycle is older than `lag` / it caught up |
| `receiver-down` | `sync` finds the receiver stopped, restarted or booted from its disk (once per outage) |
| `cutover-done` / `cutover-failed` | a cutover or group cutover finished / stopped |
| `rehearsal-expired` | `watch` destroyed a rehearsal at the end of its TTL |

`exo-v2v alert test` sends a test message. Without a webhook, alerts are only
logged. `status` and the [web page](web.md) show failed cycles, the last error
and a lag over the threshold.

## 3. Rollback

Before cleanup the source VM is intact (only its CBT setting was enabled):
`exo-v2v rollback NAME` powers it back on and stops the Exoscale instance.
After a cutover, data written on Exoscale is **not** copied back.

For a group, `exo-v2v group rollback NAME` stops the Exoscale instances (front first) and powers
the sources on, back end first. Members that were not converted yet keep syncing.

## 4. Troubleshooting

| Symptom | Cause / action |
|---|---|
| `create` times out | Console of the instance in the portal; `/var/log/v2v-bootstrap.log` on it (apt mirror reachable? 2+ disks attached?). `cleanup --delete-instance` and `create` again. |
| `receiver … is running but does not answer on SSH` | The receiver restarted; with the boot mask (0.4.1 and later) nothing boots. `exo-v2v recover NAME`, then `sync`: the system disk is copied again, the volumes only for their changes. |
| `receiver … restarted and booted the copy of the source VM on its disk` | A receiver created before 0.4.1 (no boot mask) restarted and booted its replica; `sync` stopped it. `exo-v2v recover NAME`, then `sync` (the volumes are copied in full: the copy may have written to them). |
| `receiver … is stopped` | Someone stopped it. `exo-v2v recover NAME` (do not just start it: an instance created before 0.4.1 would boot its replica). |
| `receiver is not in its RAM root any more` | The receiver booted a disk instead of its RAM root. `exo-v2v recover NAME`. |
| `cannot reopen the egress of …` at cutover | The appliance's IAM role lacks `delete-rule-from-security-group` (added in 0.4.1): update it (`exo iam role update exo-v2v-appliance --policy - < docs/iam/exo-v2v-appliance-policy.json`) and run `cutover` again. Nothing was stopped. |
| `Change tracking is not active on disk …` in logs | Per-disk CBT was off; that disk is copied whole once, CBT is enabled automatically for the next cycle. |
| `hot-added … never appeared as a block device` | The appliance has no free SCSI unit or a non-paravirtual controller only; add a pvscsi controller to the appliance. |
| `Another task is already in progress` | Handled (waits/retries). If persistent: a snapshot consolidation is running on the source; wait. |
| Cutover: `conversion failed` | `exo-v2v status -v NAME`; the receiver's `/var/log/v2v-convert.log` (via `ssh -i ~/.exo-v2v/NAME/id_ed25519 USER@IP`: `v2v` on a receiver template, `debian` or `ubuntu` (UEFI) on a stock one). The source stays off: fix and re-run `cutover`, or `rollback`. |
| Guest has no network after cutover | `/var/log/v2v-netfix.log` on the guest (portal console). Static IP configs need the new Exoscale address. |
| Data volume not mounted after cutover | `journalctl -b -u <mount>`; `exo-v2v status -v NAME` and the cutover log list what was rebuilt and what was left as is (e.g. an LV type other than linear/striped). See [Limitations](limitations.md). |
| `plan` warns that the system disk is a guess | No boot order and no VMware Tools disk mapping. Start the VM with Tools running and plan again, or pass `--system-disk scsiX:Y`. |
| Preflight: `spans the 512-sector system disk and 4Kn volumes … fold` | A volume group spans the system disk and another disk, and `plan` could not see it (no VMware Tools mapping). `cleanup --delete-instance`, then `plan --fold DISK`, `create`, `sync`. |
| `sync`: `the root disk must grow … resetting` | The system disk (or a folded disk) grew: the receiver is re-imaged with a bigger root disk; its volumes keep their copy. If interrupted, the next `sync` resumes it. |
| `sync`: `disk(s) … removed from VM` | The copy no longer matches the VM. `cleanup --delete-instance`, then plan and create again. |
| Windows: a dynamic or Storage Spaces volume is missing after cutover | Not supported on data disks (preflight reports it). Roll back, convert the disks to basic (copy spanned/striped volumes to basic ones), migrate again. |
| Windows: D: (data) missing after cutover | First boot runs several driver-install reboots (5–10 min) before the `exo-v2v-online-disks` scheduled task onlines the disks; check `C:\ProgramData\exo-v2v\online-disks.log` and `C:\Program Files\Guestfs\Firstboot\log.txt`. Manually: `Set-Disk -Number 1 -IsOffline $false`. |
| Rehearsal: `receiver template ... not found: rehearsal copies need one` | Build and register [receiver templates](receiver-templates.md) (0.4 or later) |
| Rehearsal: a copy's receiver never answers through the relay | A template older than 0.4 (it waits for the metadata server and never gets its key): rebuild the templates |
| `rehearsal ... is up` when cutting over | Destroy the rehearsal first (`exo-v2v rehearsal destroy NAME`) or pass `--allow-rehearsal` |
| `cutover` interrupted (appliance reboot, lost session) | Run `cutover NAME` again: it records its steps and resumes (it does not convert twice). A group: `group cutover NAME` again; member cutovers still running are waited for |
| `group cutover`: `preflight: ... last cycle ... ago` | Run `group sync` first, or raise `cutover.max_sync_age` |
| `group cutover` stopped on a member | `group status NAME` and the member's log in `~/.exo-v2v/groups/NAME/logs/`. Fix, then `group cutover NAME` again (it resumes), or `group rollback NAME` |
| `group cutover`: health check failed | The member runs on Exoscale but its check did not pass within `health_timeout`: check the service and its security groups (the check reaches the public IP from the appliance). Run `group cutover` again once fixed, or roll back |
| A member has no private address after cutover | `/var/log/v2v-netfix.log` in the guest; `exo compute private-network show v2v-GROUP-NET` lists the leases. The NIC map is in `/etc/v2v-netfix.map` |
| Windows volume shows RAW | It was formatted with < 4 KiB clusters (cannot be expressed in 4 KiB sectors); move the data or reformat with 4 KiB clusters before migrating. |

## 5. Clean-up of Exoscale resources

Everything exo-v2v creates carries the label `exo-v2v=<NAME>`: the instance,
its volumes, `<volume>-4k` relayout volumes and the security group
`exo-v2v-<NAME>`. A group also creates `v2v-GROUP-*` private networks, anti-affinity groups
and the security groups its manifest defines; `group cleanup --delete-instances` removes them. Rollback copies of relaid volumes additionally carry
`exo-v2v-rollback=<export>`.
