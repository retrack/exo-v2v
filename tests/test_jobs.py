"""Jobs (jobs.py), the web page's write API (web.py) and the read-only MCP server
(mcp.py). No root, no systemd: jobs run as detached processes or not at all.

Run: python3 -m pytest -q tests/
"""
import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

SRC = os.path.join(os.path.dirname(__file__), "..", "src")
sys.path.insert(0, SRC)

from exo_v2v import jobs, web  # noqa: E402
from exo_v2v.state import State  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("EXO_V2V_HOME", str(tmp_path))
    st = State.new("web01", {"vm": "web01", "zone": "ch-gva-2", "disks": [], "networks": []})
    st.data["receiver"] = {"name": "v2v-web01", "ip": "194.182.1.2"}
    st.set_phase("syncing")
    return tmp_path


def actions(home, enabled=True, web=None):
    cfg = {"actions": {"enabled": enabled, **({"web": web} if web is not None else {})}}
    (home / "config.yaml").write_text(json.dumps(cfg))


def test_policy_and_validation(home, monkeypatch):
    assert jobs.policy() == {"enabled": False, "web": list(jobs.ACTIONS), "max_running": 4}
    launched = []
    monkeypatch.setattr(jobs, "launch", launched.append)
    with pytest.raises(jobs.JobError) as e:
        jobs.start("cutover", "migration", "web01")
    assert e.value.code == 400                                  # not an action at all
    with pytest.raises(jobs.JobError) as e:
        jobs.start("sync", "migration", "web01", allowed=["rehearse"])
    assert e.value.code == 403
    for bad in (("sync", "rehearsal", "web01", {}), ("sync", "migration", "web01", {"full": True}),
                ("rehearse", "migration", "web01", {"ttl": "8h; rm -rf /"})):
        with pytest.raises(jobs.JobError) as e:
            jobs.start(*bad)
        assert e.value.code == 400
    with pytest.raises(jobs.JobError) as e:
        jobs.start("sync", "migration", "../web01")
    assert e.value.code == 404
    j = jobs.start("rehearse", "migration", "web01", {"ttl": "4h"}, source="web", requester="127.0.0.1")
    assert j["argv"] == ["rehearse", "web01", "--ttl", "4h"] and launched == [j]
    with pytest.raises(jobs.JobError) as e:                     # one live job per target
        jobs.start("sync", "migration", "web01")
    assert e.value.code == 409
    assert jobs.argv_of("sync", "group", "shop", {"consistent": True, "quiesce": True}) == \
        ["group", "sync", "shop", "--consistent", "--quiesce"]
    assert jobs.argv_of("sync", "migration", "web01", {"consistent": True}) == ["sync", "web01"]


def test_busy_target_is_refused(home, monkeypatch):
    monkeypatch.setattr(jobs, "launch", lambda j: None)
    lock = home / "web01" / ".lock"
    lock.write_text(json.dumps({"pid": os.getpid(), "what": "sync", "since": 0}))
    monkeypatch.setattr("exo_v2v.group.lock_holder",
                        lambda path: {"pid": 1, "what": "sync"} if str(path) == str(lock) else None)
    with pytest.raises(jobs.JobError) as e:
        jobs.start("sync", "migration", "web01")
    assert e.value.code == 409 and "busy: sync" in str(e.value)


def run_job(home, argv):
    os.makedirs(home / "jobs", exist_ok=True)
    j = {"id": "20261027-120000-abcdef", "action": "sync", "kind": "migration", "target": "web01", "params": {},
         "argv": argv, "source": "web", "requester": "127.0.0.1", "state": "queued", "created": 0}
    os.makedirs(home / "jobs" / j["id"], exist_ok=True)
    jobs.save(j)
    r = subprocess.run([sys.executable, "-m", "exo_v2v", "job", "run", j["id"]], capture_output=True, text=True,
                       env=dict(os.environ, PYTHONPATH=SRC, EXO_V2V_HOME=str(home)))
    return r, jobs.load(j["id"])


def test_job_run_records_the_outcome(home):
    r, j = run_job(home, ["wan", "status"])
    assert r.returncode == 0 and j["state"] == "succeeded" and j["rc"] == 0
    assert any("now: unlimited" in x for x in jobs.tail(j["id"]))
    assert "ALERT job-started" in r.stderr and "ALERT job-finished" in r.stderr
    import shutil
    shutil.rmtree(home / "jobs")
    r, j = run_job(home, ["job", "show", "20000101-000000-000000"])
    assert r.returncode == 1 and j["state"] == "failed" and j["error"] == "error: no such job"
    j["state"], j["pid"] = "running", 999999                  # its process is gone: shown as lost
    jobs.save(j)
    assert jobs.load(j["id"])["state"] == "lost"


@pytest.fixture
def server(home, monkeypatch):
    monkeypatch.setattr(jobs, "launch", lambda j: None)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    web.Handler.token = None


def call(url, body=None, headers=None, method=None):
    h = {"Content-Type": "application/json", "X-Exo-V2V": "1", **(headers or {})}
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(), headers=h,
                                 method=method or ("POST" if body is not None else "GET"))
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw.decode()


def test_web_actions_need_policy_token_and_headers(home, server):
    job = {"action": "sync", "kind": "migration", "target": "web01"}
    assert call(server + "/api/policy")[1] == {"actions": [], "enabled": False, "token": False}
    assert call(server + "/api/jobs", job)[0] == 403                 # no token: never, even on loopback
    web.Handler.token = "s3cret"
    auth = {"Authorization": "Bearer s3cret"}
    assert call(server + "/api/jobs", job, auth)[0] == 403           # actions not enabled
    actions(home, web=["rehearse"])
    assert call(server + "/api/policy", headers=auth)[1]["actions"] == ["rehearse"]
    assert call(server + "/api/jobs", job, auth)[0] == 403           # sync not in the web list
    actions(home)
    assert call(server + "/api/jobs", job)[0] == 401
    assert call(server + "/api/jobs", job, {**auth, "X-Exo-V2V": ""})[0] == 400
    assert call(server + "/api/jobs", job, {**auth, "Origin": "http://evil.example"})[0] == 403
    assert call(server + "/api/jobs", job, {**auth, "Sec-Fetch-Site": "cross-site"})[0] == 403
    code, j = call(server + "/api/jobs", job, auth)
    assert code == 202 and j["argv"] == ["sync", "web01"] and j["source"] == "web"
    assert call(server + "/api/jobs", job, auth)[0] == 409
    assert [x["id"] for x in call(server + "/api/jobs", headers=auth)[1]["jobs"]] == [j["id"]]
    assert call(server + f"/api/jobs/{j['id']}", headers=auth)[1]["state"] == "queued"
    assert call(server + "/api/jobs/../../etc", headers=auth)[0] == 404
    assert call(server + "/api/overview", {}, auth)[0] == 405


def test_web_without_token_serves_loopback_names_only(home, server):
    assert call(server + "/api/overview")[0] == 200
    assert call(server + "/api/overview", headers={"Host": "localhost:8780"})[0] == 200
    assert call(server + "/api/overview", headers={"Host": "rebind.evil.example:8780"})[0] == 421
    web.Handler.token = "s3cret"                   # a token protects every name
    assert call(server + "/api/overview", headers={"Host": "appliance.example:8780",
                                                    "Authorization": "Bearer s3cret"})[0] == 200
    code, _ = call(server + "/login", {"token": "s3cret"})
    assert code == 200


def mcp_session(home, msgs):
    p = subprocess.run([sys.executable, "-m", "exo_v2v", "mcp"], input="".join(json.dumps(m) + "\n" for m in msgs),
                       capture_output=True, text=True, env=dict(os.environ, PYTHONPATH=SRC, EXO_V2V_HOME=str(home)))
    return [json.loads(x) for x in p.stdout.splitlines()]


def test_mcp_read_only_session(home):
    out = mcp_session(home, [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "overview", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "migration", "arguments": {"name": "web01"}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "migration", "arguments": {"name": "nope"}}},
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "cutover", "arguments": {"name": "web01"}}},
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "migration", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 8, "method": "resources/list"},
        {"jsonrpc": "2.0", "id": 9, "method": "ping"}])
    by = {m["id"]: m for m in out}
    assert len(out) == 9                                            # the notification got no answer
    assert by[1]["result"]["protocolVersion"] == "2025-06-18" and by[1]["result"]["serverInfo"]["name"] == "exo-v2v"
    tools = by[2]["result"]["tools"]
    assert {t["name"] for t in tools} >= {"overview", "migration", "group", "rehearsal", "jobs", "wan_status"}
    assert all(t["annotations"]["readOnlyHint"] and not t["annotations"]["destructiveHint"] for t in tools)
    assert not any(n in t["name"] for t in tools for n in ("cutover", "rollback", "sync", "rehearse"))
    assert by[3]["result"]["structuredContent"]["migrations"][0]["name"] == "web01"
    assert by[4]["result"]["structuredContent"]["name"] == "web01" and not by[4]["result"]["isError"]
    assert by[5]["result"]["isError"] and "no migration" in by[5]["result"]["content"][0]["text"]
    assert by[6]["error"]["code"] == -32602 and by[7]["error"]["code"] == -32602
    assert by[8]["error"]["code"] == -32601 and by[9]["result"] == {}
    old = mcp_session(home, [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
                             {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "overview", "arguments": {}}}])
    assert old[0]["result"]["protocolVersion"] == "2024-11-05" and "structuredContent" not in old[1]["result"]
    unknown = mcp_session(home, [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}}])
    assert unknown[0]["result"]["protocolVersion"] == "2025-11-25"
