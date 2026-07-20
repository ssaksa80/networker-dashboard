import importlib
from nwdash import report_jobs


def _job():
    return report_jobs.ReportJob(id="j1", kind="digest", recipients=["a@x.com"],
                                 credential={"rest_api_host": "h", "username": "u",
                                             "encrypted_password": ""})


def test_validate_all_pass(monkeypatch):
    importlib.reload(report_jobs)
    from nwdash import report_render
    monkeypatch.setattr(report_jobs.report_render, "render",
                        lambda cred: report_render.RenderResult(True, {"summary": {}}, ""))
    monkeypatch.setattr(report_jobs, "_smtp_probe", lambda smtp, pw: (True, ""))
    res = report_jobs.validate(_job(), smtp={"host": "h", "port": 25, "security": "none"}, smtp_password="")
    assert res.ok is True
    assert res.checks["credential"] and res.checks["render"] and res.checks["smtp"]


def test_validate_render_fails(monkeypatch):
    importlib.reload(report_jobs)
    from nwdash import report_render
    monkeypatch.setattr(report_jobs.report_render, "render",
                        lambda cred: report_render.RenderResult(False, {}, "login rejected"))
    monkeypatch.setattr(report_jobs, "_smtp_probe", lambda smtp, pw: (True, ""))
    res = report_jobs.validate(_job(), smtp={"host": "h", "port": 25, "security": "none"}, smtp_password="")
    assert res.ok is False
    assert res.checks["render"] is False
    assert "login rejected" in res.detail


def test_validate_smtp_fails(monkeypatch):
    importlib.reload(report_jobs)
    from nwdash import report_render
    monkeypatch.setattr(report_jobs.report_render, "render",
                        lambda cred: report_render.RenderResult(True, {"summary": {}}, ""))
    monkeypatch.setattr(report_jobs, "_smtp_probe", lambda smtp, pw: (False, "connection refused"))
    res = report_jobs.validate(_job(), smtp={"host": "h", "port": 25, "security": "none"}, smtp_password="")
    assert res.ok is False
    assert res.checks["smtp"] is False
