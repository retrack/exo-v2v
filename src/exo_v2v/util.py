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


def die(msg, code=1):
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
