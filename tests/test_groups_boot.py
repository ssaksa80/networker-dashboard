import importlib
from nwdash import report_groups, main

def test_group_cfg_provider_shape(monkeypatch):
    importlib.reload(report_groups)
    cfg = main.group_cfg_provider()
    assert set(["smtp", "smtp_password", "ops_address", "connection"]) <= set(cfg)
