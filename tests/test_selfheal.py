"""The one flaw the original engine had: a schedule without a session snapshot
went silently inert. These tests pin the self-healing behavior that fixes it."""
import importlib
from nwdash import emailer

def _rec(host="10.0.0.9"):
    return {"session_id": "s-live", "last_used": 9.0, "encrypted_networker_password": "enc",
            "config": {"rest_api_host": host, "username": "administrator"}}

def _automation(connection):
    from nwdash.models import AlertAutomation
    return AlertAutomation(
        automation_id="auto1", session_id="dead-session", connection=connection,
        smtp_host="h", smtp_port=25, smtp_username="", encrypted_smtp_password="",
        smtp_from="r@x.com", recipients=["a@x.com"], smtp_security="none",
        interval_minutes=60, trigger="critical", schedule_type="alert",
        report_time="08:00", created_at=1.0, theme="default",
    )

def test_fire_adopts_latest_record_when_snapshot_empty(monkeypatch):
    importlib.reload(emailer)
    monkeypatch.setattr(emailer, "_session_exists", lambda sid: False)
    monkeypatch.setattr(emailer, "latest_session_record", lambda: _rec())
    monkeypatch.setattr(emailer, "decrypt_process_secret", lambda s: "pw" if s else "")
    recreated = {}
    monkeypatch.setattr(emailer, "recreate_session_from_snapshot",
                        lambda sid, snap: recreated.update(host=snap["config"]["rest_api_host"]) or True)
    monkeypatch.setattr(emailer, "persist_automations", lambda: None)
    a = _automation(connection={})
    assert emailer._ensure_automation_session(a) is True
    assert recreated["host"] == "10.0.0.9"
    assert a.connection.get("config", {}).get("rest_api_host") == "10.0.0.9"  # adopted + stored

def test_fire_adopts_when_snapshot_undecryptable(monkeypatch):
    importlib.reload(emailer)
    monkeypatch.setattr(emailer, "_session_exists", lambda sid: False)
    monkeypatch.setattr(emailer, "latest_session_record", lambda: _rec("adopted-host"))
    # old snapshot decrypts to "" (dead key), adopted record decrypts fine
    monkeypatch.setattr(emailer, "decrypt_process_secret",
                        lambda s: "pw" if s == "enc" else "")
    monkeypatch.setattr(emailer, "recreate_session_from_snapshot", lambda sid, snap: True)
    monkeypatch.setattr(emailer, "persist_automations", lambda: None)
    a = _automation(connection={"encrypted_networker_password": "dead", "config": {"rest_api_host": "old"}})
    assert emailer._ensure_automation_session(a) is True
    assert a.connection["config"]["rest_api_host"] == "adopted-host"

def test_fire_still_waits_gracefully_when_nothing_anywhere(monkeypatch):
    importlib.reload(emailer)
    monkeypatch.setattr(emailer, "_session_exists", lambda sid: False)
    monkeypatch.setattr(emailer, "latest_session_record", lambda: None)
    a = _automation(connection={})
    assert emailer._ensure_automation_session(a) is False
    assert "waiting" in a.last_result.lower()

def test_arm_time_start_adopts_when_no_live_session(monkeypatch):
    importlib.reload(emailer)
    monkeypatch.setattr(emailer, "connection_snapshot_for_session", lambda sid: {})
    monkeypatch.setattr(emailer, "latest_session_record", lambda: _rec())
    assert emailer._resolve_connection({}, None)["config"]["rest_api_host"] == "10.0.0.9"

def test_arm_time_prefers_live_snapshot(monkeypatch):
    importlib.reload(emailer)
    monkeypatch.setattr(emailer, "latest_session_record", lambda: _rec("fallback"))
    live = {"session_id": "live", "config": {"rest_api_host": "live-host"}}
    assert emailer._resolve_connection(live, None)["config"]["rest_api_host"] == "live-host"
