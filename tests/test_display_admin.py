import importlib
from http import HTTPStatus
from nwdash import display, server, config

def _reload(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DISPLAY_TOKEN_FILE", tmp_path / "t.json")
    monkeypatch.setattr(config, "DISPLAY_CONNECTION_FILE", tmp_path / "c.json")
    importlib.reload(display); importlib.reload(server)

def test_get_token_creates_and_returns(tmp_path, monkeypatch):
    _reload(tmp_path, monkeypatch)
    status, body = server.handle_display_config({"action": "get"})
    assert status == HTTPStatus.OK and len(body["token"]) == 32
    assert body["hasConnection"] is False

def test_rotate_changes_token(tmp_path, monkeypatch):
    _reload(tmp_path, monkeypatch)
    _, b1 = server.handle_display_config({"action": "get"})
    _, b2 = server.handle_display_config({"action": "rotate"})
    assert b2["token"] != b1["token"]

def test_set_connection_validates_then_saves(tmp_path, monkeypatch):
    _reload(tmp_path, monkeypatch)
    from nwdash import report_render
    monkeypatch.setattr(server.report_render, "render",
                        lambda cred: report_render.RenderResult(True, {"summary": {}}, ""))
    status, body = server.handle_display_config({"action": "set-connection",
        "credential": {"rest_api_host": "h", "rest_api_port": 9090, "username": "u", "password": "pw", "api_mode": "nwui"}})
    assert status == HTTPStatus.OK and body["ok"] is True
    assert display.load_connection()["username"] == "u"

def test_set_connection_rejects_bad_render(tmp_path, monkeypatch):
    _reload(tmp_path, monkeypatch)
    from nwdash import report_render
    monkeypatch.setattr(server.report_render, "render",
                        lambda cred: report_render.RenderResult(False, {}, "login rejected"))
    status, body = server.handle_display_config({"action": "set-connection",
        "credential": {"rest_api_host": "h", "username": "u", "password": "bad"}})
    assert status == HTTPStatus.BAD_REQUEST and body["ok"] is False
    assert display.load_connection() is None
