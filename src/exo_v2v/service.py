"""Unattended replication: systemd timers on the appliance.

  exo-v2v service install NAME        [--every 1h]   # `sync NAME`, one cycle per run
  exo-v2v service install --group G   [--every 1h]   # `group sync G`
  exo-v2v service install --watch     [--every 15m]  # `watch`: expired rehearsals, lag alerts
  exo-v2v service list
  exo-v2v service remove NAME | --group G | --watch

A timer fires its next run `--every` after the previous one ended (no overlap;
the migration's lock guards it anyway). The units run as root with the
variables of --env-file (VSPHERE_HOST, VSPHERE_USER, VSPHERE_PASSWORD, and
EXOSCALE_API_KEY/SECRET unless the exo CLI is configured for root): --write-env
creates it from the current environment (mode 600). `cutover`, `group cutover`
and `cleanup` stop the timers of what they move.
"""
import glob
import os
import shlex
import subprocess
import sys

from .state import home
from .util import die, log

UNIT_DIR = os.environ.get("EXO_V2V_UNIT_DIR", "/etc/systemd/system")
ENV_FILE = "/etc/exo-v2v/exo-v2v.env"
ENV_KEYS = ("VSPHERE_HOST", "VSPHERE_USER", "VSPHERE_PASSWORD", "EXOSCALE_API_KEY", "EXOSCALE_API_SECRET",
            "EXOSCALE_ACCOUNT", "EXO_V2V_HOME")
KINDS = {"sync": ("sync", "replication cycle of migration {}"),
         "gsync": ("group sync", "replication round of group {}"),
         "watch": ("watch", "expired rehearsals and lag alerts")}


def unit(kind, name=None):
    return f"exo-v2v-{kind}" + (f"-{name}" if name else "")


def systemctl(*args, check=True):
    r = subprocess.run(["systemctl", *args], capture_output=True, text=True)
    if r.returncode and check:
        die(f"systemctl {' '.join(args)}: {(r.stderr or r.stdout).strip()}")
    return r


def write_env(path):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    env = {k: os.environ[k] for k in ENV_KEYS if os.environ.get(k)}
    env.setdefault("EXO_V2V_HOME", home())
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("".join(f"{k}={shlex.quote(v)}\n" for k, v in env.items()))
    log(f"wrote {path} ({', '.join(env)})")


def units_text(kind, name, every, env_file):
    verb, what = KINDS[kind]
    cmd = [sys.executable, "-m", "exo_v2v", *verb.split(), *([name] if name else [])]
    service = f"""[Unit]
Description=exo-v2v: {what.format(name)}
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
EnvironmentFile={env_file}
Environment={shlex.quote("PATH=" + os.environ.get("PATH", "/usr/sbin:/usr/bin:/sbin:/bin"))}
Environment=EXO_V2V_HOME={home()}
Environment=HOME=/root
ExecStart={" ".join(shlex.quote(c) for c in cmd)}
Nice=5
"""
    timer = f"""[Unit]
Description=exo-v2v: {what.format(name)}, every {every}s

[Timer]
OnBootSec=5min
OnActiveSec=1min
OnUnitInactiveSec={every}s
AccuracySec=30s

[Install]
WantedBy=timers.target
"""
    return service, timer


def install(kind, name, every, env_file, write=False):
    if os.geteuid() != 0:
        die("service install: run as root (sudo -E keeps the environment)")
    if write:
        write_env(env_file)
    elif not os.path.exists(env_file):
        die(f"{env_file} does not exist: --write-env creates it from the current environment, "
            "or write VSPHERE_HOST=..., VSPHERE_USER=..., VSPHERE_PASSWORD=... into it (mode 600)")
    service, timer = units_text(kind, name, every, env_file)
    u = unit(kind, name)
    for ext, text in (("service", service), ("timer", timer)):
        with open(os.path.join(UNIT_DIR, f"{u}.{ext}"), "w") as f:
            f.write(text)
    systemctl("daemon-reload")
    systemctl("enable", "--now", f"{u}.timer")
    log(f"{u}.timer enabled: every {every}s ({KINDS[kind][1].format(name)})")


def remove(kind, name=None, quiet=False):
    u = unit(kind, name)
    path = os.path.join(UNIT_DIR, f"{u}.timer")
    if not os.path.exists(path):
        if not quiet:
            log(f"no {u}.timer")
        return False
    systemctl("disable", "--now", f"{u}.timer", check=False)
    for ext in ("timer", "service"):
        try:
            os.remove(os.path.join(UNIT_DIR, f"{u}.{ext}"))
        except FileNotFoundError:
            pass
    systemctl("daemon-reload", check=False)
    log(f"{u}.timer removed")
    return True


def stop_for(migration=None, group=None):
    """Before a cutover or a cleanup: no more timed cycles of what moves."""
    for kind, n in (("sync", migration), ("gsync", group)):
        if n and os.path.exists(os.path.join(UNIT_DIR, f"{unit(kind, n)}.timer")):
            if os.geteuid() == 0:
                remove(kind, n, quiet=True)
            else:
                log(f"WARNING {unit(kind, n)}.timer is installed: remove it (exo-v2v service remove), as root")


def cmd_install(a):
    from .group import duration
    kind, name = ("gsync", a.group) if a.group else ("watch", None) if a.watch else ("sync", a.name)
    if kind == "sync" and not name:
        die("service install NAME | --group G | --watch")
    if kind == "sync":
        from .state import State
        State.load(name)
    elif kind == "gsync":
        from .group import Group
        Group.load(name)
    every = duration(a.every or ("15m" if kind == "watch" else "1h"))
    install(kind, name, every, a.env_file, a.write_env)


def cmd_remove(a):
    kind, name = ("gsync", a.group) if a.group else ("watch", None) if a.watch else ("sync", a.name)
    if os.geteuid() != 0:
        die("service remove: run as root")
    remove(kind, name)


def cmd_list(a):
    timers = sorted(glob.glob(os.path.join(UNIT_DIR, "exo-v2v-*.timer")))
    if not timers:
        print("no exo-v2v timers")
        return
    for t in timers:
        u = os.path.basename(t)[:-6]
        r = systemctl("show", f"{u}.timer", "-p", "ActiveState", "-p", "NextElapseUSecRealtime",
                      "-p", "LastTriggerUSec", check=False)
        kv = dict(ln.split("=", 1) for ln in r.stdout.splitlines() if "=" in ln)
        s = systemctl("show", f"{u}.service", "-p", "Result", "-p", "ExecMainStatus", check=False)
        sv = dict(ln.split("=", 1) for ln in s.stdout.splitlines() if "=" in ln)
        print(f"{u:<36} {kv.get('ActiveState', '?'):<8} last {kv.get('LastTriggerUSec') or '-':<30} "
              f"next {kv.get('NextElapseUSecRealtime') or '-':<30} result {sv.get('Result', '-')}"
              + (f" (exit {sv['ExecMainStatus']})" if sv.get("ExecMainStatus") not in (None, "0") else ""))


def add_parser(sub):
    s = sub.add_parser("service", help="systemd timers for unattended replication and alerts")
    s2 = s.add_subparsers(dest="service_cmd", required=True)
    i = s2.add_parser("install", help="a timer: sync NAME, group sync G, or watch")
    i.add_argument("name", nargs="?"); i.add_argument("--group"); i.add_argument("--watch", action="store_true")
    i.add_argument("--every", help="interval between runs (default 1h; 15m for --watch)")
    i.add_argument("--env-file", default=ENV_FILE)
    i.add_argument("--write-env", action="store_true", help="write --env-file from the current environment")
    i.set_defaults(fn=cmd_install)
    r = s2.add_parser("remove"); r.add_argument("name", nargs="?"); r.add_argument("--group")
    r.add_argument("--watch", action="store_true"); r.set_defaults(fn=cmd_remove)
    s2.add_parser("list").set_defaults(fn=cmd_list)
