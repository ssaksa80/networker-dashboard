import importlib
from nwdash import report_groups, report_render

def _cfg():
    return {"smtp": {"host": "h", "port": 25, "security": "none"}, "smtp_password": "", "ops_address": "ops@x.com",
            "connection": {"rest_api_host": "h", "username": "u", "encrypted_password": ""}}

def test_fire_success_sends_selected_and_healthy(monkeypatch):
    importlib.reload(report_groups)
    sent = {}
    monkeypatch.setattr(report_groups.report_render, "render_window",
                        lambda cred, win: report_render.RenderResult(True, {"summary": {}}, ""))
    monkeypatch.setattr(report_groups.report_render, "cache_put", lambda k, d: None)
    monkeypatch.setattr(report_groups.report_notify, "send_group_report",
                        lambda g, d, smtp, pw, test=False: sent.update(sent=True, test=test) or {})
    monkeypatch.setattr(report_groups.report_notify, "send_ops_alert", lambda *a, **k: sent.update(ops=True))
    g = report_groups.ReportGroup(id="a", name="Ops", sections=["alerts"], recipients=["a@x.com"], enabled=True, cadence="daily")
    report_groups.fire_group(g, _cfg())
    assert sent.get("sent") is True and sent.get("test") is False and "ops" not in sent
    assert g.health.state == "healthy" and g.health.next_run > 0

def test_fire_failure_fallback_and_ops(monkeypatch):
    importlib.reload(report_groups)
    sent = {}
    monkeypatch.setattr(report_groups.report_render, "render_window",
                        lambda cred, win: report_render.RenderResult(False, {}, "down"))
    monkeypatch.setattr(report_groups.report_render, "cache_get", lambda k: {"summary": {}})
    monkeypatch.setattr(report_groups.report_notify, "send_group_report",
                        lambda g, d, smtp, pw, test=False: sent.update(sent=True) or {})
    monkeypatch.setattr(report_groups.report_notify, "send_ops_alert", lambda g, err, smtp, ops, pw: sent.update(ops=True))
    g = report_groups.ReportGroup(id="a", name="Ops", sections=["alerts"], recipients=["a@x.com"], enabled=True)
    report_groups.fire_group(g, _cfg())
    assert sent.get("ops") is True and g.health.state == "unhealthy"

def test_send_on_demand_test(monkeypatch):
    importlib.reload(report_groups)
    sent = {}
    monkeypatch.setattr(report_groups.report_render, "render_window",
                        lambda cred, win: report_render.RenderResult(True, {"summary": {}}, ""))
    monkeypatch.setattr(report_groups.report_notify, "send_group_report",
                        lambda g, d, smtp, pw, test=False: sent.update(test=test) or {})
    g = report_groups.ReportGroup(id="a", name="Ops", sections=["alerts"], recipients=["a@x.com"], cadence="daily")
    ok, msg = report_groups.send_on_demand(g, _cfg(), test=True)
    assert ok is True and sent.get("test") is True

def test_send_on_demand_without_connection_gives_clear_message(monkeypatch):
    import importlib
    from nwdash import report_groups
    importlib.reload(report_groups)
    called = {}
    monkeypatch.setattr(report_groups.report_render, "render_window", lambda c, w: called.setdefault("rendered", True))
    g = report_groups.ReportGroup(id="a", name="Ops", sections=["alerts"], recipients=["a@x.com"], cadence="daily")
    ok, msg = report_groups.send_on_demand(g, {"smtp": {}, "smtp_password": "", "connection": {}}, test=False)
    assert ok is False
    assert "reporting connection" in msg.lower()
    assert "rendered" not in called          # never attempted the bad connect

def test_fire_group_without_connection_marks_unhealthy_without_render(monkeypatch):
    import importlib
    from nwdash import report_groups
    importlib.reload(report_groups)
    called = {}
    monkeypatch.setattr(report_groups.report_render, "render_window", lambda c, w: called.setdefault("rendered", True))
    monkeypatch.setattr(report_groups.report_notify, "send_ops_alert", lambda *a, **k: called.setdefault("ops", True))
    g = report_groups.ReportGroup(id="a", name="Ops", sections=["alerts"], recipients=["a@x.com"], enabled=True, cadence="daily")
    report_groups.fire_group(g, {"smtp": {}, "smtp_password": "", "ops_address": "ops@x.com", "connection": {}})
    assert "rendered" not in called
    assert g.health.state == "unhealthy" and "reporting connection" in g.health.last_result.lower()
    assert g.health.next_run > 0
