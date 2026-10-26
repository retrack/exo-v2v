"""Test shim for govmomi's vcsim only (put this directory on PYTHONPATH): real
vCenter and ESXi need none of it.

- pyVmomi 9 reads a managed property with the `Fetch` method; vcsim's reply for
  an unset property (vm.snapshot without snapshots) cannot be parsed: read
  properties through the PropertyCollector instead, as pyVmomi 8 did.
- vcsim leaves two enum properties of every VM empty (runtime.faultToleranceState,
  runtime.recordReplayState); pyVmomi rejects an empty enum value: read it as
  what a real server reports for a VM without fault tolerance or replay.
"""
from pyVmomi import StubAdapterAccessorImpl, vim, vmodl


def _invoke_accessor(self, mo, info):
    pc = vmodl.query.PropertyCollector
    collector = vmodl.query.PropertyCollector("propertyCollector", self)
    spec = pc.FilterSpec(objectSet=[pc.ObjectSpec(obj=mo, skip=False)],
                         propSet=[pc.PropertySpec(type=mo.__class__, pathSet=[info.name])])
    res = collector.RetrieveContents([spec])
    if res and res[0].propSet:
        return res[0].propSet[0].val
    return [] if info.type.__name__.endswith("[]") else None


StubAdapterAccessorImpl.StubAdapterAccessorMixin.InvokeAccessor = _invoke_accessor
setattr(vim.VirtualMachine.FaultToleranceState, "", vim.VirtualMachine.FaultToleranceState.notConfigured)
setattr(vim.VirtualMachine.RecordReplayState, "", vim.VirtualMachine.RecordReplayState.inactive)
