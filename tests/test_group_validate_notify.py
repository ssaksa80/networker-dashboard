import importlib
from nwdash import report_groups, report_notify

def test_validate_ok():
    importlib.reload(report_groups)
    g = report_groups.ReportGroup(id="a", name="Ops", sections=["backup_sla"], recipients=["a@x.com"], cadence="weekly", send_time="07:00")
    r = report_groups.validate_group(g)
    assert r.ok is True and r.errors == {}

def test_validate_collects_errors():
    importlib.reload(report_groups)
    g = report_groups.ReportGroup(id="a", name="", sections=[], recipients=[], cadence="yearly", send_time="99:99")
    r = report_groups.validate_group(g)
    assert r.ok is False
    for f in ("name", "sections", "recipients", "cadence", "send_time"):
        assert f in r.errors

def test_send_group_report_filters_sections_and_test_prefix(monkeypatch):
    importlib.reload(report_notify)
    seen = {}
    def fake_send(settings, subject, body, pw, html_body="", attachments=None, **kw):
        seen["subject"] = subject; seen["to"] = list(settings.recipients); return {}
    monkeypatch.setattr(report_notify, "send_smtp_email", fake_send)
    monkeypatch.setattr(report_notify, "dashboard_report_email",
                        lambda dash, sections=None: ("plain", "<html>%s</html>" % ",".join(sections or [])))
    monkeypatch.setattr(report_notify, "render_dashboard_snapshot_png", lambda dash: b"")
    class G: recipients=["a@x.com"]; sections=["alerts"]; name="Ops"; theme="default"
    smtp = {"host": "h", "port": 25, "security": "none", "from": "r@x.com"}
    report_notify.send_group_report(G(), {"summary": {}}, smtp, "", test=True)
    assert seen["to"] == ["a@x.com"] and seen["subject"].startswith("[TEST]")

def test_send_group_report_stale_banner(monkeypatch):
    import importlib
    from nwdash import report_notify
    importlib.reload(report_notify)
    seen = {}
    def fake_send(settings, subject, body, pw, html_body="", attachments=None, **kw):
        seen["subject"] = subject; seen["stale"] = ("STALE" in html_body) or ("stale" in body.lower()); return {}
    monkeypatch.setattr(report_notify, "send_smtp_email", fake_send)
    monkeypatch.setattr(report_notify, "dashboard_report_email", lambda dash, sections=None: ("plain", "<html></html>"))
    monkeypatch.setattr(report_notify, "render_dashboard_snapshot_png", lambda dash: b"")
    class G: recipients=["a@x.com"]; sections=["alerts"]; name="Ops"; theme="default"
    report_notify.send_group_report(G(), {"summary": {}}, {"host":"h","port":25,"security":"none","from":"r@x.com"}, "", stale=True)
    assert seen["stale"] is True and "stale data" in seen["subject"].lower()
