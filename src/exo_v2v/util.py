import contextlib
import json
import os
import sys
import threading
import time

_QUIET = False
_ctx = threading.local()


def log(msg):
    if not _QUIET:
        print(f"[{time.strftime('%H:%M:%S')}] {getattr(_ctx, 'prefix', '')}{msg}", file=sys.stderr, flush=True)


@contextlib.contextmanager
def log_prefix(prefix):
    """Prefix this thread's log lines (several members worked on in parallel)."""
    old = getattr(_ctx, "prefix", "")
    _ctx.prefix = f"[{prefix}] "
    try:
        yield
    finally:
        _ctx.prefix = old


LAST_ERROR = {"msg": None}       # what die() said last: alerts quote it


def die(msg, code=1):
    LAST_ERROR["msg"] = str(msg)
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def human(n):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024


def gib_ceil(nbytes):
    return -(-nbytes // (1 << 30))


def write_json_atomic(path, obj):
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"   # threads may save the same state
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=1, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def transfer(cycle):
    """', 310 MiB sent (compressed x0.31), throttled 40s' for a cycle's disks, or ''."""
    d = list((cycle or {}).get("disks", {}).values())
    sent, wire = sum(x.get("sent", 0) for x in d), sum(x.get("wire", x.get("sent", 0)) for x in d)
    throttled = sum(x.get("throttled_s", 0) for x in d)
    out = ""
    if any(str(x.get("compression", "")).startswith("zlib") for x in d) and sent:
        out += f", {human(wire)} sent (compressed x{wire / sent:.2f})"
    if throttled >= 1:
        out += f", throttled {throttled:.0f}s"
    return out

