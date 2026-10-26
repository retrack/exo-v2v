"""Unattended replication: systemd timers (service.py) and webhook alerts
(alerts.py). No root, no systemd: systemctl is stubbed, a local HTTP server
stands in for the webhook.

Run: python3 -m pytest -q tests/
"""
import http.server
import json
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from exo_v2v import alerts, service  # noqa: E402
from exo_v2v.state import State  # noqa: E402


@pytest.fixture
def hook(tmp_path, monkeypatch):
    """A webhook that records what it receives, and EXO_V2V_HOME pointing at it."""
    got = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("EXO_V2V_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        f"alerts:\n  webhook: http://127.0.0.1:{srv.server_port}/hook\n  lag: 1h\n  failures: 2\n")
    yield got
    srv.shutdown()


def migration(name="m1"):
    st = State.new(name, {"vm": "vm1", "disks": []})
    st.data["phase"] = "syncing"
    st.save()
    return st


def test_failures_alert_once_then_recovery(hook):
    st = migration()
    alerts.sync_failed(st, "datastore unreachable")
    assert hook == []                                         # one failure: not yet
    alerts.sync_failed(st, "datastore unreachable")
    alerts.sync_failed(st, "datastore unreachable")           # still failing: no second alert
    assert [h["event"] for h in hook] == ["sync-failing"]
    assert "2 cycles failed" in hook[0]["text"] and hook[0]["migration"] == "m1"
    alerts.sync_ok(st)
    assert [h["event"] for h in hook] == ["sync-failing", "sync-recovered"]
    assert State.load("m1")["sync"]["failures"] == 0


def test_lag_alert_and_catch_up(hook):
    st = migration("m2")
    st.data["sync"] = {"last_ok": time.time() - 7200}
    st.save()
    alerts.check_lag()
    alerts.check_lag()                                        # remembered in alerts.json: once
    assert [h["event"] for h in hook] == ["lag"] and "2 h" in hook[0]["text"]
    st.data["sync"]["last_ok"] = time.time()
    st.save()
    alerts.check_lag()
    assert [h["event"] for h in hook] == ["lag", "lag-recovered"]


def test_no_webhook_only_logs(tmp_path, monkeypatch):
    monkeypatch.setenv("EXO_V2V_HOME", str(tmp_path))
    assert alerts.send("test", "hello") is False


def test_timer_units(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setenv("EXO_V2V_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(service, "UNIT_DIR", str(tmp_path))
    monkeypatch.setattr(service, "systemctl", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    env = tmp_path / "etc" / "exo-v2v.env"
    monkeypatch.setenv("VSPHERE_HOST", "vc.example.com")
    monkeypatch.setenv("VSPHERE_PASSWORD", "it's secret")
    service.install("sync", "web01", 3600, str(env), write=True)
    assert oct(env.stat().st_mode & 0o777) == "0o600"
    assert "VSPHERE_PASSWORD='it'\"'\"'s secret'" in env.read_text()
    svc = (tmp_path / "exo-v2v-sync-web01.service").read_text()
    assert f"EnvironmentFile={env}" in svc and "-m exo_v2v sync web01" in svc and "Type=oneshot" in svc
    assert "OnUnitInactiveSec=3600s" in (tmp_path / "exo-v2v-sync-web01.timer").read_text()
    assert ("enable", "--now", "exo-v2v-sync-web01.timer") in calls
    service.install("gsync", "shop", 1800, str(env))
    assert "-m exo_v2v group sync shop" in (tmp_path / "exo-v2v-gsync-shop.service").read_text()
    service.stop_for(migration="web01", group="shop")         # what a cutover does
    assert not [f for f in os.listdir(tmp_path) if f.startswith("exo-v2v-")] and env.exists()
