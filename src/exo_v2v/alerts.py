"""Alerts: a webhook for what needs a person - a migration whose cycles keep
failing or that lags behind, a receiver that restarted, a cutover that finished
or failed, a rehearsal destroyed at the end of its TTL. The payload is JSON with
a `text` field, which Slack, Mattermost and Teams incoming webhooks display.

$EXO_V2V_HOME/config.yaml:

  alerts:
    webhook: https://hooks.slack.com/services/...
    lag: 4h          # a syncing migration whose last good cycle is older
    failures: 2      # failed cycles in a row

`exo-v2v watch` (a timer: `exo-v2v service install --watch`) checks the lag and
destroys expired rehearsals; `sync` and the cutovers alert by themselves.
Without a webhook, alerts are only logged.
"""
import json
import os
import socket
import time
import urllib.request

from .state import State, home
from .util import LAST_ERROR, die, log, write_json_atomic

DEFAULTS = {"webhook": None, "lag": "4h", "failures": 2}


def config():
    from .group import duration
    import yaml
    path = os.path.join(home(), "config.yaml")
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        data = {}
    a = {**DEFAULTS, **((data or {}).get("alerts") or {})}
    try:
        a["lag_s"], a["failures"] = duration(a["lag"]), int(a["failures"])
    except (ValueError, TypeError) as e:
        die(f"{path}: alerts: {e}")
    return a


def send(event, text, **fields):
    """Log the alert and POST it to the webhook; never raises (an alert that
    cannot be sent must not break a sync or a cutover)."""
    log(f"ALERT {event}: {text}")
    url = config().get("webhook")
    if not url:
        return False
    host = socket.gethostname()
    body = {"text": f"exo-v2v on {host}: {text}", "event": event, "host": host,
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **fields}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15):
            return True
    except Exception as e:                  # noqa: BLE001 - any failure is only logged
        log(f"alert webhook failed: {e}")
        return False


def sync_failed(st, err=None):
    """Count a failed cycle; alert once the streak reaches `failures`."""
    s = st.data.setdefault("sync", {})
    s["failures"] = s.get("failures", 0) + 1
    s["last_error"] = (err or LAST_ERROR["msg"] or "failed")[-500:]
    s["last_error_t"] = time.time()
    if s["failures"] >= config()["failures"] and not s.get("alerted"):
        s["alerted"] = True
        send("sync-failing", f"{st.name}: {s['failures']} cycles failed in a row: {s['last_error']}",
             migration=st.name, group=st.data.get("group"))
    st.save()


def sync_ok(st):
    s = st.data.setdefault("sync", {})
    if s.get("alerted"):
        send("sync-recovered", f"{st.name}: replicating again after {s.get('failures', 0)} failed cycles",
             migration=st.name, group=st.data.get("group"))
    s.update(failures=0, alerted=False, last_ok=time.time())
    st.save()


def lag_of(st, now=None):
    """Seconds since the last good cycle (since creation before the first one)."""
    last = (st.data.get("sync") or {}).get("last_ok") or (st["cycles"][-1]["t"] if st["cycles"] else None) \
        or st.data.get("created", 0)
    return (now or time.time()) - last


def human_age(s):
    s = int(s)
    return f"{s}s" if s < 120 else f"{s // 60} min" if s < 7200 else f"{s // 3600} h {s % 3600 // 60} min"


def check_lag():
    """Alert on migrations that lag (and when they catch up). What was alerted
    lives in alerts.json, not in the migrations' state: a sync may be saving it."""
    cfg, path = config(), os.path.join(home(), "alerts.json")
    try:
        with open(path) as f:
            seen = json.load(f)
    except (FileNotFoundError, ValueError):
        seen = {}
    now, alive = time.time(), set()
    for n in State.all():
        st = State.load(n)
        if st.data.get("rehearsal_of") or st["phase"] not in ("receiver-ready", "syncing"):
            continue
        alive.add(n)
        lag = lag_of(st, now)
        if lag > cfg["lag_s"]:
            if n not in seen:
                down = st["receiver"].get("down")
                send("lag", f"{n}: last good cycle {human_age(lag)} ago (more than {cfg['lag']})"
                     + (f"; receiver down ({down['state']}): exo-v2v recover {n}" if down else ""),
                     migration=n, group=st.data.get("group"), lag_s=int(lag))
                seen[n] = now
        elif seen.pop(n, None):
            send("lag-recovered", f"{n}: replicating again (last good cycle {human_age(lag)} ago)",
                 migration=n, group=st.data.get("group"))
    for n in set(seen) - alive:
        seen.pop(n)
    write_json_atomic(path, seen)


def cmd_watch(a):
    from .rehearsal import cmd_gc
    cmd_gc(a)
    check_lag()


def cmd_test(a):
    cfg = config()
    if not cfg.get("webhook"):
        die(f"no alerts.webhook in {os.path.join(home(), 'config.yaml')}")
    if not send("test", "test alert (exo-v2v alert test)"):
        die("the webhook did not accept the alert (see above)")
    print(f"sent to {cfg['webhook']}")


def add_parser(sub):
    s = sub.add_parser("watch", help="destroy expired rehearsals, alert on lagging migrations (run from a timer)")
    s.set_defaults(fn=cmd_watch)
    s = sub.add_parser("alert", help="alerts (webhook in $EXO_V2V_HOME/config.yaml)")
    s2 = s.add_subparsers(dest="alert_cmd", required=True)
    s2.add_parser("test", help="send a test alert").set_defaults(fn=cmd_test)
