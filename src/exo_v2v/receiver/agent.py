#!/usr/bin/env python3
"""Receiver agent for Option C (runs from RAM on the target Exoscale instance).

- Discovers virtio disks: vda -> export "sda" (system disk), vdb -> "sdb", ...
  (override with /etc/v2v-agent.json: {"exports": {"sda": "/dev/vda", ...}})
- Refuses to start if any exported disk is mounted (i.e. not booted from RAM).
- Starts nbdserve.py on 127.0.0.1:10809 for all exports.
- Control API on 127.0.0.1:8787 (reach both through an SSH tunnel):
    GET  /status                    disks, sizes, serials, bytes written, convert state
    GET  /hashmap?export=sda&chunk=64M   per-chunk sha256 (compare with cbtsim.py hashmap)
    GET  /preflight                 4Kn compatibility of the data volumes (read-only)
    POST /convert                   virt-v2v-in-place on all disks (nested KVM if present),
                                    then 4Kn partition-table adaptation of the data
                                    volumes and the first-boot network fix (guestfix.py)
    GET  /convert                   conversion state, per-step results, problems, log tail
    POST /relayout?export=sdb&spare=/dev/vdc   rebuild 512-only filesystems (XFS sectsz=512,
                                    ext 1K) of a data volume onto an attached empty spare volume
                                    (same UUIDs/labels/PARTUUIDs); the orchestrator then swaps them
    GET  /relayout                  per-export relayout state/report
    POST /flush                     sync + flush buffers on every disk
    GET/POST /meta?export=sda       sync metadata (VMware change ID) for the sender
"""
import glob
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.dirname(HERE)]  # flat on the receiver, tools/ in the kit
import cbtsim  # noqa: E402
import guestfix  # noqa: E402

NBDSERVE = next(p for p in (os.path.join(HERE, "nbdserve.py"),
                            os.path.join(os.path.dirname(HERE), "nbdserve.py")) if os.path.exists(p))

API_PORT = int(os.environ.get("V2V_API_PORT", 8787))
NBD_PORT = int(os.environ.get("V2V_NBD_PORT", 10809))
CONVERT_LOG = "/var/log/v2v-convert.log"
STATE = {"convert": {"state": "idle"}, "relayout": {}, "started": time.time()}
META = {}


def discover():
    cfg = os.environ.get("V2V_AGENT_CONFIG", "/etc/v2v-agent.json")
    if os.path.exists(cfg):
        return json.load(open(cfg))["exports"]
    devs = sorted(glob.glob("/sys/block/vd*")) + sorted(glob.glob("/sys/block/sd*"))
    exports = {}
    for i, d in enumerate(devs):
        exports["sd" + chr(ord("a") + i)] = "/dev/" + os.path.basename(d)
    return exports


def disk_info(name, dev):
    base = os.path.basename(dev)
    serial = ""
    for p in (f"/sys/block/{base}/serial", f"/sys/block/{base}/device/serial"):
        if os.path.exists(p):
            serial = open(p).read().strip()
    return {"export": name, "dev": dev, "size": cbtsim.disk_size(dev), "serial": serial}


def mounted_devices():
    out = set()
    for line in open("/proc/mounts"):
        src = line.split()[0]
        if src.startswith("/dev/"):
            out.add(os.path.realpath(src))
    for line in open("/proc/swaps").readlines()[1:]:
        out.add(os.path.realpath(line.split()[0]))
    return out


def safety_check(exports):
    busy = mounted_devices()
    for dev in exports.values():
        base = os.path.realpath(dev)
        for m in busy:
            if m == base or m.startswith(base):
                sys.exit(f"REFUSING: {m} is mounted; the agent must run from RAM")


def data_exports(exports):
    """Every export but the system disk (sda = /dev/vda, 512-byte sectors)."""
    return {k: v for k, v in sorted(exports.items()) if k != "sda"}


def run_convert(exports):
    STATE["convert"] = {"state": "running", "started": time.time(), "step": "virt-v2v", "problems": []}
    st = STATE["convert"]
    env = dict(os.environ, LIBGUESTFS_BACKEND="direct")
    if os.path.exists("/usr/share/virtio-win/virtio-win.iso"):
        env.setdefault("VIRTIO_WIN", "/usr/share/virtio-win/virtio-win.iso")
    # The RAM root is a size-capped tmpfs (default: half the RAM). Ubuntu's base
    # OS + virtio-win (~0.85 GB) leave < 1 GB, below virt-v2v's free-space check
    # for /var/tmp: raise the cap (pages are only used on demand).
    if subprocess.call(["sh", "-c", "findmnt -no FSTYPE / | grep -qE 'tmpfs|rootfs'"]) == 0:
        subprocess.call(["mount", "-o", "remount,size=85%", "/"])
    disks = [exports[k] for k in sorted(exports)]
    cmd = ["virt-v2v-in-place", "-v", "-x", "-i", "disk", *disks]
    try:
        with open(CONVERT_LOG, "w") as log:
            log.write(f"kvm available: {os.path.exists('/dev/kvm')}\n$ {' '.join(cmd)}\n")
            log.flush()
            rc = subprocess.call(cmd, stdout=log, stderr=subprocess.STDOUT, env=env)
        st["rc"] = rc
        if rc != 0:
            raise RuntimeError(f"virt-v2v-in-place rc={rc}")
        # After virt-v2v (which reads the guest through a 512-sector appliance view):
        # make data volumes readable by a guest that sees them as 4Kn.
        st["step"] = "adapt"
        st["adapt"] = {k: guestfix.adapt(v) for k, v in data_exports(exports).items()}
        for k, r in st["adapt"].items():
            st["problems"] += [f"{k}: {p}" for p in r["problems"]]
        st["step"] = "netfix"
        st["netfix"] = guestfix.netfix(exports["sda"])
        subprocess.call(["sync"])
        st.update(state="done", step="", ended=time.time())
    except Exception as e:  # e.g. virt-v2v-in-place not installed, rc != 0
        subprocess.call(["sync"])
        st.update(state="failed", error=str(e), ended=time.time())


def spare_disks(exports):
    """Attached disks that are not exported (e.g. a spare volume for /relayout)."""
    used = {os.path.realpath(v) for v in exports.values()}
    out = []
    for d in sorted(glob.glob("/sys/block/vd*")):
        dev = "/dev/" + os.path.basename(d)
        if dev not in used:
            out.append(disk_info("", dev))
    return out


def run_relayout(export, src, spare):
    st = STATE["relayout"][export] = {"state": "running", "spare": spare, "started": time.time()}
    try:
        st["report"] = guestfix.relayout(src, spare)
        st.update(state="done" if st["report"]["compatible"] else "failed", ended=time.time())
    except Exception as e:
        st.update(state="failed", error=str(e), ended=time.time())


def tail(path, n=40):
    try:
        return open(path, errors="replace").read().splitlines()[-n:]
    except FileNotFoundError:
        return []


class Handler(BaseHTTPRequestHandler):
    exports = {}

    def _json(self, obj, code=200):
        body = json.dumps(obj, indent=1).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/status":
            self._json({"ramroot": os.path.exists("/etc/v2v-ramroot"),
                        "kvm": os.path.exists("/dev/kvm"),
                        "uptime_s": int(time.time() - STATE["started"]),
                        "nbd_port": NBD_PORT,
                        "disks": [disk_info(k, v) for k, v in sorted(self.exports.items())],
                        "spares": spare_disks(self.exports),
                        "convert": STATE["convert"]})
        elif u.path == "/hashmap":
            dev = self.exports.get(q.get("export", "sda"))
            if not dev:
                return self._json({"error": "unknown export"}, 404)
            self._json(cbtsim.hashmap(dev, cbtsim.parse_size(q.get("chunk", "64M"))))
        elif u.path == "/convert":
            self._json({**STATE["convert"], "log_tail": tail(CONVERT_LOG)})
        elif u.path == "/relayout":
            self._json(STATE["relayout"])
        elif u.path == "/preflight":
            reps = {k: guestfix.analyze(v) for k, v in data_exports(self.exports).items()}
            self._json({"compatible": all(r["compatible"] for r in reps.values()), "disks": reps})
        elif u.path == "/meta":
            if q.get("export") not in self.exports:
                return self._json({"error": "unknown export"}, 404)
            self._json(META.get(q["export"], {}))
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        if u.path == "/convert":
            if STATE["convert"]["state"] == "running":
                return self._json({"error": "already running"}, 409)
            threading.Thread(target=run_convert, args=(self.exports,), daemon=True).start()
            self._json({"state": "running"}, 202)
        elif u.path == "/meta":
            # Per-export sync metadata, e.g. {"change_id": "<VMware changeId>"}.
            # Kept in RAM on purpose: a receiver reboot invalidates the copy anyway.
            export = parse_qs(u.query).get("export", [""])[0]
            if export not in self.exports:
                return self._json({"error": "unknown export"}, 404)
            n = int(self.headers.get("Content-Length", 0))
            META.setdefault(export, {}).update(json.loads(self.rfile.read(n) or b"{}"))
            self._json(META[export])
        elif u.path == "/relayout":
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            export, spare = q.get("export", ""), q.get("spare", "")
            if export not in self.exports or export == "sda":
                return self._json({"error": "export must be a data volume (sdb...)"}, 400)
            if spare not in [d["dev"] for d in spare_disks(self.exports)]:
                return self._json({"error": f"{spare} is not an attached, unexported disk"}, 400)
            if STATE["relayout"].get(export, {}).get("state") == "running":
                return self._json({"error": "already running"}, 409)
            threading.Thread(target=run_relayout, args=(export, self.exports[export], spare), daemon=True).start()
            self._json({"state": "running"}, 202)
        elif u.path == "/flush":
            subprocess.call(["sync"])
            for dev in self.exports.values():
                subprocess.call(["blockdev", "--flushbufs", dev])
            self._json({"flushed": sorted(self.exports)})
        else:
            self._json({"error": "not found"}, 404)

    def log_message(self, fmt, *args):
        sys.stderr.write("api: " + fmt % args + "\n")


def main():
    exports = discover()
    if not exports:
        sys.exit("no disks found")
    safety_check(exports)
    nbd = subprocess.Popen([sys.executable, "-I", NBDSERVE,
                            "--port", str(NBD_PORT),
                            *[a for k, v in sorted(exports.items()) for a in ("--export", f"{k}={v}")]])

    def watch_nbd():  # if the data plane dies, exit so systemd restarts the pair
        rc = nbd.poll()
        while rc is None:
            time.sleep(2)
            rc = nbd.poll()
        sys.exit(f"nbdserve exited rc={rc}; restarting agent")

    threading.Thread(target=watch_nbd, daemon=True).start()
    Handler.exports = exports
    print(f"agent: exports {exports}, NBD :{NBD_PORT}, API :{API_PORT}", flush=True)
    try:
        ThreadingHTTPServer(("127.0.0.1", API_PORT), Handler).serve_forever()
    finally:
        nbd.terminate()


if __name__ == "__main__":
    main()
