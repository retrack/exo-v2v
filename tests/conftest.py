import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from exo_v2v import wan  # noqa: E402


@pytest.fixture(autouse=True)
def _own_wan_bucket(tmp_path, monkeypatch):
    """Keep tests (and the processes they start) off the appliance's bucket in /run/lock."""
    path = str(tmp_path / "wan.bucket")
    monkeypatch.setattr(wan, "BUCKET", path)
    monkeypatch.setenv("EXO_V2V_WAN_BUCKET", path)
