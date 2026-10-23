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
    POST /convert {"netmap": [...]} virt-v2v-in-place on all disks (nested KVM if present),
                                    then 4Kn partition-table adaptation of the data
                                    volumes and the first-boot network fix (guestfix.py;
                                    netmap: [{"mac", "mode": static|dhcp, "ip", "prefix",
                                    "nogw"}] for the instance's NICs, public first)
    GET  /convert                   conversion state, per-step results, problems, log tail
    POST /relayout {"spares": {"sdb": "/dev/vdc", ...}}   rebuild 512-only filesystems (XFS
                                    sectsz=512, ext 1K - partitions or LVs) of data volumes onto
                                    attached empty spare volumes (same UUIDs/labels/PARTUUIDs; a VG
                                    spanning several volumes is rebuilt across them together); the
                                    orchestrator then swaps them. (?export=sdb&spare=/dev/vdc: one)
    GET  /relayout                  per-export relayout state/report
    POST /exports {"sdb": "/dev/vdd", ...}   point exports at (new) devices - a volume that was
                                    resized or added - and restart the NBD server
    POST /flush                     sync + flush buffers on every disk
    GET/POST /meta?export=sda       sync metadata (VMware change ID) for the sender

Folded disks (folds.json next to the agent: [{"export", "offset", "size"}]) are
regions of the system disk exported through loop devices; at conversion their
partitions are added to the system disk's table (guestfix.fold).
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
FOLDS_FILE = os.path.join(HERE, "folds.json")
VOLUMES_FILE = os.path.join(HERE, "volumes.json")   # {"sdb": "<volume serial>", ...}
AGENT_CONFIG = os.environ.get("V2V_AGENT_CONFIG", "/etc/v2v-agent.json")


def folds():
    return json.load(open(FOLDS_FILE)) if os.path.exists(FOLDS_FILE) else []


def fold_loop(root, off, size):
    """Loop device over [off, off+size) of the system disk (reused if it exists)."""
    out = subprocess.run(["losetup", "-n", "-O", "NAME,OFFSET,SIZELIMIT", "-j", root],
                         capture_output=True, text=True).stdout.split()
    for i in range(0, len(out) - 2, 3):
        if int(out[i + 1]) == off and int(out[i + 2]) == size:
            return out[i]
    return subprocess.run(["losetup", "-f", "--show", "--direct-io=on", "-o", str(off), "--sizelimit",
                           str(size), root], capture_output=True, text=True, check=True).stdout.strip()


def serial_of(dev):
    base = os.path.basename(dev)
    for p in (f"/sys/block/{base}/serial", f"/sys/block/{base}/device/serial"):
        if os.path.exists(p):
            return open(p).read().strip()
    return ""


def by_serial(serial):
    return next((f"/dev/{os.path.basename(d)}" for d in sorted(glob.glob("/sys/block/vd*"))
                 if serial and serial_of(d) == serial), None)


def discover():
    if os.path.exists(AGENT_CONFIG):
        exports = json.load(open(AGENT_CONFIG))["exports"]
    elif os.path.exists(VOLUMES_FILE):
        # volumes by serial (Exoscale: the volume ID's first 20 characters), not by
        # device order: robust to volumes resized/added/re-attached later
        exports = {"sda": "/dev/vda"}
        for export, serial in json.load(open(VOLUMES_FILE)).items():
            dev = by_serial(serial)
            if not dev:
                sys.exit(f"volume {serial} ({export}) not attached")
            exports[export] = dev
    else:
        devs = sorted(glob.glob("/sys/block/vd*")) + sorted(glob.glob("/sys/block/sd*"))
        exports = {}
        for i, d in enumerate(devs):
            exports["sd" + chr(ord("a") + i)] = "/dev/" + os.path.basename(d)
    for f in folds():
        if f["export"] not in exports or exports[f["export"]].startswith("/dev/vd"):
            exports[f["export"]] = fold_loop(exports["sda"], f["offset"], f["size"])
    return exports


def persist(exports):
    """Survive an agent restart with the same export -> device mapping."""
    with open(AGENT_CONFIG + ".tmp", "w") as fh:
        json.dump({"exports": exports}, fh)
    os.replace(AGENT_CONFIG + ".tmp", AGENT_CONFIG)


def disk_info(name, dev):
    try:
        size = cbtsim.disk_size(dev)
    except OSError:
        size = 0
    return {"export": name, "dev": dev, "size": size, "serial": serial_of(dev)}


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
    """Every Block Storage export: not the system disk (sda = /dev/vda, 512-byte
    sectors), not the disks folded into it."""
    fold_names = {f["export"] for f in folds()}
    return {k: v for k, v in sorted(exports.items()) if k != "sda" and k not in fold_names}


def device_exports(exports):
    """System disk + Block Storage volumes: the guest's disks after folding."""
    return {"sda": exports["sda"], **data_exports(exports)}


def run_convert(exports, netmap=None):
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
    devs = device_exports(exports)
    disks = [devs[k] for k in sorted(devs)]
    cmd = ["virt-v2v-in-place", "-v", "-x", "-i", "disk", *disks]
    try:
        fl = [f for f in folds() if f["export"] in exports]
        if fl:
            # the folded regions become partitions of the system disk BEFORE
            # virt-v2v, so it sees the guest's volume groups complete. The copy
            # is final: serve the real disks only, release the loop views.
            st["step"] = "fold"
            NBD.restart(devs)
            for f in fl:
                subprocess.call(["losetup", "-d", exports.pop(f["export"])])
            persist(exports)
            st["fold"] = guestfix.fold(exports["sda"], fl)
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
        lvm = guestfix.lvm_report(list(devs.values()))
        by_dev = {v: k for k, v in devs.items()}
        for d, probs in lvm["problems"].items():
            k = by_dev.get(d)
            if k in st["adapt"]:
                st["adapt"][k]["problems"] += probs
                st["adapt"][k]["compatible"] = False
        st["lvm"] = lvm
        st["relayout_groups"], st["unfixable"] = guestfix.relayout_groups(devs)
        for k, r in st["adapt"].items():
            st["problems"] += [f"{k}: {p}" for p in r["problems"]]
        st["step"] = "netfix"
        st["netfix"] = guestfix.netfix(exports["sda"], netmap)
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


def run_relayout(group, exports):
    """group: {export: spare device}; one report per export."""
    sts = {k: {"state": "running", "spare": v, "group": sorted(group), "started": time.time()}
           for k, v in group.items()}
    STATE["relayout"].update(sts)
    try:
        others = [exports["sda"]]
        reps = guestfix.relayout_group({exports[k]: v for k, v in group.items()}, others=others)
        for k in group:
            r = reps[exports[k]]
            sts[k].update(report=r, state="done" if r["compatible"] else "failed", ended=time.time())
    except Exception as e:
        for k in group:
            sts[k].update(state="failed", error=str(e), ended=time.time())


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
                        "devices": [disk_info("", f"/dev/{os.path.basename(d)}")
                                    for d in sorted(glob.glob("/sys/block/vd*"))],
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
            # read-only, on the copy as it is now: the data volumes as the guest
            # will see them (4Kn) and the volume groups across all disks
            data = data_exports(self.exports)
            reps = {k: guestfix.analyze(v) for k, v in data.items()}
            lvm = guestfix.lvm_report(list(self.exports.values()))
            by_dev = {v: k for k, v in self.exports.items()}
            for d, probs in lvm["problems"].items():
                k = by_dev.get(d)
                if k in reps:
                    reps[k]["problems"] += probs
                    reps[k]["compatible"] = False
            self._json({"compatible": all(r["compatible"] for r in reps.values()), "disks": reps,
                        "lvm": lvm, "folds": folds()})
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
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            threading.Thread(target=run_convert, args=(self.exports, body.get("netmap")), daemon=True).start()
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
            if q.get("export"):
                group = {q["export"]: q.get("spare", "")}
            else:
                n = int(self.headers.get("Content-Length", 0))
                group = json.loads(self.rfile.read(n) or b"{}").get("spares", {})
            data = data_exports(self.exports)
            spares = [d["dev"] for d in spare_disks(self.exports)]
            for export, spare in group.items():
                if export not in data:
                    return self._json({"error": f"{export}: not a data volume (sdb...)"}, 400)
                if spare not in spares:
                    return self._json({"error": f"{spare} is not an attached, unexported disk"}, 400)
                if STATE["relayout"].get(export, {}).get("state") == "running":
                    return self._json({"error": "already running"}, 409)
            if not group or len(set(group.values())) != len(group):
                return self._json({"error": "one distinct spare per export"}, 400)
            threading.Thread(target=run_relayout, args=(group, dict(self.exports)), daemon=True).start()
            self._json({"state": "running"}, 202)
        elif u.path == "/exports":
            n = int(self.headers.get("Content-Length", 0))
            new = json.loads(self.rfile.read(n) or b"{}")
            for export, dev in new.items():
                if not (export.startswith("sd") and os.path.exists(dev)) or export == "sda":
                    return self._json({"error": f"bad export {export}={dev}"}, 400)
            self.exports.update(new)
            for export in new:
                META.pop(export, None)          # a new device: its change ID is unknown
            persist(self.exports)
            NBD.restart(self.exports)
            self._json({"exports": self.exports})
        elif u.path == "/flush":
            subprocess.call(["sync"])
            for dev in self.exports.values():
                subprocess.call(["blockdev", "--flushbufs", dev])
            self._json({"flushed": sorted(self.exports)})
        else:
            self._json({"error": "not found"}, 404)

    def log_message(self, fmt, *args):
        sys.stderr.write("api: " + fmt % args + "\n")


class Nbd:
    """The NBD server process; restart() swaps the export set. If it dies on its
    own, the agent exits so that systemd restarts the pair."""

    def __init__(self):
        self.proc, self.lock = None, threading.Lock()

    def start(self, exports):
        self.proc = subprocess.Popen([sys.executable, "-I", NBDSERVE, "--port", str(NBD_PORT),
                                      *[a for k, v in sorted(exports.items()) for a in ("--export", f"{k}={v}")]])

    def restart(self, exports):
        with self.lock:
            old, self.proc = self.proc, None
            old.terminate()
            old.wait(timeout=30)
            for _ in range(50):          # the port is free once the old server is gone
                try:
                    self.start(exports)
                    time.sleep(0.5)
                    if self.proc.poll() is None:
                        return
                except OSError:
                    pass
                time.sleep(0.5)
            raise RuntimeError("nbdserve did not restart")

    def watch(self):
        while True:
            time.sleep(2)
            with self.lock:
                if self.proc is not None and self.proc.poll() is not None:
                    print(f"nbdserve exited rc={self.proc.returncode}; restarting agent", flush=True)
                    os._exit(3)


NBD = Nbd()


def main():
    exports = discover()
    if not exports:
        sys.exit("no disks found")
    safety_check({k: v for k, v in exports.items() if not v.startswith("/dev/loop")})
    NBD.start(exports)
    threading.Thread(target=NBD.watch, daemon=True).start()
    Handler.exports = exports
    print(f"agent: exports {exports}, folds {folds()}, NBD :{NBD_PORT}, API :{API_PORT}", flush=True)
    try:
        ThreadingHTTPServer(("127.0.0.1", API_PORT), Handler).serve_forever()
    finally:
        NBD.proc and NBD.proc.terminate()


if __name__ == "__main__":
    main()
