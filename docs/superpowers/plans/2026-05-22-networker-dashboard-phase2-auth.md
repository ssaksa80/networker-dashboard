# NetWorker Dashboard — Phase 2 (core) Auth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Gate every data endpoint behind a shared gateway password with HMAC-signed cookies, keep share links as token-scoped capability URLs, default to loopback bind, redact 500s, rate-limit login, and fix the false at-rest message.

**Architecture:** All changes in `networker_dashboard.py`. New stdlib-only auth primitives (key file, pbkdf2 password hash, HMAC cookie, in-memory login rate-limit). HTTP handlers gain an auth gate plus login/logout/token-dashboard routes. Pure functions are unit-tested in `test_phase2.py` (stdlib `unittest`); HTTP flows are smoke-tested live.

**Tech Stack:** Python 3 stdlib only — `hashlib`, `hmac`, `os`, `http.cookies`, `base64`, `json`, `threading`. No new third-party deps.

---

### Task 1: Auth key + password hashing primitives

**Files:**
- Modify: `networker_dashboard.py` (imports, constants, new functions)
- Test: `test_phase2.py` (create)

- [ ] **Step 1: Write the failing test**

Create `test_phase2.py`:
```python
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

import networker_dashboard as nd


class _TmpDataDir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._orig = {
            "DATA_DIR": nd.DATA_DIR,
            "AUTH_KEY_FILE": nd.AUTH_KEY_FILE,
            "AUTH_CONFIG_FILE": nd.AUTH_CONFIG_FILE,
        }
        nd.DATA_DIR = Path(self._tmp)
        nd.AUTH_KEY_FILE = nd.DATA_DIR / ".auth_key"
        nd.AUTH_CONFIG_FILE = nd.DATA_DIR / "auth.json"

    def tearDown(self):
        nd.DATA_DIR = self._orig["DATA_DIR"]
        nd.AUTH_KEY_FILE = self._orig["AUTH_KEY_FILE"]
        nd.AUTH_CONFIG_FILE = self._orig["AUTH_CONFIG_FILE"]
        shutil.rmtree(self._tmp, ignore_errors=True)


class PasswordTests(_TmpDataDir):
    def test_set_and_verify_password(self):
        self.assertFalse(nd.auth_password_configured())
        nd.set_auth_password("hunter2")
        self.assertTrue(nd.auth_password_configured())
        self.assertTrue(nd.verify_auth_password("hunter2"))
        self.assertFalse(nd.verify_auth_password("wrong"))

    def test_verify_without_config_is_false(self):
        self.assertFalse(nd.verify_auth_password("anything"))

    def test_password_hash_not_plaintext_at_rest(self):
        nd.set_auth_password("plaintext-secret")
        raw = nd.AUTH_CONFIG_FILE.read_text(encoding="utf-8")
        self.assertNotIn("plaintext-secret", raw)
        data = json.loads(raw)
        self.assertIn("salt", data)
        self.assertIn("hash", data)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest test_phase2 -v`
Expected: FAIL with `AttributeError` on `nd.AUTH_KEY_FILE` / `set_auth_password`.

- [ ] **Step 3: Add imports**

Find the existing import block. Find:
```python
import html as html_lib
import json
```
Add after `import json`:
```python
import hashlib
import hmac
import os
```
Then find:
```python
from http.cookiejar import CookieJar
```
Add immediately below it:
```python
from http.cookies import SimpleCookie
```

- [ ] **Step 4: Add constants**

Find:
```python
PROFILES_FILE = DATA_DIR / "profiles.json"
```
Add immediately below it:
```python
AUTH_KEY_FILE = DATA_DIR / ".auth_key"
AUTH_CONFIG_FILE = DATA_DIR / "auth.json"
COOKIE_NAME = "nwdash_auth"
AUTH_TTL_SECONDS = 43200  # 12 hours
PBKDF2_ITERATIONS = 200_000
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 300
AUTH_ENABLED = False  # set in run() once a password is configured
```

- [ ] **Step 5: Add auth key loader + password functions**

Find:
```python
WMI_CREDENTIAL_KEY = _load_or_create_stable_key()
WMI_CIPHER = Fernet(WMI_CREDENTIAL_KEY) if (Fernet and WMI_CREDENTIAL_KEY) else None
```
Add immediately below it:
```python


def _load_or_create_auth_key() -> bytes:
    """Stable 32-byte HMAC key for signing auth cookies. Stdlib only."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if AUTH_KEY_FILE.exists():
            data = AUTH_KEY_FILE.read_bytes()
            if len(data) >= 32:
                return data
    except OSError:
        pass
    key = os.urandom(32)
    try:
        AUTH_KEY_FILE.write_bytes(key)
        AUTH_KEY_FILE.chmod(0o600)
    except OSError:
        pass
    return key


AUTH_SECRET_KEY = _load_or_create_auth_key()


def _hash_password(password: str, salt: bytes, iterations: int = PBKDF2_ITERATIONS) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


def set_auth_password(password: str) -> None:
    salt = os.urandom(16)
    record = {
        "salt": salt.hex(),
        "hash": _hash_password(password, salt).hex(),
        "iterations": PBKDF2_ITERATIONS,
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = AUTH_CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(record), encoding="utf-8")
    tmp.replace(AUTH_CONFIG_FILE)
    try:
        AUTH_CONFIG_FILE.chmod(0o600)
    except OSError:
        pass


def _load_auth_config() -> dict[str, Any] | None:
    try:
        if AUTH_CONFIG_FILE.exists():
            data = json.loads(AUTH_CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("hash") and data.get("salt"):
                return data
    except (OSError, json.JSONDecodeError):
        pass
    return None


def auth_password_configured() -> bool:
    return _load_auth_config() is not None


def verify_auth_password(password: str) -> bool:
    config = _load_auth_config()
    if not config:
        return False
    try:
        salt = bytes.fromhex(config["salt"])
        expected = bytes.fromhex(config["hash"])
        iterations = int(config.get("iterations") or PBKDF2_ITERATIONS)
    except (ValueError, TypeError):
        return False
    candidate = _hash_password(password, salt, iterations)
    return hmac.compare_digest(candidate, expected)
```

- [ ] **Step 6: Run test to verify it passes**

Run: `python -m unittest test_phase2 -v` → PASS.
Parse check: `python -c "import ast; ast.parse(open('networker_dashboard.py',encoding='utf-8').read()); print('parse ok')"`

- [ ] **Step 7: Commit**
```bash
git add networker_dashboard.py test_phase2.py
git commit -m "feat: auth key + pbkdf2 password hashing primitives (C4)"
```

---

### Task 2: Cookie sign/verify + login rate-limit + loopback helper

**Files:**
- Modify: `networker_dashboard.py`
- Test: `test_phase2.py`

- [ ] **Step 1: Write the failing test**

Append to `test_phase2.py`:
```python
class CookieTests(unittest.TestCase):
    def test_cookie_roundtrip(self):
        self.assertTrue(nd._verify_auth_cookie(nd._make_auth_cookie()))

    def test_tampered_cookie_rejected(self):
        cookie = nd._make_auth_cookie()
        payload, _, sig = cookie.rpartition(".")
        self.assertFalse(nd._verify_auth_cookie(payload + ".AAAA" + sig))

    def test_expired_cookie_rejected(self):
        import base64 as b64
        now = int(time.time())
        payload = b64.urlsafe_b64encode(
            json.dumps({"iat": now - 100000, "exp": now - 1}).encode()
        ).decode().rstrip("=")
        import hmac as _hmac
        import hashlib as _hashlib
        sig = b64.urlsafe_b64encode(
            _hmac.new(nd.AUTH_SECRET_KEY, payload.encode(), _hashlib.sha256).digest()
        ).decode().rstrip("=")
        self.assertFalse(nd._verify_auth_cookie(f"{payload}.{sig}"))

    def test_garbage_cookie_rejected(self):
        self.assertFalse(nd._verify_auth_cookie("not-a-cookie"))
        self.assertFalse(nd._verify_auth_cookie(""))


class RateLimitTests(unittest.TestCase):
    def setUp(self):
        nd.LOGIN_ATTEMPTS.clear()

    def tearDown(self):
        nd.LOGIN_ATTEMPTS.clear()

    def test_rate_limit_after_max_attempts(self):
        ip = "10.0.0.1"
        for _ in range(nd.LOGIN_MAX_ATTEMPTS):
            self.assertFalse(nd._login_rate_limited(ip))
            nd._record_login_failure(ip)
        self.assertTrue(nd._login_rate_limited(ip))
        nd._clear_login_failures(ip)
        self.assertFalse(nd._login_rate_limited(ip))


class LoopbackTests(unittest.TestCase):
    def test_loopback_detection(self):
        for host in ("127.0.0.1", "localhost", "::1"):
            self.assertTrue(nd._is_loopback_bind(host))
        for host in ("0.0.0.0", "", "::", "192.168.1.5"):
            self.assertFalse(nd._is_loopback_bind(host))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest test_phase2.CookieTests -v`
Expected: FAIL with `AttributeError` on `_make_auth_cookie`.

- [ ] **Step 3: Add cookie, rate-limit, loopback functions**

Append after the `verify_auth_password` function (added in Task 1):
```python


def _make_auth_cookie() -> str:
    now = int(time.time())
    payload = base64.urlsafe_b64encode(
        json.dumps({"iat": now, "exp": now + AUTH_TTL_SECONDS}).encode("utf-8")
    ).decode("ascii").rstrip("=")
    sig = base64.urlsafe_b64encode(
        hmac.new(AUTH_SECRET_KEY, payload.encode("ascii"), hashlib.sha256).digest()
    ).decode("ascii").rstrip("=")
    return f"{payload}.{sig}"


def _verify_auth_cookie(value: str) -> bool:
    if not value or "." not in value:
        return False
    payload, _, sig = value.rpartition(".")
    if not payload or not sig:
        return False
    expected_sig = base64.urlsafe_b64encode(
        hmac.new(AUTH_SECRET_KEY, payload.encode("ascii"), hashlib.sha256).digest()
    ).decode("ascii").rstrip("=")
    if not hmac.compare_digest(sig, expected_sig):
        return False
    try:
        padded = payload + "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        return int(data.get("exp", 0)) > int(time.time())
    except (ValueError, json.JSONDecodeError):
        return False


LOGIN_ATTEMPTS: dict[str, list[float]] = {}
LOGIN_ATTEMPTS_LOCK = threading.Lock()


def _login_rate_limited(ip: str) -> bool:
    now = time.time()
    with LOGIN_ATTEMPTS_LOCK:
        attempts = [t for t in LOGIN_ATTEMPTS.get(ip, []) if now - t < LOGIN_WINDOW_SECONDS]
        LOGIN_ATTEMPTS[ip] = attempts
        return len(attempts) >= LOGIN_MAX_ATTEMPTS


def _record_login_failure(ip: str) -> None:
    now = time.time()
    with LOGIN_ATTEMPTS_LOCK:
        attempts = [t for t in LOGIN_ATTEMPTS.get(ip, []) if now - t < LOGIN_WINDOW_SECONDS]
        attempts.append(now)
        LOGIN_ATTEMPTS[ip] = attempts


def _clear_login_failures(ip: str) -> None:
    with LOGIN_ATTEMPTS_LOCK:
        LOGIN_ATTEMPTS.pop(ip, None)


def _is_loopback_bind(host: str) -> bool:
    return (host or "").strip().lower() in ("127.0.0.1", "localhost", "::1")
```

- [ ] **Step 4: Run tests**
Run: `python -m unittest test_phase2 -v` → all PASS.
Parse check: `python -c "import ast; ast.parse(open('networker_dashboard.py',encoding='utf-8').read()); print('parse ok')"`

- [ ] **Step 5: Commit**
```bash
git add networker_dashboard.py test_phase2.py
git commit -m "feat: HMAC auth cookies, login rate-limit, loopback helper (C4)"
```

---

### Task 3: Login page + handler auth methods

**Files:**
- Modify: `networker_dashboard.py` (`DashboardHandler` methods, new `login_page_html`)
- Test: `test_phase2.py`

- [ ] **Step 1: Write the failing test**

Append to `test_phase2.py`:
```python
class LoginPageTests(unittest.TestCase):
    def test_login_page_renders_form(self):
        html = nd.login_page_html()
        self.assertIn("<form", html)
        self.assertIn("/api/login", html)
        self.assertIn("password", html)
```

- [ ] **Step 2: Run test to verify it fails**
Run: `python -m unittest test_phase2.LoginPageTests -v` → FAIL (`login_page_html` missing).

- [ ] **Step 3: Add `login_page_html`**

Find:
```python
def read_only_view_html(token: str) -> str:
```
Insert this function immediately ABOVE it:
```python
def login_page_html() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NetWorker Dashboard — Sign in</title>
<style>
  *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
  body{font-family:system-ui,sans-serif;background:#eef3f6;color:#172026;min-height:100vh;display:flex;align-items:center;justify-content:center}
  .card{background:#fff;border:1px solid #d7e1e7;border-radius:12px;padding:32px;width:340px;box-shadow:0 4px 16px rgba(0,0,0,.06)}
  h1{font-size:18px;margin-bottom:6px}
  p{font-size:13px;color:#5f6d76;margin-bottom:20px}
  label{display:block;font-size:13px;margin-bottom:6px}
  input{width:100%;padding:10px 12px;border:1px solid #d7e1e7;border-radius:8px;font-size:14px;margin-bottom:16px}
  button{width:100%;padding:10px;border:0;border-radius:8px;background:#126e82;color:#fff;font-size:14px;font-weight:600;cursor:pointer}
  button:disabled{opacity:.6;cursor:default}
  .err{background:#fde2e4;border:1px solid #f0b8bc;color:#bd2b3a;border-radius:8px;padding:10px 12px;font-size:13px;margin-bottom:16px;display:none}
</style>
</head>
<body>
<div class="card">
  <h1>NetWorker Dashboard</h1>
  <p>Enter the dashboard access password to continue.</p>
  <div class="err" id="err"></div>
  <form id="loginForm">
    <label for="pw">Password</label>
    <input type="password" id="pw" autocomplete="current-password" autofocus>
    <button type="submit" id="btn">Sign in</button>
  </form>
</div>
<script>
  const form=document.getElementById('loginForm');
  const err=document.getElementById('err');
  const btn=document.getElementById('btn');
  form.addEventListener('submit',async(e)=>{
    e.preventDefault();
    btn.disabled=true;err.style.display='none';
    try{
      const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:document.getElementById('pw').value})});
      if(r.ok){location.reload();return;}
      const d=await r.json().catch(()=>({}));
      err.textContent=d.error||'Sign in failed.';err.style.display='block';
    }catch(_){err.textContent='Network error.';err.style.display='block';}
    btn.disabled=false;
  });
</script>
</body>
</html>"""
```

- [ ] **Step 4: Add handler auth methods**

In class `DashboardHandler`, find:
```python
    def _require_https(self) -> bool:
        if self._is_https():
            return True
        self._send_error_json(HTTPStatus.FORBIDDEN, "HTTPS is required.")
        return False
```
Insert immediately below it:
```python
    def _authenticated(self) -> bool:
        if not AUTH_ENABLED:
            return True
        raw = self.headers.get("Cookie")
        if not raw:
            return False
        try:
            jar = SimpleCookie()
            jar.load(raw)
        except Exception:  # noqa: BLE001 — malformed cookie header
            return False
        morsel = jar.get(COOKIE_NAME)
        if not morsel:
            return False
        return _verify_auth_cookie(morsel.value)

    def _send_json_with_cookie(self, status: int, payload: dict[str, Any], cookie_value: str, max_age: int) -> None:
        body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        cookie = (
            f"{COOKIE_NAME}={cookie_value}; HttpOnly; Secure; SameSite=Strict; "
            f"Path=/; Max-Age={max_age}"
        )
        self._send_bytes(status, body, "application/json; charset=utf-8", {"Set-Cookie": cookie})

    def _handle_login(self) -> None:
        ip = self.client_address[0]
        if _login_rate_limited(ip):
            self._send_error_json(HTTPStatus.TOO_MANY_REQUESTS, "Too many login attempts. Wait and try again.")
            return
        try:
            payload = self._read_json_body()
        except (BadRequest, json.JSONDecodeError):
            self._send_error_json(HTTPStatus.BAD_REQUEST, "Invalid login request.")
            return
        password = str(payload.get("password") or "")
        if not AUTH_ENABLED:
            self._send_json(HTTPStatus.OK, {"ok": True, "authDisabled": True})
            return
        if verify_auth_password(password):
            _clear_login_failures(ip)
            self._send_json_with_cookie(HTTPStatus.OK, {"ok": True}, _make_auth_cookie(), AUTH_TTL_SECONDS)
        else:
            _record_login_failure(ip)
            self._send_error_json(HTTPStatus.UNAUTHORIZED, "Invalid password.")

    def _handle_token_dashboard(self, path: str) -> None:
        token = path[len("/api/view/"):].strip("/")
        if not token or not re.fullmatch(r"[0-9a-f]{32}", token):
            self._send_error_json(HTTPStatus.NOT_FOUND, "Not found.")
            return
        session_id = validate_share_token(token)
        if not session_id:
            self._send_error_json(HTTPStatus.GONE, "This share link has expired or been revoked.")
            return
        dashboard = cached_reliable_dashboard_for_session(session_id)
        if not isinstance(dashboard, dict):
            with SHARED_DASHBOARD_LOCK:
                if SHARED_DASHBOARD_STATE.get("sessionId") == session_id:
                    candidate = SHARED_DASHBOARD_STATE.get("dashboard")
                    dashboard = candidate if isinstance(candidate, dict) else None
        if not isinstance(dashboard, dict):
            self._send_json(HTTPStatus.OK, {"ok": False, "message": "No dashboard data available for this share link yet."})
            return
        self._send_json(
            HTTPStatus.OK,
            {"ok": True, "dashboard": json_clone(dashboard), "updatedAt": dashboard.get("generatedAt", "")},
        )
```

- [ ] **Step 5: Run tests + parse**
Run: `python -m unittest test_phase2 -v` → all PASS.
Parse: `python -c "import ast; ast.parse(open('networker_dashboard.py',encoding='utf-8').read()); print('parse ok')"`

- [ ] **Step 6: Commit**
```bash
git add networker_dashboard.py test_phase2.py
git commit -m "feat: login page + handler auth/login/token-dashboard methods (C4)"
```

---

### Task 4: Rewrite do_GET with auth gating

**Files:**
- Modify: `networker_dashboard.py` (`DashboardHandler.do_GET`)

- [ ] **Step 1: Replace the entire `do_GET` method**

Find the current `do_GET` method (from `def do_GET(self) -> None:` through its final `self._send_error_json(HTTPStatus.NOT_FOUND, "Not found.")` line, immediately before `def do_POST`). Replace the ENTIRE method with:
```python
    def do_GET(self) -> None:
        if not self._require_https():
            return
        try:
            path = urlparse(self.path).path

            # --- Always-open routes (no auth) ---
            if path == "/favicon.ico":
                self._send_bytes(HTTPStatus.OK, FAVICON_SVG, "image/svg+xml")
                return
            if path == "/networker-logo.png":
                if NETWORKER_LOGO_PATH.exists():
                    self._send_bytes(HTTPStatus.OK, NETWORKER_LOGO_PATH.read_bytes(), "image/png")
                else:
                    self._send_bytes(HTTPStatus.OK, FAVICON_SVG, "image/svg+xml")
                return
            if path == "/api/health":
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "app": APP_NAME,
                        "version": APP_VERSION,
                        "https": True,
                        "debug": APP_DEBUG,
                        "time": datetime.now().astimezone().isoformat(),
                    },
                )
                return

            # --- Token-gated share routes (capability URL, no cookie) ---
            if path.startswith("/view/"):
                token = path[6:].strip("/")
                if not token or not re.fullmatch(r"[0-9a-f]{32}", token):
                    self._send_error_json(HTTPStatus.NOT_FOUND, "Not found.")
                    return
                session_id = validate_share_token(token)
                if not session_id:
                    self._send_bytes(
                        HTTPStatus.GONE,
                        b"<html><body><p>This share link has expired or been revoked.</p></body></html>",
                        "text/html; charset=utf-8",
                    )
                    return
                self._send_bytes(
                    HTTPStatus.OK,
                    read_only_view_html(token).encode("utf-8"),
                    "text/html; charset=utf-8",
                )
                return
            if path.startswith("/api/view/"):
                self._handle_token_dashboard(path)
                return

            # --- Root: login page when auth required and not authenticated ---
            if path in ("/", "/index.html"):
                if AUTH_ENABLED and not self._authenticated():
                    self._send_bytes(HTTPStatus.OK, login_page_html().encode("utf-8"), "text/html; charset=utf-8")
                else:
                    self._send_bytes(HTTPStatus.OK, dashboard_html().encode("utf-8"), "text/html; charset=utf-8")
                return

            # --- Everything below requires authentication ---
            if AUTH_ENABLED and not self._authenticated():
                self._send_error_json(HTTPStatus.UNAUTHORIZED, "Authentication required.")
                return

            if path == "/api/current-dashboard":
                self._send_json(HTTPStatus.OK, shared_dashboard_payload())
                return
            if path == "/api/profiles":
                with PROFILES_LOCK:
                    self._send_json(HTTPStatus.OK, {"ok": True, "profiles": _mask_profiles(load_profiles())})
                return
            if path == "/api/stream":
                wfile = self.wfile
                if not _sse_register(wfile):
                    self._send_error_json(HTTPStatus.SERVICE_UNAVAILABLE, "Too many live viewers connected. Try again shortly.")
                    return
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
                self.end_headers()
                payload = shared_dashboard_payload()
                dash = payload.get("dashboard")
                if isinstance(dash, dict):
                    try:
                        wfile.write(f"event: dashboard\ndata: {json.dumps(dash, separators=(',', ':'))}\n\n".encode("utf-8"))
                        wfile.flush()
                    except OSError:
                        pass
                while not SHARED_REFRESH_STOP.is_set():
                    try:
                        wfile.write(b": heartbeat\n\n")
                        wfile.flush()
                        SHARED_REFRESH_STOP.wait(25)
                    except OSError:
                        break
                with SSE_CLIENTS_LOCK:
                    try:
                        SSE_CLIENTS.remove(wfile)
                    except ValueError:
                        pass
                return
            if path == "/api/snapshots":
                query = dict(parse_qsl(urlparse(self.path).query, keep_blank_values=True))
                action = query.get("action", "compare")
                if action == "list":
                    with SNAPSHOTS_LOCK:
                        self._send_json(HTTPStatus.OK, {"ok": True, "snapshots": list_snapshot_summary()})
                elif action == "history":
                    with SNAPSHOTS_LOCK:
                        self._send_json(HTTPStatus.OK, snapshot_history_all())
                elif action == "export":
                    with SNAPSHOTS_LOCK:
                        csv_data = snapshots_to_csv()
                    self._send_bytes(HTTPStatus.OK, csv_data.encode("utf-8"), "text/csv; charset=utf-8")
                elif action == "auto-config":
                    self._send_json(HTTPStatus.OK, {"ok": True, "enabled": load_auto_snapshot_config()})
                else:
                    with SNAPSHOTS_LOCK:
                        self._send_json(HTTPStatus.OK, compare_dashboard_snapshots(query.get("range", "7d")))
                return

            self._send_error_json(HTTPStatus.NOT_FOUND, "Not found.")
        except Exception as exc:  # noqa: BLE001
            ref = uuid.uuid4().hex[:8]
            debug_log(f"do_GET unhandled error ref={ref}: {safe_log_text(exc)}")
            try:
                self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"Internal error (ref {ref}).")
            except Exception:  # noqa: BLE001 — headers may already be sent
                pass
```

- [ ] **Step 2: Parse + run existing tests (no regressions)**
Run: `python -c "import ast; ast.parse(open('networker_dashboard.py',encoding='utf-8').read()); print('parse ok')"`
Run: `python -m unittest test_phase1 test_phase2 -v` → all PASS (handlers aren't unit-tested; this confirms nothing broke at import/parse).

- [ ] **Step 3: Commit**
```bash
git add networker_dashboard.py
git commit -m "feat: auth gating + token-scoped data route in do_GET (C4/H3)"
```

---

### Task 5: do_POST login/logout, gating, 500 redaction

**Files:**
- Modify: `networker_dashboard.py` (`DashboardHandler.do_POST`)

- [ ] **Step 1: Insert login/logout + auth gate**

In `do_POST`, find:
```python
    def do_POST(self) -> None:
        if not self._require_https():
            return
        path = urlparse(self.path).path
        allowed = {"/api/dashboard", "/api/export", "/api/server-health",
                   "/api/alert-automation", "/api/snapshots",
                   "/api/share", "/api/multi-server", "/api/profiles"}
```
Replace with:
```python
    def do_POST(self) -> None:
        if not self._require_https():
            return
        path = urlparse(self.path).path
        # Always-open auth endpoints
        if path == "/api/login":
            self._handle_login()
            return
        if path == "/api/logout":
            self._send_json_with_cookie(HTTPStatus.OK, {"ok": True}, "", 0)
            return
        # Auth gate for all other POST routes
        if AUTH_ENABLED and not self._authenticated():
            self._send_error_json(HTTPStatus.UNAUTHORIZED, "Authentication required.")
            return
        allowed = {"/api/dashboard", "/api/export", "/api/server-health",
                   "/api/alert-automation", "/api/snapshots",
                   "/api/share", "/api/multi-server", "/api/profiles"}
```

- [ ] **Step 2: Redact the 500 catch-all**

In `do_POST`, find:
```python
        except BadRequest as exc:
            self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        except json.JSONDecodeError:
            self._send_error_json(HTTPStatus.BAD_REQUEST, "Request body must be valid JSON.")
        except Exception as exc:
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))
```
Replace with:
```python
        except BadRequest as exc:
            self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        except json.JSONDecodeError:
            self._send_error_json(HTTPStatus.BAD_REQUEST, "Request body must be valid JSON.")
        except Exception as exc:
            ref = uuid.uuid4().hex[:8]
            debug_log(f"do_POST unhandled error ref={ref}: {safe_log_text(exc)}")
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"Internal error (ref {ref}).")
```

- [ ] **Step 3: Parse + tests**
Run: `python -c "import ast; ast.parse(open('networker_dashboard.py',encoding='utf-8').read()); print('parse ok')"`
Run: `python -m unittest test_phase1 test_phase2 -v` → all PASS.

- [ ] **Step 4: Commit**
```bash
git add networker_dashboard.py
git commit -m "feat: auth gate on POST + login/logout + redact 500 errors (C4/H3)"
```

---

### Task 6: run() wiring, bind default, warning, message fix, share-view fetch swap, SPA 401 handler

**Files:**
- Modify: `networker_dashboard.py` (`parse_args`, `run`, `read_only_view_html`, dashboard SPA `<script>`)

- [ ] **Step 1: Add the `--auth-password` CLI flag and change bind default**

In `parse_args`, find:
```python
    parser.add_argument(
        "--bind",
        default="0.0.0.0",
        help="Interface to bind. Defaults to 0.0.0.0 so localhost and the local server IP are both available.",
    )
```
Replace with:
```python
    parser.add_argument(
        "--bind",
        default="127.0.0.1",
        help="Interface to bind. Defaults to 127.0.0.1 (local only). Use 0.0.0.0 to expose on the network (set an auth password first).",
    )
```
Then find:
```python
    return parser.parse_args(argv)
```
Insert immediately above it:
```python
    parser.add_argument(
        "--auth-password",
        default="",
        help="Dashboard access password. May also be supplied via the DASHBOARD_AUTH_PASSWORD environment variable. Stored only as a salted hash.",
    )
```

- [ ] **Step 2: Wire auth into run()**

In `run`, find:
```python
    global APP_DEBUG, REQUEST_TIMEOUT_SECONDS, MAX_CONNECTIONS, MAX_SSE_CLIENTS
    args = parse_args(argv or sys.argv[1:])
    APP_DEBUG = bool(args.debug)
    REQUEST_TIMEOUT_SECONDS = max(5, int(args.request_timeout))
    MAX_CONNECTIONS = max(1, int(args.max_connections))
    MAX_SSE_CLIENTS = max(1, int(args.max_sse))
    DashboardHandler.timeout = REQUEST_TIMEOUT_SECONDS
```
Replace with:
```python
    global APP_DEBUG, REQUEST_TIMEOUT_SECONDS, MAX_CONNECTIONS, MAX_SSE_CLIENTS, AUTH_ENABLED
    args = parse_args(argv or sys.argv[1:])
    APP_DEBUG = bool(args.debug)
    REQUEST_TIMEOUT_SECONDS = max(5, int(args.request_timeout))
    MAX_CONNECTIONS = max(1, int(args.max_connections))
    MAX_SSE_CLIENTS = max(1, int(args.max_sse))
    DashboardHandler.timeout = REQUEST_TIMEOUT_SECONDS
    auth_password = args.auth_password or os.environ.get("DASHBOARD_AUTH_PASSWORD") or ""
    if auth_password:
        set_auth_password(auth_password)
    AUTH_ENABLED = auth_password_configured()
```

- [ ] **Step 3: Fix the false at-rest message + add the exposed warning**

In `run`, find:
```python
    print("Passwords are encrypted in process memory for seamless reconnect and are not written to disk.")
```
Replace with:
```python
    print("Credentials are encrypted at rest in the data directory and are never stored in plaintext or placed in URLs.")
    if AUTH_ENABLED:
        print("Dashboard authentication is ENABLED (gateway password required).")
    elif not _is_loopback_bind(args.bind):
        print("=" * 72)
        print("WARNING: Bound to a non-loopback address with NO authentication.")
        print("Anyone who can reach this port can view all backup data.")
        print("Set DASHBOARD_AUTH_PASSWORD (or --auth-password), or bind to 127.0.0.1.")
        print("=" * 72)
    else:
        print("Dashboard authentication is disabled (local loopback bind).")
```

- [ ] **Step 4: Point the read-only view at the token-scoped endpoint**

In `read_only_view_html`, find:
```python
      const r = await fetch('/api/current-dashboard', {{cache: 'no-store'}});
```
Replace with:
```python
      const r = await fetch('/api/view/{token}', {{cache: 'no-store'}});
```
(The function is an f-string with doubled braces; `{token}` is the single-brace interpolation of the token argument — match the existing brace style in that function exactly.)

- [ ] **Step 5: Add the SPA 401 -> reload handler**

In the dashboard SPA template, find the unique anchor:
```python
  <script>
    const form = document.getElementById("connectionForm");
```
Replace with:
```python
  <script>
    (function(){
      const _fetch = window.fetch;
      window.fetch = async function(...args){
        const resp = await _fetch.apply(this, args);
        try {
          const url = (args[0] && args[0].url) ? args[0].url : String(args[0] || "");
          if (resp.status === 401 && url.indexOf("/api/") !== -1) { location.reload(); }
        } catch (_e) {}
        return resp;
      };
    })();
    const form = document.getElementById("connectionForm");
```

- [ ] **Step 6: Parse + tests + help check**
Run: `python -c "import ast; ast.parse(open('networker_dashboard.py',encoding='utf-8').read()); print('parse ok')"`
Run: `python -m unittest test_phase1 test_phase2 -v` → all PASS.
Run: `python networker_dashboard.py --help` → shows `--auth-password`; `--bind` help shows 127.0.0.1 default.

- [ ] **Step 7: Commit**
```bash
git add networker_dashboard.py
git commit -m "feat: wire auth in run(), loopback bind default, exposed warning, fix at-rest message (C4/H2)"
```

---

### Task 7: Full regression + live auth smoke

**Files:** none (verification only)

- [ ] **Step 1: Full unit suite**
Run: `python -m unittest test_phase1 test_phase2 -v` → all PASS.

- [ ] **Step 2: Live smoke with auth enabled**

Boot (background): `python networker_dashboard.py --no-launch --port 18443 --bind 127.0.0.1 --auth-password testpass`

Then verify with this script (substitute port if needed):
```python
import ssl, json, urllib.request, http.cookiejar
ctx = ssl._create_unverified_context()
base = "https://localhost:18443"

def call(path, data=None, cookies=None, method=None):
    cj = cookies if cookies is not None else http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx), urllib.request.HTTPCookieProcessor(cj))
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(base+path, data=body, method=method or ("POST" if data is not None else "GET"))
    if data is not None: req.add_header("Content-Type","application/json")
    try:
        r = op.open(req, timeout=5); return r.status, r.read(2000), cj
    except urllib.error.HTTPError as e:
        return e.code, e.read(2000), cj

# 1. unauth data endpoint -> 401
s,_,_ = call("/api/current-dashboard"); print("unauth current-dashboard:", s); assert s == 401
# 2. health open
s,_,_ = call("/api/health"); print("health:", s); assert s == 200
# 3. wrong password -> 401
s,_,_ = call("/api/login", {"password":"nope"}); print("bad login:", s); assert s == 401
# 4. correct password -> 200 + cookie, then data endpoint with cookie -> 200
s,b,cj = call("/api/login", {"password":"testpass"}); print("good login:", s); assert s == 200
s,_,_ = call("/api/current-dashboard", cookies=cj); print("auth current-dashboard:", s); assert s == 200
print("ALL AUTH CHECKS PASSED")
```
Expected: prints `ALL AUTH CHECKS PASSED`. Stop the server afterward (PowerShell: `Get-NetTCPConnection -LocalPort 18443 -State Listen | %{ Stop-Process -Id $_.OwningProcess -Force }`).

- [ ] **Step 3: Loopback-no-password smoke (open mode parity)**
Boot: `python networker_dashboard.py --no-launch --port 18444 --bind 127.0.0.1` (no password).
Verify `GET /api/health` -> 200 and `GET /api/current-dashboard` -> 200 (open mode). Confirm startup printed "authentication is disabled (local loopback bind)". Stop the server.

---

## Self-Review

**Spec coverage:**
- Auth secret key (stdlib) → Task 1. ✔
- Password hash at rest (pbkdf2, salt, no plaintext) → Task 1. ✔
- Cookie sign/verify → Task 2. ✔
- Login rate-limit → Task 2 + Task 3 (`_handle_login`). ✔
- Loopback helper → Task 2. ✔
- `_authenticated`, login/logout, token dashboard, login page → Task 3. ✔
- Endpoint gating (GET) + token-scoped `/api/view/<token>` → Task 4. ✔
- Endpoint gating (POST) + login/logout routes → Task 5. ✔
- 500 redaction → Task 4 (GET) + Task 5 (POST). ✔
- bind default 127.0.0.1, `--auth-password`, env, AUTH_ENABLED, exposed warning, message fix → Task 6. ✔
- Share view fetch swap to token endpoint → Task 6. ✔
- SPA 401 → reload → Task 6. ✔
- Tests + live smoke → Tasks 1–3, 7. ✔

**Placeholder scan:** none — all code blocks complete; all commands show expected output.

**Type/name consistency:** `AUTH_KEY_FILE`, `AUTH_CONFIG_FILE`, `AUTH_SECRET_KEY`, `COOKIE_NAME`, `AUTH_TTL_SECONDS`, `PBKDF2_ITERATIONS`, `LOGIN_MAX_ATTEMPTS`, `LOGIN_WINDOW_SECONDS`, `AUTH_ENABLED`, `LOGIN_ATTEMPTS`, `LOGIN_ATTEMPTS_LOCK` and functions `_load_or_create_auth_key`, `_hash_password`, `set_auth_password`, `_load_auth_config`, `auth_password_configured`, `verify_auth_password`, `_make_auth_cookie`, `_verify_auth_cookie`, `_login_rate_limited`, `_record_login_failure`, `_clear_login_failures`, `_is_loopback_bind`, and handler methods `_authenticated`, `_send_json_with_cookie`, `_handle_login`, `_handle_token_dashboard`, `login_page_html` are used consistently across tasks.

**Known limitations (documented, not gaps):**
- `/api/health` stays open (LB probes) and exposes app/version/time only.
- Open mode (no password + loopback bind) preserves today's behavior for local dev.
- DPAPI key protection and SSRF allowlist are Phase 2b.
- SPA `fetch` wrapper covers `fetch`-based calls; if any call uses `XMLHttpRequest`, it is not auto-redirected (acceptable — the app uses `fetch`; expired sessions still surface a 401 error and a manual refresh returns to login).
