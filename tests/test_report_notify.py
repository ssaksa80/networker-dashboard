import importlib
from nwdash import report_notify

class _Job:
    id = "j1"; recipients = ["a@x.com"]; theme = "default"; kind = "digest"

def test_send_report_calls_smtp_with_recipients(monkeypatch):
    importlib.reload(report_notify)
    calls = {}
    def fake_send(settings, subject, body, pw, html_body="", attachments=None, **kw):
        calls["to"] = list(settings.recipients); calls["subject"] = subject
        calls["stale"] = "STALE" in (html_body + body)
        return {"host": settings.smtp_host}
    monkeypatch.setattr(report_notify, "send_smtp_email", fake_send)
    smtp = {"host": "203.0.113.7", "port": 25, "security": "none", "from": "r@x.com", "username": ""}
    report_notify.send_report(_Job(), {"summary": {}}, smtp, "", stale=False)
    assert calls["to"] == ["a@x.com"]
    assert calls["stale"] is False

def test_send_report_stale_banner(monkeypatch):
    importlib.reload(report_notify)
    seen = {}
    def fake_send(settings, subject, body, pw, html_body="", attachments=None, **kw):
        seen["stale"] = ("STALE" in html_body) or ("stale" in body.lower()); return {}
    monkeypatch.setattr(report_notify, "send_smtp_email", fake_send)
    smtp = {"host": "h", "port": 25, "security": "none", "from": "r@x.com", "username": ""}
    report_notify.send_report(_Job(), {"summary": {}}, smtp, "", stale=True)
    assert seen["stale"] is True

def test_send_ops_alert_uses_ops_address(monkeypatch):
    importlib.reload(report_notify)
    seen = {}
    def fake_send(settings, subject, body, pw, html_body="", attachments=None, **kw):
        seen["to"] = list(settings.recipients); seen["subject"] = subject; return {}
    monkeypatch.setattr(report_notify, "send_smtp_email", fake_send)
    smtp = {"host": "h", "port": 25, "security": "none", "from": "r@x.com", "username": ""}
    report_notify.send_ops_alert(_Job(), "connect failed", smtp, "ops@x.com", "")
    assert seen["to"] == ["ops@x.com"]
    assert "FAILED" in seen["subject"].upper()
