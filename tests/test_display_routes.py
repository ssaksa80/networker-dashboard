import importlib
from http import HTTPStatus
from nwdash import display, server, config


def _make_handler():
    return server.DashboardHandler.__new__(server.DashboardHandler)


class _Cap:
    def __init__(self):
        self.status = None
        self.body = None
        self.bytes = None
        self.ctype = None


def _wire(h):
    cap = _Cap()
    h._send_json = lambda s, b: (setattr(cap, "status", s), setattr(cap, "body", b))
    h._send_bytes = lambda s, b, c: (setattr(cap, "status", s), setattr(cap, "bytes", b), setattr(cap, "ctype", c))
    h._send_error_json = lambda s, m: (setattr(cap, "status", s), setattr(cap, "body", {"error": m}))
    return cap


def test_api_display_valid_token_returns_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DISPLAY_TOKEN_FILE", tmp_path / "t.json")
    importlib.reload(display)
    importlib.reload(server)
    tok = display.get_or_create_token()
    monkeypatch.setattr(server, "shared_dashboard_payload", lambda: {"ok": True, "dashboard": {"summary": {}}})
    h = _make_handler()
    cap = _wire(h)
    h._handle_display_api(f"/api/display/{tok}")
    assert cap.status == HTTPStatus.OK and cap.body["ok"] is True
    assert "dashboard" in cap.body
    assert "encrypted_password" not in str(cap.body)


def test_api_display_bad_token_gone(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DISPLAY_TOKEN_FILE", tmp_path / "t.json")
    importlib.reload(display)
    importlib.reload(server)
    display.get_or_create_token()
    h = _make_handler()
    cap = _wire(h)
    h._handle_display_api("/api/display/" + "0" * 32)
    assert cap.status == HTTPStatus.GONE
