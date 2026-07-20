import importlib
from nwdash import config, snapshots


def _fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EMAIL_CONFIG_FILE", tmp_path / "email_config.json")
    importlib.reload(snapshots)
    return snapshots


def test_save_smtp_config_writes_block_and_ops(tmp_path, monkeypatch):
    s = _fresh(tmp_path, monkeypatch)
    out = s.save_smtp_config({"host": "203.0.113.7", "port": 25, "security": "none",
                              "username": "", "from": "r@x.com", "password": "",
                              "opsAlertAddress": "ops@x.com"})
    assert out["ok"] is True
    pub = s.email_config_public()
    assert pub["smtp"]["host"] == "203.0.113.7" and pub["smtp"]["port"] == 25
    assert pub["smtp"]["from"] == "r@x.com"
    assert pub["opsAlertAddress"] == "ops@x.com"


def test_save_smtp_config_preserves_password_when_blank(tmp_path, monkeypatch):
    s = _fresh(tmp_path, monkeypatch)
    s.save_smtp_config({"host": "h", "port": 25, "security": "none", "from": "r@x.com",
                        "password": "secret", "opsAlertAddress": ""})
    assert s.saved_email_smtp_password() == "secret"
    s.save_smtp_config({"host": "h2", "port": 25, "security": "none", "from": "r@x.com",
                        "password": "", "opsAlertAddress": ""})
    assert s.saved_email_smtp_password() == "secret"
    assert s.email_config_public()["smtp"]["host"] == "h2"


def test_save_smtp_config_preserves_types(tmp_path, monkeypatch):
    s = _fresh(tmp_path, monkeypatch)
    import json
    (tmp_path / "email_config.json").write_text(json.dumps({"smtp": {}, "types": {"alert": {"recipients": ["a@x.com"]}}}), encoding="utf-8")
    s.save_smtp_config({"host": "h", "port": 25, "security": "none", "from": "r@x.com", "password": "", "opsAlertAddress": ""})
    cfg = json.loads((tmp_path / "email_config.json").read_text(encoding="utf-8"))
    assert cfg["types"]["alert"]["recipients"] == ["a@x.com"]
