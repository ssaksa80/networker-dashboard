import importlib
from http import HTTPStatus
from nwdash import config, report_api, report_jobs

def test_list_flags_legacy_when_automations_file_present(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOMATIONS_FILE", tmp_path / "automations.json")
    (tmp_path / "automations.json").write_text('{"x": {"schedule_type": "daily_report"}}', encoding="utf-8")
    importlib.reload(report_jobs); importlib.reload(report_api)
    status, body = report_api.handle_report_jobs({"action": "list"})
    assert status == HTTPStatus.OK
    assert body["legacyMigrationNeeded"] is True

def test_list_no_legacy_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTOMATIONS_FILE", tmp_path / "nope.json")
    importlib.reload(report_jobs); importlib.reload(report_api)
    status, body = report_api.handle_report_jobs({"action": "list"})
    assert body["legacyMigrationNeeded"] is False
