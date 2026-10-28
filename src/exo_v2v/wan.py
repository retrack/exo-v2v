"""WAN controls: one bandwidth limit for the whole appliance, with a weekly schedule,
and zlib compression of the copy stream.

$EXO_V2V_HOME/config.yaml:

  wan:
    limit: 200Mbit                 # default rate; off = no limit
    schedule:                      # first matching rule wins (appliance's local time)
      - {days: mon-fri, hours: "08:00-18:00", limit: 50Mbit}
    burst: 2s                      # what an idle period lets a sender catch up
    final: off                     # final (cutover) cycles never wait: "off", or "limit"
    compress: auto                 # auto: only while a limit of 50 MiB/s or less applies; on; off
    level: 1                       # zlib level

Every sender process (single syncs, group members, timers) takes its bytes from one
token bucket kept in a small file under /run/lock, so the limit holds for the
appliance as a whole and an idle sender leaves its share to the others. A sender
re-reads the configuration every 15 seconds: a schedule change takes effect in the
middle of a long copy. `--bwlimit RATE|off` on a run overrides the configured rate for
the whole appliance while that run (and its group's children) lives.
"""
import atexit
import datetime
import fcntl
import json
import os
import re
import time

from .state import home

BUCKET = os.environ.get("EXO_V2V_WAN_BUCKET", "/run/lock/exo-v2v-wan.bucket")
DEFAULTS = {"limit": None, "schedule": [], "burst": "2s", "final": "off", "compress": "auto",
            "level": 1, "timezone": None}
AUTO_MAX = 50 << 20            # compress: auto compresses at or below this rate (bytes/s)
REFRESH_S = 15
DEBT_S = 30                    # a sender never owes more than this many seconds of the rate
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
PREFIX = {"": 1, "k": 1e3, "m": 1e6, "g": 1e9, "t": 1e12}


class WanError(ValueError):
    pass


def rate(v):
    """Bytes per second from '50Mbit', '200 Mbps', '20MiB/s', '1Gbit/s', '500kB/s';
    None for off/none/unlimited."""
    if v is None or v is False or str(v).strip().lower() in ("off", "none", "unlimited", "no"):
        return None                        # YAML reads a bare `off` as false
    m = re.fullmatch(r"\s*([\d.]+)\s*([kKmMgGtT]?)(i?)(bit|bps|b|B|Bps)(/s)?\s*", str(v))
    if not m:
        raise WanError(f"rate {v!r}: use e.g. 50Mbit, 200Mbps, 20MiB/s (or off)")
    num, pre, binary, unit, _ = m.groups()
    mult = 1024 ** "kmgt".index(pre.lower()) * 1024 if binary and pre else PREFIX[pre.lower()]
    bps = float(num) * mult / (8 if unit in ("bit", "bps", "b") else 1)
    if bps <= 0:
        raise WanError(f"rate {v!r}: 0 would stop copies in the middle with their snapshots "
                       "open; schedule the timers instead")
    return bps


def human_rate(bps):
    if bps is None:
        return "unlimited"
    return f"{bps * 8 / 1e6:.0f} Mbit/s ({bps / 2**20:.1f} MiB/s)"


def _days(spec):
    if spec is None:
        return set(range(7))
    out = set()
    for part in (spec if isinstance(spec, list) else str(spec).split(",")):
        part = str(part).strip().lower()
        a, _, b = part.partition("-")
        try:
            i, j = DAYS.index(a[:3]), DAYS.index((b or a)[:3])
        except ValueError:
            raise WanError(f"days {spec!r}: use mon-fri, sat,sun, ...") from None
        out |= {(i + k) % 7 for k in range((j - i) % 7 + 1)}
    return out


def _hours(spec):
    if spec is None:
        return 0, 24 * 60
    m = re.fullmatch(r"\s*(\d{1,2}):?(\d{2})?\s*-\s*(\d{1,2}):?(\d{2})?\s*", str(spec))
    if not m:
        raise WanError(f"hours {spec!r}: use e.g. 08:00-18:00")
    h1, m1, h2, m2 = (int(x or 0) for x in m.groups())
    a, b = h1 * 60 + m1, h2 * 60 + m2
    if a > 24 * 60 or b > 24 * 60 or a == b:
        raise WanError(f"hours {spec!r}: not a time window")
    return a, b


def parse(raw):
    """The wan: section, validated: {limit, schedule: [(days, (start, end), bps, label)], ...}."""
    from .group import duration
    c = {**DEFAULTS, **(raw or {})}
    unknown = set(c) - set(DEFAULTS)
    if unknown:
        raise WanError(f"unknown wan keys {sorted(unknown)}")
    yn = {True: "on", False: "off"}                 # YAML: bare on/off are booleans
    out = {"limit": rate(c["limit"]), "schedule": [], "burst_s": duration(c["burst"]),
           "final": {True: "limit", False: "off"}.get(c["final"], str(c["final"]).lower()),
           "compress": yn.get(c["compress"], str(c["compress"]).lower()),
           "level": int(c["level"]), "timezone": c["timezone"]}
    if out["final"] not in ("off", "limit"):
        raise WanError("final: off (final cycles never wait) or limit")
    if out["compress"] not in ("auto", "on", "off", "true", "false"):
        raise WanError("compress: auto, on or off")
    out["compress"] = {"true": "on", "false": "off"}.get(out["compress"], out["compress"])
    if not 0 <= out["level"] <= 9:
        raise WanError("level: 0-9")
    for r in c["schedule"] or []:
        if not isinstance(r, dict) or "limit" not in r or set(r) - {"days", "hours", "limit"}:
            raise WanError(f"schedule rule {r!r}: {{days, hours, limit}}")
        label = " ".join(str(r[k]) for k in ("days", "hours") if r.get(k)) or "always"
        out["schedule"].append((_days(r.get("days")), _hours(r.get("hours")), rate(r["limit"]), label))
    return out


def config():
    """parse() of config.yaml's wan: section (defaults without one)."""
    import yaml
    path = os.path.join(home(), "config.yaml")
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        data = {}
    try:
        return parse(data.get("wan"))
    except (WanError, ValueError, TypeError) as e:
        raise WanError(f"{path}: wan: {e}") from None


def now_in(cfg):
    if cfg.get("timezone"):
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo(cfg["timezone"]))
    return datetime.datetime.now()


def _match(rule, t):
    days, (a, b), _bps, _label = rule
    minute = t.hour * 60 + t.minute
    if a < b:
        return t.weekday() in days and a <= minute < b
    # crosses midnight: belongs to the day it starts
    return (t.weekday() in days and minute >= a) or ((t.weekday() - 1) % 7 in days and minute < b)


def limit_at(cfg, t):
    """(bytes/s or None, what set it) at local time t."""
    for rule in cfg["schedule"]:
        if _match(rule, t):
            return rule[2], f"schedule {rule[3]}"
    return cfg["limit"], "limit" if cfg["limit"] else "no limit"


def next_change(cfg, t):
    """When the rate changes next (minute resolution, within a week), or None."""
    cur = limit_at(cfg, t)
    t = t.replace(second=0, microsecond=0)
    for k in range(1, 7 * 24 * 60 + 1):
        u = t + datetime.timedelta(minutes=k)
        if limit_at(cfg, u) != cur:
            return u
    return None


def _alive(owner):
    pid, start = owner
    return start is not None and _starttime(pid) == start


def _starttime(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            return int(f.read().rsplit(")", 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        return None


class Bucket:
    """The shared token bucket: {"v": 1, "t": monotonic, "tokens": bytes, "bytes": total,
    "override": {"bps": float|None, "owners": [[pid, starttime], ...]}}. CLOCK_MONOTONIC
    is the same for every process of the appliance."""

    def __init__(self, path=None, clock=time.monotonic):
        self.path, self.clock = path or BUCKET, clock

    def _update(self, fn, read_only_ok=False):
        try:
            f = open(self.path, "a+")
        except OSError:
            if read_only_ok and os.path.exists(self.path):
                f = open(self.path)             # the appliance's bucket, read by a user (wan status)
            else:                               # not root (http transport, tests): per user
                self.path = os.path.join(home(), ".wan.bucket")
                os.makedirs(home(), exist_ok=True)
                f = open(self.path, "a+")
        with f:
            fcntl.flock(f, fcntl.LOCK_EX if f.writable() else fcntl.LOCK_SH)
            f.seek(0)
            try:
                d = json.loads(f.read() or "{}")
                assert isinstance(d, dict) and d.get("v") == 1
            except (ValueError, AssertionError):
                d = {"v": 1, "t": self.clock(), "tokens": 0.0, "bytes": 0}
            out = fn(d)
            if f.writable():
                f.seek(0)
                f.truncate()
                f.write(json.dumps(d))
                f.flush()
            return out

    def take(self, n, bps, burst_s):
        """Take n bytes at bps; returns the seconds the caller has to wait."""
        def fn(d):
            now = self.clock()
            tokens = min(bps * burst_s, d["tokens"] + max(0.0, now - d["t"]) * bps)
            tokens = max(tokens - n, -bps * DEBT_S)
            d.update(t=now, tokens=tokens, bytes=d.get("bytes", 0) + n)
            return max(0.0, -tokens / bps)
        return self._update(fn)

    def override(self):
        """(active, bytes/s or None) of a live --bwlimit run."""
        def fn(d):
            ov = d.get("override") or {}
            live = [o for o in ov.get("owners", []) if _alive(o)]
            if live != ov.get("owners"):
                d["override"] = {**ov, "owners": live} if live else None
            return (bool(live), ov.get("bps") if live else None)
        return self._update(fn, read_only_ok=True)

    def claim(self, bps, pid=None):
        pid = pid or os.getpid()

        def fn(d):
            ov = d.get("override") or {}
            owners = [o for o in ov.get("owners", []) if _alive(o) and o[0] != pid]
            if owners and ov.get("bps") != bps:
                owners = []                     # the latest --bwlimit wins
            d["override"] = {"bps": bps, "owners": owners + [[pid, _starttime(pid)]]}
        self._update(fn)

    def release(self, pid=None):
        pid = pid or os.getpid()

        def fn(d):
            ov = d.get("override") or {}
            owners = [o for o in ov.get("owners", []) if o[0] != pid]
            d["override"] = {**ov, "owners": owners} if owners else None
        self._update(fn)


def claim_override(value):
    """--bwlimit RATE|off: the appliance-wide rate while this process lives."""
    bps = rate(value)
    b = Bucket()
    b.claim(bps)
    atexit.register(b.release)
    return bps


class Pacer:
    """One sender's view of the limit: take(n) before putting n bytes on the wire."""

    def __init__(self, final=False, cfg=None, bucket=None, clock=time.monotonic, sleep=time.sleep):
        self.final, self.clock, self.sleep = final, clock, sleep
        self.bucket = bucket or Bucket(clock=clock)
        self._cfg = cfg
        self._fixed = cfg is not None
        self._next = 0.0
        self.bps = None
        self.source = "no limit"
        self.wire = 0
        self.throttled_s = 0.0
        self.refresh(force=True)

    def refresh(self, force=False):
        now = self.clock()
        if not force and now < self._next:
            return
        self._next = now + REFRESH_S
        if not self._fixed:
            try:
                self._cfg = config()
            except WanError:
                if self._cfg is None:
                    raise
        active, bps = self.bucket.override()
        if active:
            self.bps, self.source = bps, "--bwlimit"
        else:
            self.bps, self.source = limit_at(self._cfg, now_in(self._cfg))

    @property
    def cfg(self):
        return self._cfg

    def compress(self):
        mode = self._cfg["compress"]
        return mode == "on" or (mode == "auto" and self.bps is not None and self.bps <= AUTO_MAX)

    def slice_size(self):
        if self.bps is None:
            return 4 << 20
        return int(min(4 << 20, max(256 << 10, self.bps / 20)))

    def take(self, n):
        self.wire += n
        self.refresh()
        if self.bps is None:
            return
        wait = self.bucket.take(n, self.bps, self._cfg["burst_s"])
        if wait and not (self.final and self._cfg["final"] == "off"):
            self.sleep(wait)
            self.throttled_s += wait

    def stats(self):
        return {"throttled_s": round(self.throttled_s, 1), "limit_bps": self.bps,
                "limit_source": self.source}


def status(cfg=None, t=None):
    """What `exo-v2v wan status` prints."""
    cfg = cfg or config()
    t = t or now_in(cfg)
    active, bps = Bucket().override()
    cur, src = (bps, "--bwlimit of a running exo-v2v") if active else limit_at(cfg, t)
    lines = [f"now: {human_rate(cur)} ({src})"]
    if not active:
        nxt = next_change(cfg, t)
        if nxt:
            lines.append(f"next change: {nxt:%a %H:%M} -> {human_rate(limit_at(cfg, nxt)[0])}")
    lines.append(f"compression: {cfg['compress']}"
                 + (f" (on at or below {human_rate(AUTO_MAX)})" if cfg["compress"] == "auto" else "")
                 + f", zlib level {cfg['level']}; final cycles: "
                 + ("never wait" if cfg["final"] == "off" else "limited"))
    return lines


def cmd_status(a):
    from .util import die
    try:
        print("\n".join(status()))
    except WanError as e:
        die(str(e))


def add_parser(sub):
    s = sub.add_parser("wan", help="WAN limit and compression (config.yaml wan:)")
    w = s.add_subparsers(dest="wan_cmd", required=True)
    w.add_parser("status", help="the limit now, the next change, compression").set_defaults(fn=cmd_status)
