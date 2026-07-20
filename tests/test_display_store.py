import importlib
from nwdash import config, display


def _fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DISPLAY_TOKEN_FILE", tmp_path / "display_token.json")
    monkeypatch.setattr(config, "DISPLAY_CONNECTION_FILE", tmp_path / "display_connection.json")
    importlib.reload(display)
    return display


def test_token_get_or_create_is_stable(tmp_path, monkeypatch):
    d = _fresh(tmp_path, monkeypatch)
    t1 = d.get_or_create_token()
    t2 = d.get_or_create_token()
    assert t1 == t2 and len(t1) == 32 and all(c in "0123456789abcdef" for c in t1)


def test_token_validate(tmp_path, monkeypatch):
    d = _fresh(tmp_path, monkeypatch)
    t = d.get_or_create_token()
    assert d.validate_token(t) is True
    assert d.validate_token("0" * 32) is False
    assert d.validate_token("nope") is False


def test_token_rotate_invalidates_old(tmp_path, monkeypatch):
    d = _fresh(tmp_path, monkeypatch)
    old = d.get_or_create_token()
    new = d.rotate_token()
    assert new != old
    assert d.validate_token(old) is False
    assert d.validate_token(new) is True


def test_token_revoke(tmp_path, monkeypatch):
    d = _fresh(tmp_path, monkeypatch)
    t = d.get_or_create_token()
    assert d.revoke_token() is True
    assert d.validate_token(t) is False
    assert d.current_token() == ""


def test_token_persists_across_reload(tmp_path, monkeypatch):
    d = _fresh(tmp_path, monkeypatch)
    t = d.get_or_create_token()
    importlib.reload(display)
    assert display.current_token() == t


def test_connection_roundtrip_seals_password(tmp_path, monkeypatch):
    d = _fresh(tmp_path, monkeypatch)
    assert d.load_connection() is None
    d.save_connection({"rest_api_host": "h", "rest_api_port": 9090, "username": "u",
                       "password": "pw", "api_mode": "nwui"})
    conn = d.load_connection()
    assert conn["rest_api_host"] == "h" and conn["username"] == "u"
    assert conn.get("encrypted_password") and "password" not in conn
    from nwdash.report_cred import decrypt_credential_password
    assert decrypt_credential_password(conn["encrypted_password"]) == "pw"


def test_connection_clear(tmp_path, monkeypatch):
    d = _fresh(tmp_path, monkeypatch)
    d.save_connection({"rest_api_host": "h", "username": "u", "password": "pw"})
    assert d.clear_connection() is True
    assert d.load_connection() is None
