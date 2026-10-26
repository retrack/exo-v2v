"""Read-only web status page: `exo-v2v web [--listen 127.0.0.1:8780]`.

One page and a small JSON API over the state files (~/.exo-v2v). GET only: it
never writes a state file, never takes a lock (running commands are found by
the pid their lock file records) and needs no Exoscale or vSphere credentials.
Loopback by default: reach it with `ssh -L 8780:127.0.0.1:8780 appliance`.
Any other address needs a token (--token-file FILE or EXO_V2V_WEB_TOKEN);
open http://HOST:8780/login?token=TOKEN once (it sets a cookie).

  GET /                                  the page
  GET /api/overview                      groups and migrations (summaries)
  GET /api/migrations/NAME               one migration: plan, cycles, cutover steps, events
  GET /api/groups/NAME                   one group: members by tier, cutover, events
  GET /api/groups/NAME/log/VM?lines=N    the tail of a member's log
  GET /api/rehearsals/NAME               one rehearsal: copies, relay, steps, report
"""
import hmac
import ipaddress
import json
import os
import socket
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

from . import __version__
from .state import State, home
from .util import die

PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web.html")
CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
       "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def _lock(path):
    from .group import lock_holder
    return lock_holder(path)


def _load(name):
    try:
        return State.load(name)
    except (FileNotFoundError, ValueError, OSError):
        return None


def summary(st):
    p, r = st["plan"], st["receiver"]
    last = st["cycles"][-1] if st["cycles"] else None
    return {
        "name": st.name, "vm": p.get("vm"), "group": st.data.get("group"), "tier": p.get("tier"),
        "phase": st["phase"], "zone": p.get("zone"), "os": p.get("os_family"), "firmware": p.get("firmware"),
        "receiver": r.get("name"), "ip": r.get("ip"), "type": p.get("target_type"),
        "disks": len(p.get("disks", [])), "bytes": sum(d.get("capacity", 0) for d in p.get("disks", [])),
        "cycles": len(st["cycles"]),
        "last_cycle": {"t": last.get("t"), "seconds": last.get("seconds"), "final": last.get("final"),
                       "covered": sum(d.get("covered", 0) for d in last.get("disks", {}).values()),
                       "modes": sorted({d.get("mode") for d in last.get("disks", {}).values()})} if last else None,
        "downtime_s": st.data.get("downtime_s"),
        "private_ips": [n["ip"] for n in p.get("networks", []) if n.get("ip")],
        "errors": len(p.get("errors", [])), "warnings": len(p.get("warnings", [])),
        "busy": _lock(os.path.join(st.dir, ".lock")),
        "down": r.get("down"),          # the receiver stopped answering: `exo-v2v recover`
        "sync": {k: (st.data.get("sync") or {}).get(k) for k in ("failures", "last_error", "last_ok")},
        "updated": os.path.getmtime(st.path),
    }


def detail(st):
    d = summary(st)
    p = dict(st["plan"])
    inv = p.pop("inventory", {}) or {}
    p["source"] = {k: inv.get(k) for k in ("guest_full", "cpus", "memory_mb", "power", "tools", "nics",
                                           "guest_mounts", "snapshots")}
    d.update(plan=p, cycles=st["cycles"][-200:], events=st["events"][-300:],
             cutover_steps=st.data.get("cutover_steps", {}), convert=st.data.get("convert"),
             preflight={k: v.get("problems") for k, v in ((st.data.get("preflight") or {}).get("disks") or {}).items()},
             receiver={k: v for k, v in st["receiver"].items() if k not in ("saved_meta",)})
    return d


def rehearsal_doc(name, full=False):
    from .rehearsal import rdir, shadow_name
    try:
        r = json.load(open(os.path.join(rdir(), name, "rehearsal.json")))
    except (OSError, ValueError):
        return None
    s = r.get("settings") or {}
    copies = []
    for m in r.get("members", []):
        st = _load(shadow_name(m["migration"]))
        c = {"vm": m["vm"], "tier": m["tier"], "migration": m["migration"]}
        if st:
            c.update(phase=st["phase"], address=st["receiver"].get("ip"), name=shadow_name(m["migration"]),
                     private_ips=[n["ip"] for n in st["plan"].get("networks", []) if n.get("ip")],
                     health=st.data.get("rehearsal_health"), seed=st.data.get("seed"),
                     boot_s=st.data.get("rehearsal_boot_s"), busy=_lock(os.path.join(st.dir, ".lock")))
        copies.append(c)
    out = {"name": name, "state": r.get("state"), "step": r.get("step"), "created": r.get("created"),
           "ttl": s.get("ttl"), "ttl_s": s.get("ttl_s"), "relay": (r.get("relay") or {}).get("ip"),
           "copies": copies, "error": r.get("error"), "busy": _lock(os.path.join(rdir(), name, ".lock"))}
    if full:
        out.update(steps=r.get("steps", {}), networks=r.get("networks", []), report=r.get("report"),
                   events=r.get("events", [])[-300:], access=s.get("access", []), hosts=s.get("hosts", {}))
    return out


def group_doc(name, full=False):
    from .group import groups_dir
    gdir = os.path.join(groups_dir(), name)
    try:
        g = json.load(open(os.path.join(gdir, "group.json")))
    except (OSError, ValueError):
        return None
    members = []
    for x in g.get("members", []):
        st = _load(x["migration"])
        s = summary(st) if st else {"name": x["migration"], "phase": "missing"}
        s.update(vm=x["vm"], tier=x["tier"], **(g.get("cutover") or {}).get("members", {}).get(x["vm"], {}))
        members.append(s)
    m = g.get("manifest", {})
    out = {"name": name, "zone": m.get("zone"), "members": members, "cutover": g.get("cutover"),
           "last_sync": g.get("last_sync"), "busy": _lock(os.path.join(gdir, ".lock")),
           "updated": os.path.getmtime(os.path.join(gdir, "group.json"))}
    rh = rehearsal_doc(name)
    out["rehearsal"] = rh if rh and rh["state"] != "destroyed" else None
    if full:
        out.update(events=g.get("events", [])[-300:], resources=g.get("resources", {}),
                   networks=m.get("networks", {}), hooks=m.get("hooks", {}), settings=m.get("cutover", {}),
                   anti_affinity=m.get("anti_affinity", {}))
    return out


def overview():
    from .group import Group
    from .rehearsal import Rehearsal
    migs = []
    for n in State.all():
        st = _load(n)
        if st and not st.data.get("rehearsal_of"):         # copies show under their rehearsal
            migs.append(summary(st))
    rhs = [r for r in (rehearsal_doc(n) for n in Rehearsal.all()) if r and r["state"] != "destroyed"]
    try:
        from .alerts import config
        lag = config()["lag_s"]
    except SystemExit:                # a broken config.yaml must not take the page down
        lag = None
    return {"host": socket.gethostname(), "version": __version__, "now": time.time(), "home": home(),
            "lag_alert_s": lag,
            "groups": [g for g in (group_doc(n) for n in Group.all()) if g], "migrations": migs,
            "rehearsals": rhs}


def tail(path, lines):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 256 * 1024))
            return f.read().decode(errors="replace").splitlines()[-lines:]
    except OSError:
        return []


class Handler(BaseHTTPRequestHandler):
    token = None
    server_version = f"exo-v2v/{__version__}"

    def _send(self, code, body, ctype="application/json", extra=()):
        if not isinstance(body, bytes):
            body = (json.dumps(body, default=str) if ctype == "application/json" else body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _authorized(self):
        if not self.token:
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].strip(), self.token):
            return True
        for c in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = c.strip().partition("=")
            if k == "v2v_token" and hmac.compare_digest(v, self.token):
                return True
        return False

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if u.path == "/login":
            if self.token and hmac.compare_digest(q.get("token", ""), self.token):
                return self._send(303, "", "text/plain", [("Location", "/"), (
                    "Set-Cookie", f"v2v_token={quote(self.token)}; HttpOnly; SameSite=Strict; Path=/")])
            return self._send(403, "bad token", "text/plain")
        if not self._authorized():
            return self._send(401, "token required: open /login?token=...", "text/plain")
        parts = [p for p in u.path.split("/") if p]
        try:
            if not parts:
                return self._send(200, open(PAGE, "rb").read(), "text/html")
            if parts == ["api", "overview"]:
                return self._send(200, overview())
            if len(parts) == 3 and parts[:2] == ["api", "migrations"]:
                st = _load(parts[2]) if parts[2] in State.all() else None
                return self._send(200, detail(st)) if st else self._send(404, {"error": "no such migration"})
            if len(parts) == 3 and parts[:2] == ["api", "groups"]:
                from .group import Group
                g = group_doc(parts[2], full=True) if parts[2] in Group.all() else None
                return self._send(200, g) if g else self._send(404, {"error": "no such group"})
            if len(parts) == 3 and parts[:2] == ["api", "rehearsals"]:
                from .rehearsal import Rehearsal
                r = rehearsal_doc(parts[2], full=True) if parts[2] in Rehearsal.all() else None
                return self._send(200, r) if r else self._send(404, {"error": "no such rehearsal"})
            if len(parts) == 5 and parts[:2] == ["api", "groups"] and parts[3] == "log":
                from .group import Group, groups_dir
                if parts[2] not in Group.all():
                    return self._send(404, {"error": "no such group"})
                g = json.load(open(os.path.join(groups_dir(), parts[2], "group.json")))
                if parts[4] not in [x["vm"] for x in g["members"]]:      # never a path from the URL
                    return self._send(404, {"error": "no such member"})
                n = max(1, min(2000, int(q.get("lines", 200)) if str(q.get("lines", "200")).isdigit() else 200))
                return self._send(200, {"lines": tail(os.path.join(groups_dir(), parts[2], "logs",
                                                                   f"{parts[4]}.log"), n)})
        except (OSError, ValueError, KeyError) as e:
            return self._send(500, {"error": str(e)})
        return self._send(404, {"error": "not found"})

    def _readonly(self):
        self._send(405, {"error": "read-only"}, extra=[("Allow", "GET, HEAD")])

    do_POST = do_PUT = do_DELETE = do_PATCH = _readonly

    def log_message(self, fmt, *args):
        if os.environ.get("EXO_V2V_WEB_LOG"):
            sys.stderr.write(f"web: {self.address_string()} {fmt % args}\n")


def cmd_web(a):
    host, _, port = a.listen.rpartition(":")
    host = host.strip("[]") or "127.0.0.1"
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    token = os.environ.get("EXO_V2V_WEB_TOKEN")
    if a.token_file:
        token = open(a.token_file).read().strip()
    if not loopback and not token:
        die(f"{host} is not a loopback address: set a token (--token-file or EXO_V2V_WEB_TOKEN), or keep "
            "127.0.0.1 and use an SSH port forward")
    Handler.token = token or None
    srv = ThreadingHTTPServer((host, int(port)), Handler)
    print(f"exo-v2v web (read-only) on http://{a.listen}/ over {home()}"
          + (" — token required (/login?token=...)" if token else ""), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def add_parser(sub):
    s = sub.add_parser("web", help="read-only status page of the migrations and groups")
    s.add_argument("--listen", default="127.0.0.1:8780", help="ADDRESS:PORT (default 127.0.0.1:8780)")
    s.add_argument("--token-file", help="file holding the access token (required off loopback)")
    s.set_defaults(fn=cmd_web)
