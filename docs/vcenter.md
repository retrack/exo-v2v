# vCenter

> **Experimental.** exo-v2v 0.6.0 is an early release, validated in one lab (ESXi 7.0 U3 to Exoscale ch-gva-2). Try it on non-critical VMs first; commands and behaviour may change.

exo-v2v talks to a vCenter Server the way it talks to a standalone ESXi host:
`VSPHERE_HOST` names the vCenter (`vcenter.example.com`, or
`vcenter.example.com:8443` on another port), `VSPHERE_USER` an SSO account with
the role below. This page covers what a vCenter adds: several datacenters and
clusters, folders and tags, roles, DRS.

**What was tested.** Everything that reads or writes disks was validated on a
standalone ESXi 7.0 U3. The vCenter code paths (inventory paths, duplicate
names, folders, tags, the privilege check, DRS settings) are tested against
govmomi's vCenter simulator (`vcsim`), in CI. vMotion, Storage vMotion and DRS
moves during a replication, and privilege enforcement by a real vCenter, are
not tested yet.

## Naming the VMs

| Form | Example | When |
|---|---|---|
| Name | `--vm web01` | The name is unique in the vCenter (templates do not count) |
| Inventory path | `--vm DC1/vm/shop/web01` | Several VMs share the name (two datacenters, two folders): exo-v2v refuses the bare name and lists their paths |
| Instance UUID | `--vm uuid:5032c8a4-...` | Scripts |

A migration records the VM's instance UUID: renaming the VM or moving it to
another folder afterwards does not lose it.

For a group, `group discover` takes VMs by name or path, a whole folder, or a
vCenter tag, in any mix:

```bash
exo-v2v group discover shop --folder DC1/vm/shop --zone ch-gva-2
exo-v2v group discover shop --tag app:shop --vm DC1/vm/infra/lb01 --zone ch-gva-2
```

`--folder` includes subfolders. `--tag CATEGORY:TAG` reads the vSphere
Automation API (`/api`, or `/rest` on vCenter 6.7 and early 7.0); standalone
ESXi hosts have no tags. The draft manifest names each VM by its name, or by
its path when the name is ambiguous.

## The vCenter role

Create a role with these privileges and grant it to the migration account:

| Where | Privileges |
|---|---|
| The source VMs (or their folder, with propagation) | `VirtualMachine.State.CreateSnapshot`, `VirtualMachine.State.RemoveSnapshot`, `VirtualMachine.Config.ChangeTracking`, `VirtualMachine.Interact.PowerOff`, `VirtualMachine.Interact.PowerOn`, `VirtualMachine.Provisioning.DiskRandomRead` |
| The replication appliance VM | `VirtualMachine.Config.AddExistingDisk`, `VirtualMachine.Config.RemoveDisk` |
| The source VMs' datastores | `Datastore.FileManagement` |

With govc:

```bash
govc role.create exo-v2v VirtualMachine.State.CreateSnapshot VirtualMachine.State.RemoveSnapshot \
  VirtualMachine.Config.ChangeTracking VirtualMachine.Interact.PowerOff VirtualMachine.Interact.PowerOn \
  VirtualMachine.Provisioning.DiskRandomRead VirtualMachine.Config.AddExistingDisk \
  VirtualMachine.Config.RemoveDisk Datastore.FileManagement
govc permissions.set -principal exo-v2v@vsphere.local -role exo-v2v -propagate=true /DC1/vm/shop
govc permissions.set -principal exo-v2v@vsphere.local -role exo-v2v -propagate=false /DC1/vm/infra/v2v-appliance
govc permissions.set -principal exo-v2v@vsphere.local -role exo-v2v -propagate=false /DC1/datastore/ds-prod-01
```

With PowerCLI:

```powershell
$p = Get-VIPrivilege -Id VirtualMachine.State.CreateSnapshot, VirtualMachine.State.RemoveSnapshot,
  VirtualMachine.Config.ChangeTracking, VirtualMachine.Interact.PowerOff, VirtualMachine.Interact.PowerOn,
  VirtualMachine.Provisioning.DiskRandomRead, VirtualMachine.Config.AddExistingDisk,
  VirtualMachine.Config.RemoveDisk, Datastore.FileManagement
New-VIRole -Name exo-v2v -Privilege $p
New-VIPermission -Entity (Get-Folder shop) -Principal 'VSPHERE.LOCAL\exo-v2v' -Role exo-v2v -Propagate:$true
New-VIPermission -Entity (Get-VM v2v-appliance) -Principal 'VSPHERE.LOCAL\exo-v2v' -Role exo-v2v
New-VIPermission -Entity (Get-Datastore ds-prod-01) -Principal 'VSPHERE.LOCAL\exo-v2v' -Role exo-v2v
```

`exo-v2v plan` (and `group plan`) asks the vCenter which of these privileges
the account lacks on the VM, its datastores and, when it runs on the appliance
as root, the appliance VM, and lists them as warnings.

## Where the appliance runs

The appliance reads a VM's disks by attaching its frozen snapshot disks to
itself (hot-add), so its host must mount every datastore of the VMs it
replicates. In a cluster with shared datastores, any host will do; VMs on a
host's local datastore need an appliance on that host. `plan` (run on the
appliance) warns when its host does not see a datastore of the VM; `sync`
refuses to start, naming the datastore.

DRS must not move the appliance while snapshot disks are attached to it. In a
cluster with fully automated DRS, `plan` and `sync` warn until the appliance has
a VM override (DRS automation level **Disabled**, or **Partially automated**).

## During the replication

| Event | Effect |
|---|---|
| The source VM moves to another host (vMotion, DRS) | Nothing: snapshots and hot-add do not depend on the VM's host. Each cycle checks again that the appliance's host sees the VM's datastores |
| The source VM's disks move (Storage vMotion) | Each cycle reads the disk paths again. When VMware resets the change tracking, the next cycle copies the allocated areas of the disks again (a full cycle, not a broken one). A cycle running while a disk moves fails and the next one starts over |
| The VM is renamed or moved to another folder | Nothing: the migration finds it by instance UUID |
