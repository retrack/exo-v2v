"""Receiver provisioning: minimal cloud-init user-data (the migration's SSH key)
and the agent payload (tar.gz) that `create` streams over SSH, plus the command
that starts the bootstrap (builds the RAM root and kexecs)."""
import base64
import io
import json
import os
import tarfile

from . import __version__

PAYLOAD_FILES = ["agent.py", "bootmask.py", "guestfix.py", "lvmlayout.py", "nbdserve.py", "nbdproto.py",
                 "cbtsim.py", "rootcopy.py", "bootstrap-kexec.sh", "win-support.sh"]
RECEIVER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "receiver")
MiB = 1 << 20


def boot_mask(plan):
    """{"sda": [[offset, length]]}: the first and the last MiB of the system disk
    (MBR/GPT and boot loader, backup GPT), kept off the receiver's disk until the
    conversion (receiver/bootmask.py)."""
    root = next((d for d in plan.get("disks", []) if d.get("role") == "root"), None)
    if not root or not root.get("capacity"):
        return {}
    cap = root["capacity"]
    return {root["export"]: [[0, min(MiB, cap)]] + ([[cap - MiB, MiB]] if cap >= 2 * MiB else [])}


def payload_b64(folds=(), volumes=None, masks=None):
    """The receiver payload. `folds` ([{"export", "offset", "size"}], disks
    replicated into regions of the system disk) goes along as folds.json,
    `volumes` ({export: volume serial}) as volumes.json, `masks` (boot_mask)
    as mask.json, and the exo-v2v version as version.json."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for f in PAYLOAD_FILES:
            ti = tar.gettarinfo(os.path.join(RECEIVER_DIR, f), arcname=f)
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = "root"
            with open(os.path.join(RECEIVER_DIR, f), "rb") as fh:
                tar.addfile(ti, fh)
        for name, obj in (("folds.json", list(folds)), ("volumes.json", volumes), ("mask.json", masks),
                          ("version.json", {"version": __version__})):
            if obj:
                data = json.dumps(obj).encode()
                ti = tarfile.TarInfo(name)
                ti.size, ti.mode = len(data), 0o644
                tar.addfile(ti, io.BytesIO(data))
    return base64.b64encode(buf.getvalue()).decode()


def payload_tgz(folds=(), volumes=None, masks=None):
    return base64.b64decode(payload_b64(folds, volumes, masks))


def build(pubkey):
    """User-data carries only the migration's SSH key. The agent payload is
    streamed over SSH by `exo-v2v create` (Exoscale caps user-data at 32 KiB
    base64-encoded, which the payload outgrew)."""
    return f"""#cloud-config
ssh_authorized_keys:
  - {pubkey.strip()}
"""


def agent_start_command(expected_disks):
    """Receiver template: already in RAM. Wait for the data volumes (the agent
    exports the disks it sees when it starts), then (re)start the agent."""
    return ("mkdir -p /opt/v2v-agent && tar -xzf - -C /opt/v2v-agent && "
            # writable disks only: an instance without a public IP also has a tiny read-only NoCloud drive
            f"for i in $(seq 1 90); do n=0; for d in /sys/block/vd*; do [ \"$(cat $d/ro)\" = 0 ] && [ $(cat $d/size) -gt 131072 ] && n=$((n+1)); done; "
            f"[ $n -ge {expected_disks} ] && break; sleep 5; done && "
            "systemctl restart v2v-agent")


def bootstrap_command(expected_disks):
    """Run on the instance (as root, from the payload directory): detached, because
    the bootstrap ends with kexec and takes the SSH session down."""
    # only the bootstrap is backgrounded: `a && b &` would background the whole
    # list, and a background job reads stdin from /dev/null (tar gets nothing)
    return (f"mkdir -p /opt/v2v-agent && tar -xzf - -C /opt/v2v-agent && "
            f"{{ setsid nohup env EXPECTED_DISKS={expected_disks} AGENT_DIR=/opt/v2v-agent "
            f"bash /opt/v2v-agent/bootstrap-kexec.sh > /var/log/v2v-bootstrap.out 2>&1 < /dev/null & }}")
