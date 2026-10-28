"""Web status page: `exo-v2v web [--listen 127.0.0.1:8780]`.

One page and a small JSON API over the state files (~/.exo-v2v). Reading never
writes a state file, never takes a lock (running commands are found by the pid
their lock file records) and needs no Exoscale or vSphere credentials.
Loopback by default: reach it with `ssh -L 8780:127.0.0.1:8780 appliance`.
Any other address needs a token (--token-file FILE or EXO_V2V_WEB_TOKEN);
sign in once on the page (or POST /login {"token"}; GET /login?token= still works).

Actions (jobs.py: a sync cycle, a rehearsal, destroying a rehearsal - never a
cutover or a rollback) need `actions: {enabled: true}` in config.yaml AND a
token, on loopback too. Writes take JSON with the header `X-Exo-V2V: 1`.

  GET  /                                  the page
  GET  /api/overview                      groups and migrations (summaries)
  GET  /api/migrations/NAME               one migration: plan, cycles, cutover steps, events
  GET  /api/groups/NAME                   one group: members by tier, cutover, events
  GET  /api/groups/NAME/log/VM?lines=N    the tail of a member's log
  GET  /api/rehearsals/NAME               one rehearsal: copies, relay, steps, report
  GET  /api/policy                        the actions this page may start
  GET  /api/jobs[?target=NAME]            recent jobs;  GET /api/jobs/ID[/log]
  POST /api/jobs                          {"action", "kind", "target", "params"} -> 202 job
  POST /api/jobs/ID/cancel                interrupt a sync or a rehearsal
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

from . import __version__, jobs
from .state import State, home
from .util import die

MAX_BODY = 16 * 1024
DNS_KEYS = ("fqdn", "old", "new", "ttl", "old_ttl", "switched", "served_s", "restored")

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
        "elastic_ip": (r.get("eip") or {}).get("ip"),
        "dns": [{k: d.get(k) for k in DNS_KEYS} for d in st.data.get("dns") or []],
        "disks": len(p.get("disks", [])), "bytes": sum(d.get("capacity", 0) for d in p.get("disks", [])),
        "cycles": len(st["cycles"]),
        "last_cycle": {"t": last.get("t"), "seconds": last.get("seconds"), "final": last.get("final"),
                       "covered": sum(d.get("covered", 0) for d in last.get("disks", {}).values()),
                       "sent": sum(d.get("sent", 0) for d in last.get("disks", {}).values()),
                       "wire": sum(d.get("wire", d.get("sent", 0)) for d in last.get("disks", {}).values()),
                       "throttled_s": round(sum(d.get("throttled_s", 0) for d in last.get("disks", {}).values()), 1),
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
           "dns": [{k: d.get(k) for k in DNS_KEYS + ("migration",)} for d in g.get("dns") or []],
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
    try:
        from . import wan
        cfg = wan.config()
        bps, src = wan.limit_at(cfg, wan.now_in(cfg))
        wan_now = {"limit_bps": bps, "source": src, "compress": cfg["compress"]}
    except Exception as e:            # same: a broken wan: section shows as such
        wan_now = {"error": str(e)}
    return {"host": socket.gethostname(), "version": __version__, "now": time.time(), "home": home(),
            "lag_alert_s": lag, "wan": wan_now, "jobs": jobs.all_jobs(limit=8),
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

    def _host_ok(self):
        """Without a token only loopback names are served: a page on another site
        whose name resolves to 127.0.0.1 (DNS rebinding) cannot read it."""
        if self.token:
            return True
        host = (self.headers.get("Host") or "").lower()
        host = host[1:].split("]")[0] if host.startswith("[") else host.split(":")[0]
        if host == "localhost":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def _write_ok(self):
        """(ok, code, message) for a request that changes something."""
        if not self.token:
            return False, 403, "actions need a token (exo-v2v web --token-file), on loopback too"
        if not self._authorized():
            return False, 401, "token required"
        if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json" \
                or self.headers.get("X-Exo-V2V") != "1":
            return False, 400, "JSON with the header X-Exo-V2V: 1"
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != self.headers.get("Host"):
            return False, 403, "cross-origin request"
        if self.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
            return False, 403, "cross-site request"
        return True, 200, ""

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("body too large")
        d = json.loads(self.rfile.read(n) or b"{}")
        if not isinstance(d, dict):
            raise ValueError("a JSON object")
        return d

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if not self._host_ok():
            return self._send(421, "use 127.0.0.1 or localhost", "text/plain")
        if u.path == "/login":
            if self.token and hmac.compare_digest(q.get("token", ""), self.token):
                return self._send(303, "", "text/plain", [("Location", "/"), (
                    "Set-Cookie", f"v2v_token={quote(self.token)}; HttpOnly; SameSite=Strict; Path=/")])
            return self._send(403, "bad token", "text/plain")
        parts = [p for p in u.path.split("/") if p]
        if not parts:                       # the page holds no data: it asks for the token itself
            return self._send(200, open(PAGE, "rb").read(), "text/html")
        if not self._authorized():
            return self._send(401, "token required: sign in on the page", "text/plain")
        try:
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
            if parts == ["api", "policy"]:
                pol = jobs.policy()
                return self._send(200, {"actions": pol["web"] if pol["enabled"] and self.token else [],
                                        "enabled": pol["enabled"], "token": bool(self.token)})
            if parts == ["api", "jobs"]:
                return self._send(200, {"jobs": jobs.all_jobs(q.get("target"))})
            if len(parts) in (3, 4) and parts[:2] == ["api", "jobs"]:
                try:
                    if len(parts) == 3:
                        return self._send(200, jobs.load(parts[2]))
                    if parts[3] == "log":
                        n = max(1, min(2000, int(q.get("lines", 200)) if str(q.get("lines", "200")).isdigit() else 200))
                        return self._send(200, {"lines": jobs.tail(parts[2], n)})
                except jobs.JobError as e:
                    return self._send(e.code, {"error": str(e)})
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

    def do_POST(self):
        u = urlparse(self.path)
        parts = [p for p in u.path.split("/") if p]
        if not self._host_ok():
            return self._send(421, "use 127.0.0.1 or localhost", "text/plain")
        if parts == ["login"]:
            try:
                tok = str(self._body().get("token", ""))
            except ValueError:
                tok = ""
            if self.token and hmac.compare_digest(tok, self.token):
                return self._send(200, {"ok": True}, extra=[(
                    "Set-Cookie", f"v2v_token={quote(self.token)}; HttpOnly; SameSite=Strict; Path=/")])
            return self._send(403, {"error": "bad token"})
        if parts[:2] != ["api", "jobs"] or len(parts) not in (2, 4) or (len(parts) == 4 and parts[3] != "cancel"):
            return self._send(405, {"error": "not writable"}, extra=[("Allow", "GET, HEAD")])
        ok, code, msg = self._write_ok()
        if not ok:
            return self._send(code, {"error": msg})
        try:
            if len(parts) == 4:
                return self._send(200, jobs.cancel(parts[2]))
            pol = jobs.policy()
            if not pol["enabled"]:
                return self._send(403, {"error": "actions are not enabled (config.yaml actions: enabled: true)"})
            b = self._body()
            j = jobs.start(str(b.get("action")), str(b.get("kind")), str(b.get("target")), b.get("params") or {},
                           source="web", requester=self.client_address[0], allowed=pol["web"])
            return self._send(202, j)
        except jobs.JobError as e:
            return self._send(e.code, {"error": str(e)})
        except (ValueError, TypeError, AttributeError) as e:
            return self._send(400, {"error": str(e)})

    def _readonly(self):
        self._send(405, {"error": "read-only"}, extra=[("Allow", "GET, HEAD, POST")])

    do_PUT = do_DELETE = do_PATCH = _readonly

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
    pol = jobs.policy()
    print(f"exo-v2v web on http://{a.listen}/ over {home()}"
          + (" — token required" if token else "")
          + (f"; actions: {', '.join(pol['web'])}" if pol["enabled"] and token else
             "; read-only" + (" (actions need a token)" if pol["enabled"] else "")), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def add_parser(sub):
    s = sub.add_parser("web", help="status page of the migrations and groups (actions: config.yaml)")
    s.add_argument("--listen", default="127.0.0.1:8780", help="ADDRESS:PORT (default 127.0.0.1:8780)")
    s.add_argument("--token-file", help="file holding the access token (required off loopback)")
    s.set_defaults(fn=cmd_web)
