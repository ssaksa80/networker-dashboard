import importlib, json
from nwdash import sessions, config

class _Cfg:
    rest_api_host="10.0.0.9"; rest_api_port=9090; backup_server_host="bkp"; backup_server_port=9090
    username="administrator"; api_mode="nwui"; api_version="auto"; report_range="24h"
    custom_start_date=""; custom_end_date=""; use_wmi_health=False; wmi_username=""
    timeout_seconds=30; verify_tls=False; use_authc_header=False

class _S:
    def __init__(self, last_used):
        self.config=_Cfg(); self.encrypted_networker_password="enc"; self.encrypted_wmi_password=""
        self.created_at=1.0; self.last_used=last_used

def test_live_sessions_win_and_newest_picked(monkeypatch):
    importlib.reload(sessions)
    monkeypatch.setattr(sessions, "_session_items_snapshot",
                        lambda: [("old", _S(10.0)), ("new", _S(99.0))])
    rec = sessions.latest_session_record()
    assert rec["session_id"] == "new"
    assert rec["config"]["rest_api_host"] == "10.0.0.9"

def test_disk_fallback_when_no_live(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SESSION_PERSISTENCE_FILE", tmp_path / "sessions.json")
    importlib.reload(sessions)
    monkeypatch.setattr(sessions, "_session_items_snapshot", lambda: [])
    (tmp_path / "sessions.json").write_text(json.dumps({
        "a": {"session_id": "a", "last_used": 5.0, "encrypted_networker_password": "e",
              "config": {"rest_api_host": "h1", "username": "u"}},
        "b": {"session_id": "b", "last_used": 50.0, "encrypted_networker_password": "e",
              "config": {"rest_api_host": "h2", "username": "u"}},
    }), encoding="utf-8")
    rec = sessions.latest_session_record()
    assert rec["session_id"] == "b" and rec["config"]["rest_api_host"] == "h2"

def test_none_when_nothing_anywhere(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SESSION_PERSISTENCE_FILE", tmp_path / "none.json")
    importlib.reload(sessions)
    monkeypatch.setattr(sessions, "_session_items_snapshot", lambda: [])
    assert sessions.latest_session_record() is None
