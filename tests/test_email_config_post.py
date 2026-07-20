import importlib
from http import HTTPStatus
from nwdash import config, snapshots, server

def test_handle_email_config_post_saves(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EMAIL_CONFIG_FILE", tmp_path / "email_config.json")
    importlib.reload(snapshots); importlib.reload(server)
    status, body = server.handle_email_config_post({"host": "203.0.113.7", "port": 25,
        "security": "none", "from": "r@x.com", "password": "", "opsAlertAddress": "ops@x.com"})
    assert status == HTTPStatus.OK and body["ok"] is True
    assert body["smtp"]["host"] == "203.0.113.7"
    assert body["opsAlertAddress"] == "ops@x.com"
    assert "encrypted_password" not in str(body)
