# DSO TV Display Token Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the DSO TV wall render live with no login via a revocable capability URL `/tv/<token>`, while a dashboard password protects admin + mutations, and a stored display connection keeps the wall live 24/7 with nobody logged in.

**Architecture:** A persistent display token (in `data/display_token.json`) authorizes two new pre-auth routes — `/tv/<token>` (serves the existing TV page) and `/api/display/<token>` (returns a read-only, sanitized bundle). `tv.js` becomes token-aware: in token mode it polls the bundle instead of the cookie-gated APIs. A stored display connection (NetWorker credential, machine-DPAPI, reusing 2.9.0 `report_cred`) drives the background shared-refresh via the session-free render path when no interactive session is present. Admin UI manages the token + connection behind auth.

**Tech Stack:** Python 3.12 stdlib, existing `nwdash` package, pytest, vanilla-JS SPA assets.

**Reused primitives:**
- `nwdash/models.py`: `shared_dashboard_payload()`, `set_shared_dashboard(session_id, dashboard)`, `_shared_dashboard_refresh_once()` (refresh loop body, returns early when `SHARED_DASHBOARD_STATE["sessionId"]` is empty), `SHARED_DASHBOARD_LOCK`, `SHARED_DASHBOARD_STATE`.
- `nwdash/report_cred.py`: `encrypt_credential_password`, `decrypt_credential_password`, `credential_to_apiconfig`.
- `nwdash/report_render.py`: `render(cred) -> RenderResult(ok, dashboard, error)`.
- `nwdash/server.py`: `DashboardHandler.do_GET`/`do_POST`; pre-auth token routes live at the `/view/<token>` branch (~line 355) BEFORE the `if _cfg.AUTH_ENABLED and not self._authenticated()` gate (~line 396); response helpers `self._send_json(status, body)`, `self._send_bytes(status, bytes, ctype)`, `self._send_error_json(status, msg)`; POST body via `self._read_json_body()`; the POST `allowed` path set (~line 538).
- `nwdash/ui.py`: `tv_page_html()`.
- Token format everywhere: 32 lowercase hex (`uuid.uuid4().hex`), route regex `[0-9a-f]{32}`.

---

## File Structure

- Create `nwdash/display.py` — display-token store + display-connection store (both persistent under `data/`).
- Modify `nwdash/config.py` — `DISPLAY_TOKEN_FILE`, `DISPLAY_CONNECTION_FILE` constants.
- Modify `nwdash/server.py` — pre-auth `/tv/<token>` + `/api/display/<token>`; authed admin `/api/display-config`.
- Modify `nwdash/models.py` — shared-refresh falls back to the display connection when no session.
- Modify `nwdash/assets/tv.js` — token-aware data source.
- Modify `nwdash/assets/dashboard.html`, `app.js`, `app.css` — admin "TV / Display" section.
- Modify `deploy/build-bundle.ps1` — add `nwdash\display.py` to `$shipFiles`.
- Tests under `tests/`.

Repo gotcha (applies to every task that adds a `nwdash/*.py`): `tests/test_deploy.py::TestBundleAllowList` requires the file in `$shipFiles`. Run the FULL suite before each commit.

---

## Task 1: Config constants

**Files:** Modify `nwdash/config.py`

- [ ] **Step 1:** After `REPORT_CACHE_DIR = DATA_DIR / "report_cache"`, add:

```python
DISPLAY_TOKEN_FILE = DATA_DIR / "display_token.json"
DISPLAY_CONNECTION_FILE = DATA_DIR / "display_connection.json"
```

- [ ] **Step 2:** Verify: `python -c "from nwdash.config import DISPLAY_TOKEN_FILE, DISPLAY_CONNECTION_FILE; print(DISPLAY_TOKEN_FILE.name, DISPLAY_CONNECTION_FILE.name)"`
Expected: `display_token.json display_connection.json`

- [ ] **Step 3:** Commit:
```bash
git add nwdash/config.py
git commit -m "feat(tv): add display token + connection path constants"
```

---

## Task 2: display.py — token + connection stores

**Files:** Create `nwdash/display.py`; Test `tests/test_display_store.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_display_store.py
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
    assert conn.get("encrypted_password") and "password" not in conn   # sealed, no plaintext
    from nwdash.report_cred import decrypt_credential_password
    assert decrypt_credential_password(conn["encrypted_password"]) == "pw"

def test_connection_clear(tmp_path, monkeypatch):
    d = _fresh(tmp_path, monkeypatch)
    d.save_connection({"rest_api_host": "h", "username": "u", "password": "pw"})
    assert d.clear_connection() is True
    assert d.load_connection() is None
```

- [ ] **Step 2:** Run → FAIL (`ModuleNotFoundError`). `python -m pytest tests/test_display_store.py -v`

- [ ] **Step 3: Implement `nwdash/display.py`**

```python
"""Persistent display token and display connection for the no-login TV wall.

The display token is a durable capability (unlike session-scoped share tokens):
whoever holds the /tv/<token> URL sees the read-only wall. The display
connection is a NetWorker credential (password sealed with machine-DPAPI via
report_cred) used to keep the shared dashboard live when no one is logged in."""
from __future__ import annotations

import json
import threading
import uuid
from typing import Any

from . import config
from .report_cred import encrypt_credential_password

_LOCK = threading.Lock()


def _read(path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write(path, data: dict[str, Any]) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


# ── display token ────────────────────────────────────────────────────────────
def current_token() -> str:
    with _LOCK:
        return str(_read(config.DISPLAY_TOKEN_FILE).get("token") or "")


def get_or_create_token() -> str:
    with _LOCK:
        tok = str(_read(config.DISPLAY_TOKEN_FILE).get("token") or "")
        if not tok:
            tok = uuid.uuid4().hex
            _write(config.DISPLAY_TOKEN_FILE, {"token": tok})
        return tok


def rotate_token() -> str:
    with _LOCK:
        tok = uuid.uuid4().hex
        _write(config.DISPLAY_TOKEN_FILE, {"token": tok})
        return tok


def revoke_token() -> bool:
    with _LOCK:
        if not _read(config.DISPLAY_TOKEN_FILE).get("token"):
            return False
        _write(config.DISPLAY_TOKEN_FILE, {})
        return True


def validate_token(token: str) -> bool:
    if not token or len(token) != 32:
        return False
    return token == current_token()


# ── display connection ───────────────────────────────────────────────────────
def save_connection(cred: dict[str, Any]) -> None:
    """Persist a NetWorker credential with the password sealed (no plaintext)."""
    sealed = {k: v for k, v in cred.items() if k != "password"}
    sealed["encrypted_password"] = encrypt_credential_password(str(cred.get("password") or ""))
    with _LOCK:
        _write(config.DISPLAY_CONNECTION_FILE, sealed)


def load_connection() -> dict[str, Any] | None:
    with _LOCK:
        data = _read(config.DISPLAY_CONNECTION_FILE)
        return data or None


def clear_connection() -> bool:
    with _LOCK:
        if not config.DISPLAY_CONNECTION_FILE.exists():
            return False
        try:
            config.DISPLAY_CONNECTION_FILE.unlink()
            return True
        except OSError:
            return False
```

- [ ] **Step 4:** Add `'nwdash\display.py',` to `deploy/build-bundle.ps1` `$shipFiles` (alphabetical, near `config.py`). Run `python -m pytest tests/test_display_store.py -v` (7 passed), then FULL suite `python -m pytest -q`.

- [ ] **Step 5:** Commit:
```bash
git add nwdash/display.py tests/test_display_store.py deploy/build-bundle.ps1
git commit -m "feat(tv): display token + display connection stores (machine-DPAPI)"
```

---

## Task 3: pre-auth routes `/tv/<token>` + `/api/display/<token>`

**Files:** Modify `nwdash/server.py`; Test `tests/test_display_routes.py`

Both routes go BEFORE the auth gate, right after the existing `/view/<token>` / `/api/view/<token>` block (~line 375). `/api/display/<token>` returns a bundled read-only payload: the shared dashboard plus theme, so `tv.js` needs a single public endpoint.

- [ ] **Step 1: Locate** the `/view/` branch and the auth gate:
`grep -n "/view/\|_handle_token_dashboard\|not self._authenticated\|def do_GET" nwdash/server.py`

- [ ] **Step 2: Write the failing test** `tests/test_display_routes.py`:

```python
import importlib
from http import HTTPStatus
from nwdash import display, server

class _Resp:
    def __init__(self): self.status=None; self.body=None; self.ctype=None; self.bytes=None
def _make_handler(monkeypatch):
    """Build a DashboardHandler without a socket; capture responses."""
    h = server.DashboardHandler.__new__(server.DashboardHandler)
    r = _Resp()
    monkeypatch.setattr(h, "_send_json", lambda s, b: (setattr(r,"status",s), setattr(r,"body",b)), raising=False)
    monkeypatch.setattr(h, "_send_bytes", lambda s, b, c: (setattr(r,"status",s), setattr(r,"bytes",b), setattr(r,"ctype",c)), raising=False)
    monkeypatch.setattr(h, "_send_error_json", lambda s, m: (setattr(r,"status",s), setattr(r,"body",{"error":m})), raising=False)
    return h, r

def test_api_display_valid_token_returns_payload(tmp_path, monkeypatch):
    from nwdash import config
    monkeypatch.setattr(config, "DISPLAY_TOKEN_FILE", tmp_path / "t.json")
    importlib.reload(display); importlib.reload(server)
    tok = display.get_or_create_token()
    monkeypatch.setattr(server, "shared_dashboard_payload", lambda: {"ok": True, "dashboard": {"summary": {}}})
    monkeypatch.setattr(server, "load_ui_theme", lambda: "midnight", raising=False)
    h, r = _make_handler(monkeypatch)
    h._handle_display_api(f"/api/display/{tok}")
    assert r.status == HTTPStatus.OK and r.body["ok"] is True
    assert "dashboard" in r.body
    # never leaks a credential field
    assert "encrypted_password" not in str(r.body)

def test_api_display_bad_token_gone(tmp_path, monkeypatch):
    from nwdash import config
    monkeypatch.setattr(config, "DISPLAY_TOKEN_FILE", tmp_path / "t.json")
    importlib.reload(display); importlib.reload(server)
    display.get_or_create_token()
    h, r = _make_handler(monkeypatch)
    h._handle_display_api("/api/display/" + "0"*32)
    assert r.status == HTTPStatus.GONE
```

- [ ] **Step 3: Implement.** Add imports at the top of `server.py` (near the other `from .models import` / `from .display import`):
```python
from .display import validate_token as validate_display_token, current_token as current_display_token, get_or_create_token, rotate_token, revoke_token, save_connection as save_display_connection, load_connection as load_display_connection, clear_connection as clear_display_connection
```
Add a handler method to `DashboardHandler`:
```python
    def _handle_display_api(self, path: str) -> None:
        token = path[len("/api/display/"):].strip("/")
        if not validate_display_token(token):
            self._send_error_json(HTTPStatus.GONE, "This display link has expired or been revoked.")
            return
        payload = shared_dashboard_payload()   # already sanitized, session-independent
        try:
            theme = load_ui_theme() or "default"
        except Exception:  # noqa: BLE001
            theme = "default"
        body = dict(payload) if isinstance(payload, dict) else {"ok": False}
        body["theme"] = theme
        self._send_json(HTTPStatus.OK, body)
```
(Confirm `shared_dashboard_payload` and `load_ui_theme` are imported in server.py; add imports if missing — `from .models import shared_dashboard_payload`, and `load_ui_theme` from wherever it is: `grep -rn "def load_ui_theme" nwdash/`.)

In `do_GET`, right after the `/api/view/` branch, add the two pre-auth routes:
```python
            if path.startswith("/api/display/"):
                self._handle_display_api(path)
                return
            if path.startswith("/tv/"):
                token = path[len("/tv/"):].strip("/")
                if validate_display_token(token):
                    self._send_bytes(HTTPStatus.OK, tv_page_html().encode("utf-8"), "text/html; charset=utf-8")
                else:
                    self._send_bytes(HTTPStatus.OK,
                                     b"<html><body><p>This display link has expired or been revoked.</p></body></html>",
                                     "text/html; charset=utf-8")
                return
```
(These sit BEFORE the `if _cfg.AUTH_ENABLED and not self._authenticated()` gate, exactly like the `/view/` routes.)

- [ ] **Step 4:** Run `python -m pytest tests/test_display_routes.py -v` (2 passed), then FULL suite.
Note: if `_make_handler` monkeypatch approach fails due to `DashboardHandler` internals, simplify by testing `_handle_display_api` on a minimal subclass instance that defines the three `_send_*` methods directly. Keep the assertions.

- [ ] **Step 5:** Commit:
```bash
git add nwdash/server.py tests/test_display_routes.py
git commit -m "feat(tv): pre-auth /tv/<token> + /api/display/<token> capability routes"
```

---

## Task 4: admin endpoints `/api/display-config`

**Files:** Modify `nwdash/server.py`; Test `tests/test_display_admin.py`

Authed (post-gate) POST endpoint to read/rotate/revoke the token and set/clear the connection. Validates the connection with a live render before saving.

- [ ] **Step 1: Write the failing test** `tests/test_display_admin.py`:

```python
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
```

- [ ] **Step 2:** Run → FAIL. `python -m pytest tests/test_display_admin.py -v`

- [ ] **Step 3: Implement** in `server.py` (add `from . import report_render` at top if not present):

```python
def handle_display_config(payload: dict) -> tuple[int, dict]:
    from . import display, report_render
    action = str(payload.get("action") or "").strip().lower()
    if action in ("get", "rotate", "revoke"):
        if action == "rotate":
            token = display.rotate_token()
        elif action == "revoke":
            display.revoke_token(); token = ""
        else:
            token = display.get_or_create_token()
        return HTTPStatus.OK, {"ok": True, "token": token, "hasConnection": display.load_connection() is not None}
    if action == "set-connection":
        cred = payload.get("credential") if isinstance(payload.get("credential"), dict) else {}
        res = report_render.render(_cred_for_render(cred))
        if not res.ok:
            return HTTPStatus.BAD_REQUEST, {"ok": False, "message": res.error or "Could not connect to NetWorker with these credentials."}
        display.save_connection(cred)
        return HTTPStatus.OK, {"ok": True, "hasConnection": True}
    if action == "clear-connection":
        display.clear_connection()
        return HTTPStatus.OK, {"ok": True, "hasConnection": False}
    return HTTPStatus.BAD_REQUEST, {"ok": False, "message": f"Unknown action {action!r}."}


def _cred_for_render(cred: dict) -> dict:
    """report_render.render expects a stored-shape cred (encrypted_password). For
    validation we seal the just-entered plaintext so the same code path runs."""
    from .report_cred import encrypt_credential_password
    out = {k: v for k, v in cred.items() if k != "password"}
    out["encrypted_password"] = encrypt_credential_password(str(cred.get("password") or ""))
    return out
```
Route it in `do_POST` (AFTER the auth gate, with the other authed POST endpoints) and add `/api/display-config` to the `allowed` path set:
```python
        if path == "/api/display-config":
            status, body = handle_display_config(payload)
            return self._send_json(status, body)
```

- [ ] **Step 4:** Run tests (4 passed), then FULL suite.

- [ ] **Step 5:** Commit:
```bash
git add nwdash/server.py tests/test_display_admin.py
git commit -m "feat(tv): authed /api/display-config (token get/rotate/revoke, validated connection)"
```

---

## Task 5: shared-refresh fallback to the display connection

**Files:** Modify `nwdash/models.py`; Test `tests/test_display_feed.py`

When no interactive session drives the shared refresh, render from the display connection so the wall stays live.

- [ ] **Step 1: Write the failing test** `tests/test_display_feed.py`:

```python
import importlib
from nwdash import models, display, config

def test_refresh_uses_display_connection_when_no_session(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DISPLAY_CONNECTION_FILE", tmp_path / "c.json")
    importlib.reload(display); importlib.reload(models)
    # no session
    with models.SHARED_DASHBOARD_LOCK:
        models.SHARED_DASHBOARD_STATE["sessionId"] = ""
    display.save_connection({"rest_api_host": "h", "username": "u", "password": "pw", "api_mode": "nwui"})
    captured = {}
    monkeypatch.setattr(models, "set_shared_dashboard",
                        lambda sid, dash: captured.update(sid=sid, dash=dash))
    # patch the render used inside the fallback
    import nwdash.report_render as rr
    monkeypatch.setattr(rr, "render", lambda cred: rr.RenderResult(True, {"ok": True, "summary": {"totalJobs": 3}}, ""))
    models._shared_dashboard_refresh_once()
    assert captured.get("dash", {}).get("summary", {}).get("totalJobs") == 3

def test_refresh_noop_when_no_session_and_no_connection(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DISPLAY_CONNECTION_FILE", tmp_path / "none.json")
    importlib.reload(display); importlib.reload(models)
    with models.SHARED_DASHBOARD_LOCK:
        models.SHARED_DASHBOARD_STATE["sessionId"] = ""
    called = {}
    monkeypatch.setattr(models, "set_shared_dashboard", lambda sid, dash: called.update(hit=True))
    models._shared_dashboard_refresh_once()
    assert "hit" not in called
```

- [ ] **Step 2:** Run → FAIL (fallback not implemented). `python -m pytest tests/test_display_feed.py -v`

- [ ] **Step 3: Implement.** In `nwdash/models.py`, at the START of `_shared_dashboard_refresh_once`, replace the early `if not session_id: return` with a display-connection fallback:

```python
    with SHARED_DASHBOARD_LOCK:
        session_id = str(SHARED_DASHBOARD_STATE.get("sessionId") or "")
    if not session_id:
        # No interactive session: keep the TV wall live from the persistent
        # display connection via the session-free render path. Late imports
        # avoid a circular import at module load.
        try:
            from . import display, report_render
        except Exception:  # noqa: BLE001
            return
        conn = display.load_connection()
        if not conn:
            return
        res = report_render.render(conn)
        if res.ok:
            set_shared_dashboard("display", res.dashboard)
        else:
            with SHARED_DASHBOARD_LOCK:
                SHARED_DASHBOARD_STATE["lastError"] = res.error or "Display connection refresh failed."
        return
```
Keep the rest of the function (the session-based path) unchanged below.

- [ ] **Step 4:** Run tests (2 passed), then FULL suite. Confirm no circular-import error: `python -c "import nwdash.models, nwdash.display, nwdash.report_render"`.

- [ ] **Step 5:** Commit:
```bash
git add nwdash/models.py tests/test_display_feed.py
git commit -m "feat(tv): shared-refresh falls back to display connection when no session"
```

---

## Task 6: token-aware `tv.js`

**Files:** Modify `nwdash/assets/tv.js`; Test `tests/test_tv_token_assets.py`

In token mode (URL path `/tv/<token>`), poll `/api/display/<token>` instead of the cookie-gated APIs, and skip SSE.

- [ ] **Step 1: Write the failing asset test** `tests/test_tv_token_assets.py`:

```python
from pathlib import Path
TV = Path(__file__).resolve().parents[1] / "nwdash" / "assets" / "tv.js"

def test_tv_js_is_token_aware():
    js = TV.read_text(encoding="utf-8")
    assert "/api/display/" in js
    assert "DISPLAY_TOKEN" in js or "displayToken" in js
```

- [ ] **Step 2:** Run → FAIL.

- [ ] **Step 3: Implement.** Near the TOP of `tv.js`, add token detection and make the dashboard fetch token-aware. Read the current fetch of `/api/current-dashboard` (around line 433) and the SSE setup (around line 458).

Add near the top (after the opening IIFE/vars):
```javascript
  // DSO wall: when opened as /tv/<token> this page runs WITHOUT login and
  // reads the read-only display feed instead of the cookie-gated APIs.
  var _m = location.pathname.match(/\/tv\/([0-9a-f]{32})/);
  var DISPLAY_TOKEN = _m ? _m[1] : "";
  function dashboardUrl() {
    return DISPLAY_TOKEN ? ("/api/display/" + DISPLAY_TOKEN) : "/api/current-dashboard";
  }
```
Change the dashboard fetch to use `dashboardUrl()`:
```javascript
    return fetch(dashboardUrl(), { cache: "no-store" })
```
Guard SSE so token mode falls back to polling (SSE needs the cookie):
```javascript
  function startStream() {
    if (DISPLAY_TOKEN) { return; }   // token mode: poll only, no SSE
    if (sseSource || !window.EventSource) return;
    sseSource = new EventSource("/api/stream");
    ...
```
Add a poll in token mode where SSE would have driven re-render — near the existing `setInterval(refreshSnapshots, SNAP_REFRESH_MS);` add:
```javascript
  if (DISPLAY_TOKEN) { setInterval(function () { /* re-fetch dashboard */ loadDashboard(); }, 60000); }
```
Use the REAL name of the dashboard-loading function you find (the one that wraps the `/api/current-dashboard` fetch). If snapshots/theme in token mode 401 (they hit cookie APIs), wrap those fetches so a failure is non-fatal (the wall still shows the dashboard from the display feed, which already includes `theme`). Apply the theme from the display payload's `theme` field in token mode.

- [ ] **Step 4:** Verify JS parses: `node -c nwdash/assets/tv.js` (if `node` available; else skip). Run `python -m pytest tests/test_tv_token_assets.py -v` (2 passed), then FULL suite.

- [ ] **Step 5:** Commit:
```bash
git add nwdash/assets/tv.js tests/test_tv_token_assets.py
git commit -m "feat(tv): tv.js reads the token display feed in no-login wall mode"
```

---

## Task 7: admin "TV / Display" UI section

**Files:** Modify `nwdash/assets/dashboard.html`, `app.js`, `app.css`; Test `tests/test_tv_admin_assets.py`

- [ ] **Step 1: Write the failing asset test** `tests/test_tv_admin_assets.py`:

```python
from pathlib import Path
A = Path(__file__).resolve().parents[1] / "nwdash" / "assets"

def test_dashboard_has_display_section():
    assert 'id="tvDisplayPanel"' in (A / "dashboard.html").read_text(encoding="utf-8")

def test_appjs_wires_display_config():
    js = (A / "app.js").read_text(encoding="utf-8")
    assert "/api/display-config" in js
    assert "renderDisplayConfig" in js
```

- [ ] **Step 2:** Run → FAIL.

- [ ] **Step 3: Markup** in `dashboard.html` (new panel in the main content flow):
```html
<section id="tvDisplayPanel" class="panel">
  <header class="panel-head"><h2>TV / Display</h2></header>
  <p>Point the DSO TV at this URL — it shows the live wall with no login:</p>
  <div class="tv-url-row">
    <input id="tvDisplayUrl" readonly>
    <button id="tvRotateBtn" type="button">Rotate</button>
    <button id="tvRevokeBtn" type="button">Revoke</button>
  </div>
  <h3>Display connection (keeps the wall live 24/7)</h3>
  <form id="tvConnForm" class="report-form">
    <label>NetWorker host <input name="rest_api_host" required></label>
    <label>Port <input name="rest_api_port" type="number" value="9090"></label>
    <label>Username <input name="username" required></label>
    <label>Password <input name="password" type="password" required></label>
    <div id="tvConnError" class="form-error" role="alert"></div>
    <span id="tvConnState" class="health-badge health-idle">not set</span>
    <button type="submit">Validate &amp; save</button>
  </form>
</section>
```

- [ ] **Step 4: JS** in `app.js`:
```javascript
async function renderDisplayConfig() {
  const panel = document.getElementById("tvDisplayPanel");
  if (!panel) return;
  const r = await fetch("/api/display-config", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({action: "get"})});
  const d = await r.json();
  const urlBox = document.getElementById("tvDisplayUrl");
  urlBox.value = d.token ? `${location.origin}/tv/${d.token}` : "(revoked — click Rotate to create one)";
  const st = document.getElementById("tvConnState");
  st.textContent = d.hasConnection ? "connection set" : "not set";
  st.className = "health-badge " + (d.hasConnection ? "health-ok" : "health-idle");
}
async function _displayAction(action) {
  await fetch("/api/display-config", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({action})});
  renderDisplayConfig();
}
async function submitDisplayConn(ev) {
  ev.preventDefault();
  const f = ev.target, err = document.getElementById("tvConnError");
  err.textContent = "Validating connection…";
  const r = await fetch("/api/display-config", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({action: "set-connection", credential: {
      rest_api_host: f.rest_api_host.value, rest_api_port: Number(f.rest_api_port.value),
      backup_server_host: f.rest_api_host.value, backup_server_port: Number(f.rest_api_port.value),
      username: f.username.value, password: f.password.value, api_mode: "nwui"}})});
  const d = await r.json();
  if (!d.ok) { err.textContent = "Not saved — " + (d.message || "validation failed"); return; }
  err.textContent = ""; f.reset(); renderDisplayConfig();
}
function initDisplayConfig() {
  const panel = document.getElementById("tvDisplayPanel");
  if (!panel) return;
  document.getElementById("tvRotateBtn").addEventListener("click", () => _displayAction("rotate"));
  document.getElementById("tvRevokeBtn").addEventListener("click", () => _displayAction("revoke"));
  document.getElementById("tvConnForm").addEventListener("submit", submitDisplayConn);
  renderDisplayConfig();
}
```
Call `initDisplayConfig();` from the same top-level init path where `initScheduledReports();` is called.

- [ ] **Step 5: CSS** in `app.css`:
```css
.tv-url-row { display: flex; gap: 8px; margin-bottom: 12px; }
.tv-url-row input { flex: 1; font-family: monospace; }
```

- [ ] **Step 6:** Run `python -m pytest tests/test_tv_admin_assets.py -v` (2 passed), then FULL suite.

- [ ] **Step 7:** Commit:
```bash
git add nwdash/assets/dashboard.html nwdash/assets/app.js nwdash/assets/app.css tests/test_tv_admin_assets.py
git commit -m "feat(tv): admin TV/Display panel — token URL + validated display connection"
```

---

## Task 8: version bump + docs + bundle build

**Files:** `nwdash/config.py`, `pyproject.toml`, `README.md`

- [ ] **Step 1:** Bump `APP_VERSION = "2.10.0"` in `nwdash/config.py` and `version = "2.10.0"` in `pyproject.toml` (additive minor).

- [ ] **Step 2:** README: add a short "TV / DSO wall" note — point the TV at `/tv/<token>` (generated in admin → TV / Display); it shows the live wall with no login; set a display connection so it stays live 24/7; protect the rest with a dashboard password (`Setup-NWDash.cmd -Upgrade -AuthPassword '...'`).

- [ ] **Step 3:** FULL suite: `python -m pytest -q` (0 failures).

- [ ] **Step 4:** Build the offline bundle: `pwsh -ExecutionPolicy Bypass -File deploy\build-bundle.ps1 -SkipRuntimeFetch` → `done -> dist\nwdash-bundle-2.10.0-win-x64.zip`.

- [ ] **Step 5:** Commit:
```bash
git add nwdash/config.py pyproject.toml README.md
git commit -m "chore(tv): bump to 2.10.0 for DSO TV display token + display feed"
```

---

## Deployment note (not a code task)

Ships as 2.10.0. Post-deploy: (1) set the dashboard password (`Setup-NWDash.cmd -Upgrade -AuthPassword '...'`); (2) in admin → **TV / Display**, set the display connection (validated) and copy the `/tv/<token>` URL; (3) point the DSO TV at that URL. The wall renders live with no login; admin + NetWorker credentials stay behind the password. Rotate the token to invalidate an old TV URL.
