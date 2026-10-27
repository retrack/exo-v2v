# Trademarks and licences

## Trademarks

VMware, VMware vSphere, vCenter, ESXi, vMotion, vSAN, vVols, VMware Tools and
PowerCLI are trademarks or registered trademarks of Broadcom Inc. and/or its
subsidiaries in the United States and other countries. The term "Broadcom"
refers to Broadcom Inc. and/or its subsidiaries.

Other trademarks named in this documentation:

- Microsoft, Windows, Windows Server and PowerShell are trademarks of the
  Microsoft group of companies.
- Linux® is the registered trademark of Linus Torvalds in the U.S. and other
  countries.
- Red Hat, Red Hat Enterprise Linux and Fedora are trademarks or registered
  trademarks of Red Hat, Inc. or its subsidiaries in the United States and other
  countries.
- Ubuntu is a registered trademark of Canonical Ltd.
- Debian is a registered trademark owned by Software in the Public Interest, Inc.
- AlmaLinux is a trademark of the AlmaLinux OS Foundation.
- Python is a registered trademark of the Python Software Foundation.

All other trademarks, service marks and product names are the property of their
respective owners. This documentation uses them only to name the products
exo-v2v works with; that use implies no affiliation with their owners and no
endorsement by them.

**exo-v2v is not affiliated with, sponsored, endorsed or supported by Broadcom
Inc. or VMware**, nor by the owners of the other trademarks listed above. Contact
Broadcom or your reseller for support of your VMware products, and open an issue
on [GitHub](https://github.com/retrack/exo-v2v/issues) for exo-v2v.

## VMware software and licences

- **No VMware software inside.** exo-v2v contains no VMware code, and it does
  not use or ship VDDK (the Virtual Disk Development Kit) or any other VMware
  binary. It calls the public APIs of vCenter and ESXi (the vSphere Web Services
  API, and the vSphere Automation API for tags) through
  [pyVmomi](https://github.com/vmware/pyvmomi), the vSphere Python SDK that
  Broadcom publishes under the Apache License 2.0. pip installs pyVmomi from
  PyPI as a separate dependency.
- **Your VMware licences.** You need valid licences for the VMware products you
  migrate from (ESXi, vCenter Server), and you must use exo-v2v within their
  terms. exo-v2v does not provide, extend or change any VMware licence.
- **What the licence must allow.** exo-v2v takes snapshots, enables Changed
  Block Tracking, adds disks to the replication appliance and removes them, and
  powers VMs off and on, all through the vSphere API. Your licence edition must
  allow these operations: the API of the free vSphere Hypervisor is read-only,
  so exo-v2v cannot replicate from it.
- **Tests.** The CI tests the vCenter code paths against govmomi's vCenter
  simulator (`vcsim`, Apache License 2.0), which it downloads from the govmomi
  releases on GitHub. The simulator is not part of exo-v2v.

## Software on the target instances

The receivers that exo-v2v starts on Exoscale install their tools from public
sources when they boot, each under its own licence; exo-v2v does not
redistribute any of them:

- virt-v2v (GPL), libguestfs (LGPL) and the disk tools, from the receiver's
  Debian packages;
- for Windows guests, the virtio-win drivers and srvany, from the Fedora
  project.

A [receiver template](receiver-templates.md) you build holds the same software.
After the migration, a Windows guest keeps the virtio-win drivers that virt-v2v
installed in it.

## Licences of the systems you migrate

Moving a VM to Exoscale does not change who is responsible for the licences of
its software. Before you migrate, check that the licences of its operating
system and applications allow it to run on Exoscale, in particular Windows
Server licences and licences tied to a host, a number of CPUs or a hypervisor.
Windows may need to be activated again on the new hardware.

## Licence and warranty

exo-v2v is licensed under the
[Apache License 2.0](https://github.com/retrack/exo-v2v/blob/main/LICENSE); the
[NOTICE](https://github.com/retrack/exo-v2v/blob/main/NOTICE) file goes with
every copy. exo-v2v is provided **"as is", without warranties or conditions of
any kind**: see sections 7 (Disclaimer of Warranty) and 8 (Limitation of
Liability) of the licence.

A migration stops, copies and changes systems. Try exo-v2v on non-critical VMs
first, keep backups of what you migrate, and keep the source VM until you have
validated the migrated instance (see [Project status](status.md)).
