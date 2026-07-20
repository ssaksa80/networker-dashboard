import importlib
from http import HTTPStatus
from nwdash import report_render

def test_render_window_sets_range_and_customs(monkeypatch):
    importlib.reload(report_render)
    captured = {}
    def fake_build(cfg):
        captured["range"] = cfg.report_range
        captured["start"] = cfg.custom_start_date
        captured["end"] = cfg.custom_end_date
        return HTTPStatus.OK, {"summary": {}}
    monkeypatch.setattr(report_render, "build_dashboard", fake_build)
    cred = {"rest_api_host": "h", "username": "u", "encrypted_password": "", "api_mode": "nwui"}
    res = report_render.render_window(cred, ("custom", "2026-06-01", "2026-06-30"))
    assert res.ok is True
    assert captured["range"] == "custom" and captured["start"] == "2026-06-01" and captured["end"] == "2026-06-30"

def test_render_window_daily(monkeypatch):
    importlib.reload(report_render)
    monkeypatch.setattr(report_render, "build_dashboard", lambda cfg: (HTTPStatus.OK, {"summary": {}}))
    res = report_render.render_window({"rest_api_host": "h", "username": "u", "encrypted_password": ""}, ("24h", "", ""))
    assert res.ok is True
