import importlib, time
from nwdash import report_jobs, report_render

def _cfg():
    return {"smtp": {"host": "h", "port": 25, "security": "none", "from": "r@x.com"},
            "smtp_password": "", "ops_address": "ops@x.com"}

def test_fire_success_sends_and_caches(monkeypatch):
    importlib.reload(report_jobs)
    sent = {}
    monkeypatch.setattr(report_jobs.report_render, "render",
                        lambda cred: report_render.RenderResult(True, {"summary": {"totalJobs": 4}}, ""))
    monkeypatch.setattr(report_jobs.report_render, "cache_put", lambda jid, dash: sent.setdefault("cached", dash))
    monkeypatch.setattr(report_jobs.report_notify, "send_report",
                        lambda job, dash, smtp, pw, stale=False: sent.update(report=True, stale=stale) or {})
    monkeypatch.setattr(report_jobs.report_notify, "send_ops_alert", lambda *a, **k: sent.update(ops=True))
    job = report_jobs.ReportJob(id="j1", kind="digest", recipients=["a@x.com"], enabled=True,
                                credential={"rest_api_host": "h", "username": "u", "encrypted_password": ""})
    report_jobs.fire_job(job, _cfg())
    assert sent.get("report") is True and sent.get("stale") is False
    assert "cached" in sent and "ops" not in sent
    assert job.health.state == "healthy" and job.health.consecutive_failures == 0

def test_fire_failure_sends_fallback_and_ops_alert(monkeypatch):
    importlib.reload(report_jobs)
    sent = {}
    monkeypatch.setattr(report_jobs.report_render, "render",
                        lambda cred: report_render.RenderResult(False, {}, "netWorker down"))
    monkeypatch.setattr(report_jobs.report_render, "cache_get", lambda jid: {"summary": {"totalJobs": 9}})
    monkeypatch.setattr(report_jobs.report_notify, "send_report",
                        lambda job, dash, smtp, pw, stale=False: sent.update(report=True, stale=stale) or {})
    monkeypatch.setattr(report_jobs.report_notify, "send_ops_alert",
                        lambda job, err, smtp, ops, pw: sent.update(ops=True, err=err))
    job = report_jobs.ReportJob(id="j1", kind="digest", recipients=["a@x.com"], enabled=True,
                                credential={"rest_api_host": "h", "username": "u", "encrypted_password": ""})
    report_jobs.fire_job(job, _cfg())
    assert sent.get("report") is True and sent.get("stale") is True
    assert sent.get("ops") is True and "netWorker down" in sent["err"]
    assert job.health.state == "unhealthy" and job.health.consecutive_failures == 1

def test_fire_failure_no_cache_still_alerts_ops(monkeypatch):
    importlib.reload(report_jobs)
    sent = {}
    monkeypatch.setattr(report_jobs.report_render, "render",
                        lambda cred: report_render.RenderResult(False, {}, "down"))
    monkeypatch.setattr(report_jobs.report_render, "cache_get", lambda jid: None)
    monkeypatch.setattr(report_jobs.report_notify, "send_report", lambda *a, **k: sent.update(report=True))
    monkeypatch.setattr(report_jobs.report_notify, "send_ops_alert", lambda *a, **k: sent.update(ops=True))
    job = report_jobs.ReportJob(id="j1", kind="digest", recipients=["a@x.com"], enabled=True, credential={})
    report_jobs.fire_job(job, _cfg())
    assert "report" not in sent and sent.get("ops") is True

def test_disabled_job_does_not_fire(monkeypatch):
    importlib.reload(report_jobs)
    sent = {}
    monkeypatch.setattr(report_jobs.report_render, "render", lambda cred: sent.update(rendered=True))
    job = report_jobs.ReportJob(id="j1", kind="digest", recipients=["a@x.com"], enabled=False, credential={})
    report_jobs.fire_job(job, _cfg())
    assert "rendered" not in sent
