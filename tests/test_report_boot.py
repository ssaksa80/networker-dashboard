import importlib
from nwdash import report_jobs, main


def test_report_cfg_provider_shape(monkeypatch):
    importlib.reload(report_jobs)
    monkeypatch.setattr(main, "_report_smtp_config", lambda: ({"host": "h", "port": 25}, ""))
    cfg = main.report_cfg_provider()
    assert "smtp" in cfg and "smtp_password" in cfg and "ops_address" in cfg
