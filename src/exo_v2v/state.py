"""Per-migration state: ~/.exo-v2v/<name>/state.json (+ ssh key, logs).

Every command reads and updates it, so each step is resumable and `status`
can always say where a migration stands.
"""
import json
import os
import re
import time

from .util import write_json_atomic

PHASES = ["planned", "receiver-creating", "receiver-ready", "syncing", "cutover", "converted",
          "done", "rolled-back", "cleaned"]


RESERVED = {"groups", "rehearsals", "jobs", "config.yaml", "alerts.json"}   # entries of home() itself
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")


class BadName(ValueError):
    pass


def check_name(name):
    if not NAME_RE.match(name) or name in RESERVED:
        raise BadName(f"invalid migration name {name!r}: letters, digits, '.', '_', '-' "
                              f"(and not {', '.join(sorted(RESERVED))})")


def home():
    return os.environ.get("EXO_V2V_HOME", os.path.expanduser("~/.exo-v2v"))


class State:
    def __init__(self, name):
        self.name = name
        self.dir = os.path.join(home(), name)
        self.path = os.path.join(self.dir, "state.json")
        self.data = {}

    @classmethod
    def new(cls, name, plan):
        check_name(name)
        st = cls(name)
        if os.path.exists(st.path):
            raise FileExistsError(f"migration {name!r} already exists ({st.path})")
        os.makedirs(st.dir, mode=0o700, exist_ok=True)
        st.data = {"name": name, "created": time.time(), "phase": "planned", "plan": plan,
                   "receiver": {}, "cycles": [], "events": []}
        st.save()
        return st

    @classmethod
    def load(cls, name):
        st = cls(name)
        if not os.path.exists(st.path):
            raise FileNotFoundError(f"no migration {name!r} (looked in {st.path})")
        st.data = json.load(open(st.path))
        return st

    @staticmethod
    def all():
        h = home()
        return sorted(d for d in os.listdir(h) if os.path.exists(os.path.join(h, d, "state.json"))) \
            if os.path.isdir(h) else []

    def save(self):
        write_json_atomic(self.path, self.data)

    def event(self, msg, **kw):
        self.data["events"].append({"t": time.time(), "msg": msg, **kw})
        self.save()

    def set_phase(self, phase):
        assert phase in PHASES, phase
        self.data["phase"] = phase
        self.event(f"phase -> {phase}")

    @property
    def key_path(self):
        return os.path.join(self.dir, "id_ed25519")

    def __getitem__(self, k):
        return self.data[k]
