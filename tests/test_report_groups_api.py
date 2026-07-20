import importlib
from http import HTTPStatus
from nwdash import report_groups, report_groups_api as api

def _payload(**kw):
    b = {"action": "create", "name": "Ops", "sections": ["backup_sla", "alerts"],
         "recipients": "a@x.com, b@x.com", "cadence": "weekly", "sendTime": "07:00", "enabled": True}
    b.update(kw); return b

def test_create_validates_and_lists_ordered(monkeypatch):
    importlib.reload(report_groups); importlib.reload(api)
    monkeypatch.setattr(api, "_reporting_connection", lambda: {"rest_api_host": "h"})
    monkeypatch.setattr(api.report_groups, "persist_groups", lambda: None)
    st, body = api.handle_report_groups(_payload())
    assert st == HTTPStatus.OK and body["ok"] is True
    gid = body["id"]
    st, body = api.handle_report_groups({"action": "list"})
    assert body["groups"][0]["id"] == gid
    assert body["groups"][0]["recipients"] == ["a@x.com", "b@x.com"]
    assert "health" in body["groups"][0]

def test_create_rejects_invalid(monkeypatch):
    importlib.reload(report_groups); importlib.reload(api)
    monkeypatch.setattr(api, "_reporting_connection", lambda: {"rest_api_host": "h"})
    st, body = api.handle_report_groups(_payload(name="", sections=[]))
    assert st == HTTPStatus.BAD_REQUEST and body["ok"] is False
    assert "name" in body["errors"] and "sections" in body["errors"]

def test_enabled_requires_connection(monkeypatch):
    importlib.reload(report_groups); importlib.reload(api)
    monkeypatch.setattr(api, "_reporting_connection", lambda: None)
    monkeypatch.setattr(api.report_groups, "persist_groups", lambda: None)
    st, body = api.handle_report_groups(_payload())
    assert st == HTTPStatus.OK and body["ok"] is True
    assert report_groups.get_group(body["id"]).enabled is False
    assert body.get("warning")

def test_toggle_delete_reorder_send(monkeypatch):
    importlib.reload(report_groups); importlib.reload(api)
    monkeypatch.setattr(api, "_reporting_connection", lambda: {"rest_api_host": "h"})
    monkeypatch.setattr(api.report_groups, "persist_groups", lambda: None)
    api.report_groups.put_group(report_groups.ReportGroup(id="a", name="A", sections=["alerts"], recipients=["a@x.com"]))
    api.report_groups.put_group(report_groups.ReportGroup(id="b", name="B", sections=["alerts"], recipients=["b@x.com"]))
    assert api.handle_report_groups({"action": "reorder", "order": ["b", "a"]})[0] == HTTPStatus.OK
    assert [g.id for g in report_groups.groups_ordered()] == ["b", "a"]
    assert api.handle_report_groups({"action": "toggle", "id": "a", "enabled": False})[0] == HTTPStatus.OK
    monkeypatch.setattr(api.report_groups, "send_on_demand", lambda g, cfg, test: (True, "Test sent."))
    st, body = api.handle_report_groups({"action": "send", "id": "a", "test": True})
    assert st == HTTPStatus.OK and body["ok"] is True and "Test" in body["message"]
    assert api.handle_report_groups({"action": "delete", "id": "b"})[0] == HTTPStatus.OK
    assert report_groups.get_group("b") is None
