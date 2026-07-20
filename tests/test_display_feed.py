import importlib
from nwdash import models, display, config

def test_refresh_uses_display_connection_when_no_session(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DISPLAY_CONNECTION_FILE", tmp_path / "c.json")
    importlib.reload(display); importlib.reload(models)
    with models.SHARED_DASHBOARD_LOCK:
        models.SHARED_DASHBOARD_STATE["sessionId"] = ""
    display.save_connection({"rest_api_host": "h", "username": "u", "password": "pw", "api_mode": "nwui"})
    captured = {}
    monkeypatch.setattr(models, "set_shared_dashboard",
                        lambda sid, dash: captured.update(sid=sid, dash=dash))
    import nwdash.report_render as rr
    monkeypatch.setattr(rr, "render", lambda cred: rr.RenderResult(True, {"ok": True, "summary": {"totalJobs": 3}}, ""))
    models._shared_dashboard_refresh_once()
    assert captured.get("dash", {}).get("summary", {}).get("totalJobs") == 3

def test_refresh_noop_when_no_session_and_no_connection(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DISPLAY_CONNECTION_FILE", tmp_path / "none.json")
    importlib.reload(display); importlib.reload(models)
    with models.SHARED_DASHBOARD_LOCK:
        models.SHARED_DASHBOARD_STATE["sessionId"] = ""
    called = {}
    monkeypatch.setattr(models, "set_shared_dashboard", lambda sid, dash: called.update(hit=True))
    models._shared_dashboard_refresh_once()
    assert "hit" not in called


def test_display_feed_refreshes_every_cycle_not_latched(tmp_path, monkeypatch):
    import importlib
    from nwdash import models, display, config
    monkeypatch.setattr(config, "DISPLAY_CONNECTION_FILE", tmp_path / "c.json")
    importlib.reload(display); importlib.reload(models)
    with models.SHARED_DASHBOARD_LOCK:
        models.SHARED_DASHBOARD_STATE["sessionId"] = ""
    display.save_connection({"rest_api_host": "h", "username": "u", "password": "pw", "api_mode": "nwui"})
    import nwdash.report_render as rr
    calls = {"n": 0}
    def fake_render(cred):
        calls["n"] += 1
        return rr.RenderResult(True, {"ok": True, "summary": {"totalJobs": calls["n"]},
                                      "target": {"backupServer": "h"}}, "")
    monkeypatch.setattr(rr, "render", fake_render)
    models._shared_dashboard_refresh_once()
    models._shared_dashboard_refresh_once()
    models._shared_dashboard_refresh_once()
    assert calls["n"] == 3   # display connection renders on EVERY cycle, not latched off after one
