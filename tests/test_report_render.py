import importlib
from http import HTTPStatus
from nwdash import config, report_render


def test_render_ok(monkeypatch):
    importlib.reload(report_render)
    monkeypatch.setattr(report_render, "build_dashboard",
                        lambda cfg: (HTTPStatus.OK, {"summary": {"totalJobs": 5}}))
    cred = {"rest_api_host": "h", "rest_api_port": 1, "username": "u",
            "encrypted_password": "", "api_mode": "nwui", "report_range": "7d"}
    res = report_render.render(cred)
    assert res.ok is True
    assert res.dashboard["summary"]["totalJobs"] == 5
    assert res.error == ""


def test_render_failure_surfaces_error(monkeypatch):
    importlib.reload(report_render)
    monkeypatch.setattr(report_render, "build_dashboard",
                        lambda cfg: (HTTPStatus.UNAUTHORIZED, {"error": "login rejected"}))
    res = report_render.render({"rest_api_host": "h", "username": "u", "encrypted_password": ""})
    assert res.ok is False
    assert "login rejected" in res.error


def test_last_good_cache_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REPORT_CACHE_DIR", tmp_path / "cache")
    importlib.reload(report_render)
    assert report_render.cache_get("j1") is None
    report_render.cache_put("j1", {"summary": {"totalJobs": 3}})
    assert report_render.cache_get("j1")["summary"]["totalJobs"] == 3
