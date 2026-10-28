"""`exo-v2v mcp`: a read-only Model Context Protocol server on stdin/stdout, so an
AI assistant can read where the migrations, groups, rehearsals and jobs stand.
It starts nothing and changes nothing (actions: the web page, jobs.py).

Run on the appliance through SSH, e.g. with Claude Code:

  claude mcp add exo-v2v -- ssh ops@appliance sudo -n exo-v2v mcp

JSON-RPC 2.0, one message per line (MCP stdio transport); the same data as the
web page's API (web.py).
"""
import json
import os
import sys

from . import __version__, jobs, wan, web
from .state import State

VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
ENV_FILE = "/etc/exo-v2v/exo-v2v.env"


def _name(props):
    return {"type": "object", "properties": props, "required": [k for k in props if k != "lines" and k != "target"],
            "additionalProperties": False}


S = {"type": "string"}


def _migration(a):
    if a["name"] not in State.all():
        raise LookupError(f"no migration {a['name']!r} (overview lists them)")
    return web.detail(State.load(a["name"]))


def _group(a):
    from .group import Group
    if a["name"] not in Group.all():
        raise LookupError(f"no group {a['name']!r}")
    return web.group_doc(a["name"], full=True)


def _member_log(a):
    from .group import Group, groups_dir
    if a["group"] not in Group.all():
        raise LookupError(f"no group {a['group']!r}")
    g = json.load(open(os.path.join(groups_dir(), a["group"], "group.json")))
    if a["vm"] not in [x["vm"] for x in g["members"]]:
        raise LookupError(f"no member {a['vm']!r} in {a['group']}")
    n = max(1, min(2000, int(a.get("lines") or 200)))
    return {"lines": web.tail(os.path.join(groups_dir(), a["group"], "logs", f"{a['vm']}.log"), n)}


def _rehearsal(a):
    from .rehearsal import Rehearsal
    if a["name"] not in Rehearsal.all():
        raise LookupError(f"no rehearsal {a['name']!r}")
    return web.rehearsal_doc(a["name"], full=True)


def _job(a):
    try:
        return {**jobs.load(a["id"]), "log": jobs.tail(a["id"], 100)}
    except jobs.JobError as e:
        raise LookupError(str(e)) from None


TOOLS = {
    "overview": ("Overview", "Every group, migration and rehearsal on this appliance: phase, last "
                 "replication cycle, failures, receivers down, the WAN limit.", _name({}),
                 lambda a: web.overview()),
    "migration": ("Migration", "One migration: plan (disks, networks, public side), recent cycles with "
                  "bytes sent, cutover steps, events.", _name({"name": S}), _migration),
    "group": ("Group", "One group: members by tier, last sync round, cutover state and window, DNS "
              "records, rehearsal, events.", _name({"name": S}), _group),
    "group_member_log": ("Group member log", "The tail of a group member's log (its sync and cutover "
                         "processes).", _name({"group": S, "vm": S, "lines": {"type": "integer"}}), _member_log),
    "rehearsal": ("Rehearsal", "A rehearsal: its airgapped copies, relay, steps, and the report (health, "
                  "predicted cutover window, consistent point, names the copies tried to resolve).",
                  _name({"name": S}), _rehearsal),
    "jobs": ("Jobs", "Actions started from the web page (sync, rehearse, rehearsal destroy): state, "
             "requester, error.", _name({"target": S}), lambda a: {"jobs": jobs.all_jobs(a.get("target"))}),
    "job": ("Job", "One job with the tail of its log.", _name({"id": S}), _job),
    "wan_status": ("WAN status", "The bandwidth limit now, the next scheduled change, compression.",
                   _name({}), lambda a: {"lines": wan.status()}),
}


class Server:
    def __init__(self, out):
        self.out, self.version = out, VERSIONS[0]

    def send(self, msg):
        self.out.write(json.dumps({"jsonrpc": "2.0", **msg}, default=str) + "\n")
        self.out.flush()

    def handle(self, msg):
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            return {"id": msg.get("id") if isinstance(msg, dict) else None,
                    "error": {"code": -32600, "message": "invalid request"}}
        mid, method, params = msg.get("id"), msg["method"], msg.get("params") or {}
        if "id" not in msg:
            return None                                  # notifications (initialized, cancelled) need no answer
        if method == "initialize":
            asked = params.get("protocolVersion")
            self.version = asked if asked in VERSIONS else VERSIONS[0]
            return {"id": mid, "result": {
                "protocolVersion": self.version, "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "exo-v2v", "version": __version__},
                "instructions": "Read-only view of exo-v2v (VMware to Exoscale migrations) on this appliance. "
                                "Start with `overview`. Nothing here changes a migration."}}
        if method == "ping":
            return {"id": mid, "result": {}}
        if method == "tools/list":
            return {"id": mid, "result": {"tools": [
                {"name": k, "title": t, "description": d, "inputSchema": schema,
                 "annotations": {"title": t, "readOnlyHint": True, "destructiveHint": False,
                                 "idempotentHint": True, "openWorldHint": False}}
                for k, (t, d, schema, _fn) in TOOLS.items()]}}
        if method == "tools/call":
            tool = TOOLS.get(params.get("name"))
            args = params.get("arguments") or {}
            if not tool:
                return {"id": mid, "error": {"code": -32602, "message": f"unknown tool {params.get('name')!r}"}}
            schema = tool[2]
            missing = [k for k in schema["required"] if not isinstance(args.get(k), str) or not args.get(k)]
            extra = set(args) - set(schema["properties"])
            if missing or extra or not isinstance(args, dict):
                return {"id": mid, "error": {"code": -32602, "message": f"arguments: need {schema['required']}"}}
            try:
                data = tool[3](args)
            except (LookupError, OSError, ValueError, SystemExit) as e:
                return {"id": mid, "result": {"content": [{"type": "text", "text": str(e)}], "isError": True}}
            res = {"content": [{"type": "text", "text": json.dumps(data, default=str, indent=1)}], "isError": False}
            if self.version >= "2025-06-18":
                res["structuredContent"] = json.loads(json.dumps(data, default=str))
            return {"id": mid, "result": res}
        return {"id": mid, "error": {"code": -32601, "message": f"method {method!r} not found"}}


def serve(inp=None, out=None):
    out = out or sys.stdout
    inp = inp or sys.stdin
    real, sys.stdout = out, sys.stderr          # a stray print never reaches the protocol stream
    srv = Server(real)
    try:
        for line in inp:
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                srv.send({"id": None, "error": {"code": -32700, "message": "parse error"}})
                continue
            reply = srv.handle(msg)
            if reply:
                srv.send(reply)
    finally:
        sys.stdout = real


def cmd_mcp(a):
    if "EXO_V2V_HOME" not in os.environ and os.path.exists(ENV_FILE) and os.access(ENV_FILE, os.R_OK):
        for ln in open(ENV_FILE):
            k, _, v = ln.strip().partition("=")
            if k == "EXO_V2V_HOME":
                os.environ["EXO_V2V_HOME"] = v.strip("'\"")
    serve()


def add_parser(sub):
    s = sub.add_parser("mcp", help="read-only MCP server on stdin/stdout (for AI assistants, over SSH)")
    s.set_defaults(fn=cmd_mcp)
