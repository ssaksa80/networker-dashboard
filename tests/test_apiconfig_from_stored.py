from nwdash.report_cred import apiconfig_from_stored, encrypt_credential_password
from nwdash.secrets import encrypt_process_secret


def _snapshot():
    return {
        "session_id": "s1", "created_at": 1.0, "last_used": 2.0,
        "encrypted_networker_password": encrypt_process_secret("realpw"),
        "encrypted_wmi_password": "",
        "config": {
            "rest_api_host": "10.0.0.9", "rest_api_port": 9090,
            "backup_server_host": "bkp", "backup_server_port": 9091,
            "username": "administrator", "api_mode": "nwui", "api_version": "v3",
            "report_range": "24h", "custom_start_date": "", "custom_end_date": "",
            "use_wmi_health": True, "wmi_username": "wmiu",
            "timeout_seconds": 45, "verify_tls": True, "use_authc_header": True,
        },
    }


def test_snapshot_shape_carries_every_field_verbatim():
    cfg = apiconfig_from_stored(_snapshot())
    assert cfg.use_authc_header is True
    assert cfg.verify_tls is True
    assert cfg.api_version == "v3"
    assert cfg.backup_server_host == "bkp" and cfg.backup_server_port == 9091
    assert cfg.use_wmi_health is True and cfg.wmi_username == "wmiu"
    assert cfg.timeout_seconds == 45
    assert cfg.username == "administrator"
    assert cfg.password == "realpw"


def test_legacy_flat_shape_still_works():
    flat = {"rest_api_host": "h", "rest_api_port": 9090, "username": "u",
            "encrypted_password": encrypt_credential_password("pw"), "api_mode": "nwui"}
    cfg = apiconfig_from_stored(flat)
    assert cfg.rest_api_host == "h" and cfg.username == "u" and cfg.password == "pw"


def test_empty_returns_blank_host():
    cfg = apiconfig_from_stored({})
    assert cfg.rest_api_host == ""


def test_render_uses_snapshot_shape(monkeypatch):
    """render() (used by the TV-wall refresh fallback) must understand the
    session-snapshot shape, not just the legacy flat dict."""
    import importlib
    from http import HTTPStatus
    from nwdash import report_render
    from nwdash.secrets import encrypt_process_secret
    importlib.reload(report_render)
    captured = {}
    def fake_build(cfg):
        captured["host"] = cfg.rest_api_host
        captured["authc"] = cfg.use_authc_header
        return HTTPStatus.OK, {"summary": {}}
    monkeypatch.setattr(report_render, "build_dashboard", fake_build)
    snap = {"session_id": "s1", "encrypted_networker_password": encrypt_process_secret("pw"),
            "config": {"rest_api_host": "10.0.0.9", "rest_api_port": 9090, "username": "u",
                       "api_mode": "nwui", "use_authc_header": True}}
    res = report_render.render(snap)
    assert res.ok is True
    assert captured["host"] == "10.0.0.9"      # NOT blank
    assert captured["authc"] is True           # carried verbatim
