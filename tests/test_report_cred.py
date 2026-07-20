from nwdash.report_cred import encrypt_credential_password, decrypt_credential_password, credential_to_apiconfig

def test_password_roundtrip():
    token = encrypt_credential_password("s3cret")
    assert token and token != "s3cret"
    assert decrypt_credential_password(token) == "s3cret"

def test_empty_password_roundtrips_to_empty():
    assert decrypt_credential_password(encrypt_credential_password("")) == ""

def test_bad_token_returns_empty():
    assert decrypt_credential_password("not-a-real-token") == ""

def test_credential_to_apiconfig_maps_fields_and_injects_password():
    cred = {
        "rest_api_host": "10.0.0.9", "rest_api_port": 9090,
        "backup_server_host": "10.0.0.9", "backup_server_port": 9090,
        "username": "administrator", "encrypted_password": encrypt_credential_password("pw"),
        "api_mode": "nwui", "api_version": "auto", "verify_tls": False, "report_range": "7d",
    }
    cfg = credential_to_apiconfig(cred)
    assert cfg.rest_api_host == "10.0.0.9"
    assert cfg.username == "administrator"
    assert cfg.password == "pw"
    assert cfg.api_mode == "nwui"
    assert cfg.report_range == "7d"
