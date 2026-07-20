import importlib
from http import HTTPStatus
from nwdash import report_api, report_jobs

def _payload(**kw):
    base = {"action": "create", "kind": "digest", "recipients": "a@x.com, b@x.com",
            "reportTime": "07:30",
            "credential": {"rest_api_host": "h", "rest_api_port": 9090, "username": "u",
                           "password": "pw", "api_mode": "nwui"}}
    base.update(kw); return base

def test_create_rejected_when_validation_fails(monkeypatch):
    importlib.reload(report_jobs); importlib.reload(report_api)
    monkeypatch.setattr(report_api.report_jobs, "validate",
                        lambda job, smtp, smtp_password: report_jobs.ValidationResult(
                            False, {"credential": True, "render": False, "smtp": True}, "login rejected"))
    monkeypatch.setattr(report_api, "_smtp_config", lambda: ({"host": "h"}, ""))
    status, body = report_api.handle_report_jobs(_payload())
    assert status == HTTPStatus.BAD_REQUEST
    assert body["ok"] is False
    assert body["checks"]["render"] is False
    assert report_jobs.get_job(body.get("id", "")) is None

def test_create_stores_enabled_job_when_valid(monkeypatch):
    importlib.reload(report_jobs); importlib.reload(report_api)
    monkeypatch.setattr(report_api.report_jobs, "validate",
                        lambda job, smtp, smtp_password: report_jobs.ValidationResult(
                            True, {"credential": True, "render": True, "smtp": True}, ""))
    monkeypatch.setattr(report_api, "_smtp_config", lambda: ({"host": "h"}, ""))
    monkeypatch.setattr(report_api.report_jobs, "persist_jobs", lambda: None)
    status, body = report_api.handle_report_jobs(_payload())
    assert status == HTTPStatus.OK and body["ok"] is True
    job = report_jobs.get_job(body["id"])
    assert job is not None and job.enabled is True
    assert job.recipients == ["a@x.com", "b@x.com"]
    assert job.credential["encrypted_password"]
    assert "password" not in job.credential

def test_list_returns_health(monkeypatch):
    importlib.reload(report_jobs); importlib.reload(report_api)
    report_jobs.put_job(report_jobs.ReportJob(id="j1", kind="digest", recipients=["a@x.com"], enabled=True))
    status, body = report_api.handle_report_jobs({"action": "list"})
    assert status == HTTPStatus.OK
    assert body["jobs"][0]["id"] == "j1"
    assert "health" in body["jobs"][0]

def test_delete_removes_job(monkeypatch):
    importlib.reload(report_jobs); importlib.reload(report_api)
    monkeypatch.setattr(report_api.report_jobs, "persist_jobs", lambda: None)
    report_jobs.put_job(report_jobs.ReportJob(id="j1", kind="digest", recipients=["a@x.com"]))
    status, body = report_api.handle_report_jobs({"action": "delete", "id": "j1"})
    assert status == HTTPStatus.OK and report_jobs.get_job("j1") is None

def test_create_rejects_bad_report_time(monkeypatch):
    import importlib
    from nwdash import report_api, report_jobs
    importlib.reload(report_jobs); importlib.reload(report_api)
    monkeypatch.setattr(report_api, "_smtp_config", lambda: ({"host": "h"}, ""))
    p = {"action": "create", "kind": "digest", "recipients": "a@x.com", "reportTime": "25:00",
         "credential": {"rest_api_host": "h", "username": "u", "password": "pw"}}
    status, body = report_api.handle_report_jobs(p)
    from http import HTTPStatus
    assert status == HTTPStatus.BAD_REQUEST and body["ok"] is False
