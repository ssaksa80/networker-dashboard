import importlib
from nwdash import sessions

class _Cfg:
    rest_api_host="10.0.0.9"; rest_api_port=9090; backup_server_host="bkp"; backup_server_port=9090
    username="administrator"; api_mode="nwui"; api_version="v3"; report_range="24h"
    custom_start_date=""; custom_end_date=""; use_wmi_health=True; wmi_username="wmiu"
    timeout_seconds=30; verify_tls=True; use_authc_header=True

class _S:
    def __init__(self, last_used):
        self.config=_Cfg(); self.encrypted_networker_password="enc-pw"
        self.encrypted_wmi_password="enc-wmi"; self.created_at=1.0; self.last_used=last_used

def test_returns_none_when_no_sessions(monkeypatch):
    importlib.reload(sessions)
    monkeypatch.setattr(sessions, "_session_items_snapshot", lambda: [])
    assert sessions.snapshot_latest_live_session() is None

def test_picks_most_recently_used(monkeypatch):
    importlib.reload(sessions)
    monkeypatch.setattr(sessions, "_session_items_snapshot",
                        lambda: [("old", _S(100.0)), ("new", _S(900.0)), ("mid", _S(500.0))])
    snap = sessions.snapshot_latest_live_session()
    assert snap["session_id"] == "new"

def test_snapshot_carries_every_config_field(monkeypatch):
    importlib.reload(sessions)
    monkeypatch.setattr(sessions, "_session_items_snapshot", lambda: [("a", _S(1.0))])
    snap = sessions.snapshot_latest_live_session()
    cfg = snap["config"]
    assert cfg["use_authc_header"] is True
    assert cfg["verify_tls"] is True
    assert cfg["api_version"] == "v3"
    assert cfg["backup_server_host"] == "bkp"
    assert snap["encrypted_networker_password"] == "enc-pw"
