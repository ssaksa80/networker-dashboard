import importlib
from http import HTTPStatus
from nwdash import report_groups_api as api

def test_use_current_connection_without_session(monkeypatch):
    importlib.reload(api)
    monkeypatch.setattr(api, "snapshot_latest_live_session", lambda: None)
    saved = {}
    monkeypatch.setattr(api.display, "save_connection", lambda s: saved.setdefault("saved", True))
    st, body = api.handle_report_groups({"action": "use-current-connection"})
    assert st == HTTPStatus.BAD_REQUEST and body["ok"] is False
    assert "connect" in body["message"].lower()
    assert "saved" not in saved

def test_use_current_connection_saves_and_validates(monkeypatch):
    importlib.reload(api)
    snap = {"session_id": "s1", "config": {"rest_api_host": "h", "username": "u"},
            "encrypted_networker_password": ""}
    monkeypatch.setattr(api, "snapshot_latest_live_session", lambda: snap)
    saved = {}
    monkeypatch.setattr(api.display, "save_connection", lambda s: saved.setdefault("snap", s))
    monkeypatch.setattr(api, "_reporting_connection", lambda: snap)
    from nwdash import report_render
    monkeypatch.setattr(api.report_render, "render_window",
                        lambda c, w: report_render.RenderResult(True, {"summary": {"totalJobs": 2245, "totalAlerts": 3}}, ""))
    st, body = api.handle_report_groups({"action": "use-current-connection"})
    assert st == HTTPStatus.OK and body["ok"] is True
    assert saved["snap"]["session_id"] == "s1"
    assert body["jobs"] == 2245
    assert ("2245" in body["message"]) or ("2,245" in body["message"])

def test_validate_flags_zero_data(monkeypatch):
    importlib.reload(api)
    monkeypatch.setattr(api, "_reporting_connection", lambda: {"config": {"rest_api_host": "h", "username": "u"}})
    from nwdash import report_render
    monkeypatch.setattr(api.report_render, "render_window",
                        lambda c, w: report_render.RenderResult(True, {"summary": {"totalJobs": 0, "totalAlerts": 0}}, ""))
    st, body = api.handle_report_groups({"action": "validate-connection"})
    assert st == HTTPStatus.OK and body["ok"] is True
    assert body["zeroData"] is True
    assert "0 jobs" in body["message"]

def test_validate_render_failure(monkeypatch):
    importlib.reload(api)
    monkeypatch.setattr(api, "_reporting_connection", lambda: {"config": {"rest_api_host": "h", "username": "u"}})
    from nwdash import report_render
    monkeypatch.setattr(api.report_render, "render_window",
                        lambda c, w: report_render.RenderResult(False, {}, "login rejected"))
    st, body = api.handle_report_groups({"action": "validate-connection"})
    assert body["ok"] is False and "login rejected" in body["message"]

def test_connection_status_never_leaks_password(monkeypatch):
    importlib.reload(api)
    monkeypatch.setattr(api, "_reporting_connection",
                        lambda: {"config": {"rest_api_host": "h", "username": "u", "api_mode": "nwui"},
                                 "encrypted_networker_password": "SECRET-TOKEN"})
    st, body = api.handle_report_groups({"action": "connection-status"})
    assert st == HTTPStatus.OK and body["hasConnection"] is True
    assert body["host"] == "h" and body["username"] == "u"
    assert "SECRET-TOKEN" not in str(body)
