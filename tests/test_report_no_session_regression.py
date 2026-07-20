"""Regression: the exact production failure. A restart wipes all in-memory
sessions; an enabled report job must STILL render and send because it carries
its own credential — nothing here touches the session store."""
import importlib
from http import HTTPStatus
from nwdash import report_jobs, report_render, sessions

def test_enabled_job_fires_with_no_sessions(monkeypatch):
    importlib.reload(report_jobs)
    monkeypatch.setattr(sessions, "_SESSIONS", {}, raising=False)
    monkeypatch.setattr(report_render, "build_dashboard",
                        lambda cfg: (HTTPStatus.OK, {"summary": {"totalJobs": 7}}))
    sent = {}
    monkeypatch.setattr(report_jobs.report_notify, "send_report",
                        lambda job, dash, smtp, pw, stale=False: sent.update(ok=True, n=dash["summary"]["totalJobs"]))
    monkeypatch.setattr(report_jobs.report_render, "cache_put", lambda *a, **k: None)
    job = report_jobs.ReportJob(id="j1", kind="digest", recipients=["a@x.com"], enabled=True,
                                credential={"rest_api_host": "h", "username": "administrator",
                                            "encrypted_password": "", "api_mode": "nwui"})
    report_jobs.fire_job(job, {"smtp": {"host": "h", "port": 25, "security": "none"}, "smtp_password": ""})
    assert sent.get("ok") is True and sent.get("n") == 7
    assert job.health.state == "healthy"
