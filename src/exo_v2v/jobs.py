"""Jobs: the actions the web page (and its API) may start, each run as its own
`exo-v2v` command, recorded in $EXO_V2V_HOME/jobs/ID/ (job.json, log).

Only these actions exist; nothing else is reachable, and cutover and rollback
never are (they stay on the command line):

  sync               one replication cycle of a migration, or a round of a group
                     (group: consistent / quiesce)
  rehearse           airgapped copies of a migration or a group (ttl; group:
                     consistent / quiesce)
  rehearsal-destroy  a rehearsal's copies, relay and networks

$EXO_V2V_HOME/config.yaml:

  actions:
    enabled: false                 # nothing can be started until this is true
    web: [sync, rehearse, rehearsal-destroy]
    max_running: 4

As root with systemd (the appliance), a job is a transient unit
(exo-v2v-job-ID) reading the credentials of /etc/exo-v2v/exo-v2v.env, so it
outlives the web server; otherwise a detached process with this environment.
`exo-v2v job list|show|log|cancel` manage them from the command line.
"""
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time

from . import alerts
from .state import State, home
from .util import die, write_json_atomic

ACTIONS = ("sync", "rehearse", "rehearsal-destroy")
KINDS = {"sync": ("migration", "group"), "rehearse": ("migration", "group"), "rehearsal-destroy": ("rehearsal",)}
PARAMS = {"sync": {"consistent": bool, "quiesce": bool},
          "rehearse": {"ttl": str, "consistent": bool, "quiesce": bool},
          "rehearsal-destroy": {}}
ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")
ENV_FILE = "/etc/exo-v2v/exo-v2v.env"
LIVE = ("queued", "running")


class JobError(Exception):
    """HTTP-like code (400 bad request, 403 not allowed, 404 no target, 409 busy) + message."""

    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def jobs_dir():
    return os.path.join(home(), "jobs")


def policy():
    """config.yaml actions: -> {"enabled", "web": [...], "max_running"}."""
    import yaml
    try:
        with open(os.path.join(home(), "config.yaml")) as f:
            raw = (yaml.safe_load(f) or {}).get("actions") or {}
    except FileNotFoundError:
        raw = {}
    web = [x for x in raw.get("web", list(ACTIONS)) if x in ACTIONS]
    return {"enabled": bool(raw.get("enabled", False)), "web": web, "max_running": int(raw.get("max_running", 4))}


def argv_of(action, kind, target, params):
    if action == "sync":
        return (["sync", target] if kind == "migration" else ["group", "sync", target]
                + (["--consistent"] if params.get("consistent") else [])
                + (["--quiesce"] if params.get("quiesce") and params.get("consistent") else []))
    if action == "rehearse":
        args = ["rehearse", target] if kind == "migration" else ["group", "rehearse", target]
        if params.get("ttl"):
            args += ["--ttl", params["ttl"]]
        if kind == "group" and params.get("consistent"):
            args += ["--consistent"] + (["--quiesce"] if params.get("quiesce") else [])
        return args
    return ["rehearsal", "destroy", target]


def targets(kind):
    if kind == "migration":
        return State.all()
    if kind == "group":
        from .group import Group
        return Group.all()
    from .rehearsal import Rehearsal
    return Rehearsal.all()


def locks_of(kind, target):
    """The lock files the job will need."""
    if kind == "migration":
        return [os.path.join(home(), target, ".lock")]
    if kind == "group":
        from .group import Group
        g = Group.load(target)
        return [os.path.join(g.dir, ".lock")] + [os.path.join(home(), x["migration"], ".lock") for x in g.members()]
    from .rehearsal import Rehearsal
    return [os.path.join(Rehearsal(target).dir, ".lock")]


def load(job_id):
    if not ID_RE.match(job_id or ""):
        raise JobError(404, "no such job")
    try:
        with open(os.path.join(jobs_dir(), job_id, "job.json")) as f:
            j = json.load(f)
    except (FileNotFoundError, ValueError):
        raise JobError(404, "no such job") from None
    if j["state"] in LIVE and j.get("pid") and not _alive(j["pid"]) and not _unit_active(j):
        j["state"] = "lost"                     # computed, never written: the record shows what it knew
    return j


def _alive(pid):
    try:
        os.kill(pid, 0)
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return b"exo_v2v" in f.read()
    except (OSError, ProcessLookupError):
        return False


def _unit_active(j):
    if not j.get("unit") or not shutil.which("systemctl"):
        return False
    r = subprocess.run(["systemctl", "is-active", j["unit"]], capture_output=True, text=True)
    return r.stdout.strip() in ("active", "activating")


def save(j):
    write_json_atomic(os.path.join(jobs_dir(), j["id"], "job.json"), j)


def all_jobs(target=None, limit=50):
    d = jobs_dir()
    ids = sorted((x for x in os.listdir(d) if ID_RE.match(x)), reverse=True) if os.path.isdir(d) else []
    out = []
    for i in ids:
        try:
            j = load(i)
        except JobError:
            continue
        if target is None or j["target"] == target:
            out.append(j)
        if len(out) >= limit:
            break
    return out


def start(action, kind, target, params=None, source="cli", requester=None, allowed=None):
    """Validates and launches a job; returns its record. allowed: the actions the
    caller may start (policy), None for the command line."""
    params = dict(params or {})
    if action not in ACTIONS:
        raise JobError(400, f"unknown action {action!r} ({', '.join(ACTIONS)})")
    if allowed is not None and action not in allowed:
        raise JobError(403, f"action {action} is not enabled (config.yaml actions:)")
    if kind not in KINDS[action]:
        raise JobError(400, f"{action} takes a {' or '.join(KINDS[action])}")
    for k, v in params.items():
        typ = PARAMS[action].get(k)
        if typ is None or not isinstance(v, typ) or (typ is str and not re.fullmatch(r"\d+[smh]?", v)):
            raise JobError(400, f"parameter {k}={v!r} not accepted for {action}")
    if target not in targets(kind):
        raise JobError(404, f"no {kind} {target!r}")
    if kind == "migration" and State.load(target).data.get("group") and action == "sync":
        raise JobError(400, f"{target} belongs to group {State.load(target)['group']}: sync the group")
    from .group import lock_holder
    for path in locks_of(kind, target):
        h = lock_holder(path)
        if h:
            raise JobError(409, f"busy: {h['what']} (pid {h['pid']})")
    live = [j for j in all_jobs(limit=200) if j["state"] in LIVE]
    if any(j["target"] == target for j in live):
        raise JobError(409, f"a job on {target} is already running")
    if len(live) >= policy()["max_running"]:
        raise JobError(409, f"{len(live)} jobs running (actions.max_running)")
    j = {"id": time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3), "action": action, "kind": kind,
         "target": target, "params": params, "argv": argv_of(action, kind, target, params), "source": source,
         "requester": requester, "state": "queued", "created": time.time()}
    os.makedirs(os.path.join(jobs_dir(), j["id"]), mode=0o700)
    save(j)
    launch(j)
    return j


def launch(j):
    cmd = [sys.executable, "-m", "exo_v2v", "job", "run", j["id"]]
    if os.geteuid() == 0 and shutil.which("systemd-run") and os.path.exists("/run/systemd/system"):
        if not os.path.exists(ENV_FILE):
            j.update(state="failed", error=f"no {ENV_FILE} (`exo-v2v service install --write-env` writes it)")
            save(j)
            raise JobError(403, j["error"])
        j["unit"] = f"exo-v2v-job-{j['id']}"
        save(j)
        r = subprocess.run(["systemd-run", "--unit", j["unit"], "--collect", "--quiet",
                            "-p", f"EnvironmentFile={ENV_FILE}", "-E", "HOME=/root", "-E", f"EXO_V2V_HOME={home()}",
                            "-E", f"PATH={os.environ.get('PATH', '/usr/bin:/bin')}", *cmd],
                           capture_output=True, text=True)
        if r.returncode:
            j.update(state="failed", error=(r.stderr or r.stdout).strip()[-300:])
            save(j)
            raise JobError(500, f"systemd-run: {j['error']}")
    else:
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)


def run(job_id):
    """`exo-v2v job run ID` (the job's own process): runs the command, records the result."""
    j = load(job_id)
    if j["state"] != "queued":
        die(f"job {job_id} is {j['state']}")
    log_path = os.path.join(jobs_dir(), job_id, "log")
    j.update(state="running", pid=os.getpid(), started=time.time())
    save(j)
    who = f" ({j['source']}" + (f", {j['requester']}" if j.get("requester") else "") + ")"
    alerts.send("job-started", f"{' '.join(j['argv'])}{who}", job=job_id, source=j["source"])
    with open(log_path, "a") as log:
        log.write(f"==== {time.strftime('%Y-%m-%d %H:%M:%S')} exo-v2v {' '.join(j['argv'])}{who}\n")
        log.flush()
        child = subprocess.Popen([sys.executable, "-m", "exo_v2v", *j["argv"]], stdout=log, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, start_new_session=True)
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda s, _f: os.killpg(child.pid, signal.SIGINT))
        rc = child.wait()
    lines = open(log_path, errors="replace").read().splitlines()
    err = next((x for x in reversed(lines) if x.startswith("error:")), None)
    j = load(job_id)
    j.update(state="succeeded" if rc == 0 else "failed", rc=rc, ended=time.time(),
             error=None if rc == 0 else (err or (lines[-1] if lines else f"exit {rc}")))
    save(j)
    alerts.send("job-finished" if rc == 0 else "job-failed",
                f"{' '.join(j['argv'])}: " + ("done" if rc == 0 else j["error"]) + f" in {round(j['ended'] - j['started'])}s",
                job=job_id, source=j["source"])
    return rc


def cancel(job_id):
    """SIGINT to the job: a sync finishes the cycle or round in progress, then stops; a
    rehearsal stops where it is (resumable, or destroyed)."""
    j = load(job_id)
    if j["state"] not in LIVE:
        raise JobError(409, f"job {job_id} is {j['state']}")
    if j["action"] == "rehearsal-destroy":
        raise JobError(409, "a rehearsal destroy is not interrupted")
    if j.get("pid") and _alive(j["pid"]):
        os.kill(j["pid"], signal.SIGINT)
    elif j.get("unit"):
        subprocess.run(["systemctl", "kill", "--signal=SIGINT", j["unit"]], capture_output=True)
    j["cancel_requested"] = time.time()
    save(j)
    return j


def tail(job_id, lines=200):
    load(job_id)
    from .web import tail as t
    return t(os.path.join(jobs_dir(), job_id, "log"), lines)


def cmd_job(a):
    try:
        if a.job_cmd == "run":
            sys.exit(run(a.id))
        if a.job_cmd == "list":
            for j in all_jobs(a.target):
                print(f"{j['id']}  {j['state']:<9} {' '.join(j['argv'])}  ({j['source']}"
                      + (f", {j['requester']}" if j.get("requester") else "") + ")"
                      + (f"  {j['error']}" if j.get("error") else ""))
        elif a.job_cmd == "show":
            print(json.dumps(load(a.id), indent=2))
        elif a.job_cmd == "log":
            print("\n".join(tail(a.id, a.lines)))
        elif a.job_cmd == "cancel":
            cancel(a.id)
            print(f"job {a.id}: interrupt sent")
        elif a.job_cmd == "start":
            j = start(a.action, a.kind, a.target, source="cli")
            print(f"job {j['id']}: {' '.join(j['argv'])}")
    except JobError as e:
        die(str(e))


def add_parser(sub):
    import argparse
    s = sub.add_parser("job", help="actions started from the web page (list, show, log, cancel)")
    js = s.add_subparsers(dest="job_cmd", required=True)
    x = js.add_parser("list"); x.add_argument("--target"); x.set_defaults(fn=cmd_job)
    x = js.add_parser("show"); x.add_argument("id"); x.set_defaults(fn=cmd_job)
    x = js.add_parser("log"); x.add_argument("id"); x.add_argument("--lines", type=int, default=200)
    x.set_defaults(fn=cmd_job)
    x = js.add_parser("cancel"); x.add_argument("id"); x.set_defaults(fn=cmd_job)
    x = js.add_parser("start", help="start a job as the web page would (for scripts)")
    x.add_argument("action", choices=ACTIONS); x.add_argument("kind", choices=("migration", "group", "rehearsal"))
    x.add_argument("target"); x.set_defaults(fn=cmd_job)
    x = js.add_parser("run", help=argparse.SUPPRESS); x.add_argument("id"); x.set_defaults(fn=cmd_job)
