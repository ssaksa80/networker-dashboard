# NetWorker Dashboard — Phase 3: Logging + /api/status Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace ad-hoc `sys.stderr.write` logging with stdlib `logging` (JSON lines, size-rotating file + console), add per-request correlation IDs unified with the 500 error ref, and a cookie-gated `/api/status` operational endpoint.

**Architecture:** All in `networker_dashboard.py`. A module logger `LOG` + `_JsonLogFormatter` + `configure_logging` are defined near the top (before the DPAPI helpers, which log during import). Existing log sites migrate to `LOG`. `do_GET`/`do_POST` set a `request_id` threaded into logs and reused as the 500 ref. `/api/status` reads live counters. Pure pieces unit-tested in `test_phase3.py`; `/api/status` verified by live smoke.

**Tech Stack:** Python 3 stdlib only — `logging`, `logging.handlers`. No new third-party deps.

---

### Task 1: Logging core (formatter + configure_logging)

**Files:**
- Modify: `networker_dashboard.py` (imports + new constants/class/function)
- Test: `test_phase3.py` (create)

- [ ] **Step 1: Write the failing test**

Create `test_phase3.py`:
```python
import json
import logging
import shutil
import tempfile
import unittest
from pathlib import Path

import networker_dashboard as nd


class JsonFormatterTests(unittest.TestCase):
    def test_format_basic(self):
        fmt = nd._JsonLogFormatter()
        rec = logging.LogRecord("networker_dashboard", logging.INFO, __file__, 1, "hello %s", ("world",), None)
        obj = json.loads(fmt.format(rec))
        self.assertEqual(obj["level"], "INFO")
        self.assertEqual(obj["logger"], "networker_dashboard")
        self.assertEqual(obj["msg"], "hello world")
        self.assertIn("ts", obj)

    def test_format_request_id(self):
        fmt = nd._JsonLogFormatter()
        rec = logging.LogRecord("networker_dashboard", logging.INFO, __file__, 1, "m", (), None)
        rec.request_id = "abc123"
        obj = json.loads(fmt.format(rec))
        self.assertEqual(obj["request_id"], "abc123")


class ConfigureLoggingTests(unittest.TestCase):
    def setUp(self):
        self._handlers = list(nd.LOG.handlers)
        self._level = nd.LOG.level
        self._propagate = nd.LOG.propagate
        self._dir = nd.LOG_DIR
        self._file = nd.LOG_FILE
        self._tmp = Path(tempfile.mkdtemp())
        nd.LOG_DIR = self._tmp
        nd.LOG_FILE = self._tmp / "test.log"

    def tearDown(self):
        for h in list(nd.LOG.handlers):
            try:
                h.close()
            except Exception:
                pass
        nd.LOG.handlers[:] = self._handlers
        nd.LOG.setLevel(self._level)
        nd.LOG.propagate = self._propagate
        nd.LOG_DIR = self._dir
        nd.LOG_FILE = self._file
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_idempotent_handlers(self):
        nd.configure_logging(True)
        n1 = len(nd.LOG.handlers)
        nd.configure_logging(True)
        n2 = len(nd.LOG.handlers)
        self.assertEqual(n1, n2)
        self.assertGreaterEqual(n1, 1)

    def test_level_reflects_debug(self):
        nd.configure_logging(True)
        self.assertEqual(nd.LOG.level, logging.DEBUG)
        nd.configure_logging(False)
        self.assertEqual(nd.LOG.level, logging.INFO)

    def test_writes_to_file(self):
        nd.configure_logging(False)
        nd.LOG.info("hello-file-test")
        for h in nd.LOG.handlers:
            try:
                h.flush()
            except Exception:
                pass
        self.assertTrue(nd.LOG_FILE.exists())
        self.assertIn("hello-file-test", nd.LOG_FILE.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m unittest test_phase3 -v`
Expected: FAIL with `AttributeError` on `nd._JsonLogFormatter` / `nd.LOG`.

- [ ] **Step 3: Add imports**

Find:
```python
import json
import hashlib
```
Replace with:
```python
import json
import logging
import logging.handlers
import hashlib
```

- [ ] **Step 4: Add the logging core**

Find:
```python
AUTH_ENABLED = False  # set in run() once a password is configured
```
Insert IMMEDIATELY BELOW it:
```python

# ── Logging ────────────────────────────────────────────────────────────────
LOG_DIR = APP_BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "networker_dashboard.log"
PROCESS_START_TIME = time.time()
LOG = logging.getLogger("networker_dashboard")
_LOG_EXTRA_KEYS = ("request_id", "client", "status", "path", "event")


class _JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        obj = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in _LOG_EXTRA_KEYS:
            val = getattr(record, key, None)
            if val is not None:
                obj[key] = val
        if record.exc_info:
            obj["exc"] = self.formatException(record.exc_info)
        return json.dumps(obj, ensure_ascii=True, default=str)


def configure_logging(debug: bool) -> None:
    LOG.setLevel(logging.DEBUG if debug else logging.INFO)
    for handler in list(LOG.handlers):
        LOG.removeHandler(handler)
    formatter = _JsonLogFormatter()
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        LOG.addHandler(file_handler)
    except OSError:
        pass
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    LOG.addHandler(stream_handler)
    LOG.propagate = False
```

- [ ] **Step 5: Run + parse**

Run: `python -m unittest test_phase3 -v` → all PASS.
Run: `python -c "import ast; ast.parse(open('networker_dashboard.py',encoding='utf-8').read()); print('parse ok')"`
Run: `python -m unittest test_phase1 test_phase2 test_phase2b test_phase3 -v` → all PASS.

- [ ] **Step 6: Commit**
```bash
git add networker_dashboard.py test_phase3.py
git commit -m "feat: JSON structured logging core with rotating file handler (M5)"
```
(Use `git -c user.name="dev" -c user.email="dev@local" commit ...` if needed.)

---

### Task 2: Migrate existing log sites to LOG

**Files:**
- Modify: `networker_dashboard.py` (DPAPI helpers, `debug_log`, `log_message`, `log_dashboard_failure`, `DashboardHandler` class attr)

- [ ] **Step 1: Migrate DPAPI warnings**

Find:
```python
            sys.stderr.write(f"DPAPI protect failed; storing key unwrapped: {exc}\n")
```
Replace with:
```python
            LOG.warning(f"DPAPI protect failed; storing key unwrapped: {exc}")
```
Find:
```python
            sys.stderr.write(f"DPAPI unprotect failed for {path.name}: {exc}\n")
```
Replace with:
```python
            LOG.warning(f"DPAPI unprotect failed for {path.name}: {exc}")
```

- [ ] **Step 2: Migrate debug_log**

Find:
```python
def debug_log(message: str) -> None:
    if APP_DEBUG:
        now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        sys.stderr.write(f"[DEBUG {now}] {safe_log_text(message, 520)}\n")
```
Replace with:
```python
def debug_log(message: str) -> None:
    LOG.debug(safe_log_text(message, 520))
```

- [ ] **Step 3: Add request_id class default + migrate log_message**

Find:
```python
class DashboardHandler(BaseHTTPRequestHandler):
    server_version = f"NetWorkerDashboard/{APP_VERSION}"
    protocol_version = "HTTP/1.1"
    timeout = REQUEST_TIMEOUT_SECONDS

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write(
            "%s - - [%s] %s\n"
            % (self.client_address[0], self.log_date_time_string(), format % args)
        )
```
Replace with:
```python
class DashboardHandler(BaseHTTPRequestHandler):
    server_version = f"NetWorkerDashboard/{APP_VERSION}"
    protocol_version = "HTTP/1.1"
    timeout = REQUEST_TIMEOUT_SECONDS
    request_id = "-"

    def log_message(self, format: str, *args: Any) -> None:
        LOG.info(
            format % args,
            extra={"request_id": getattr(self, "request_id", "-"), "client": self.client_address[0]},
        )
```

- [ ] **Step 4: Migrate log_dashboard_failure**

Find:
```python
    def log_dashboard_failure(self, status: int, body: dict[str, Any]) -> None:
        target = body.get("target") if isinstance(body.get("target"), dict) else {}
        sources = body.get("sources") if isinstance(body.get("sources"), dict) else {}
        rest_base = safe_log_text(target.get("restApiBase", "unknown"))
        api_mode = safe_log_text(target.get("apiMode", "rest"))
        authc_header = "enabled" if target.get("authcHeaderEnabled") else "disabled"
        sys.stderr.write(
            f"NetWorker dashboard upstream failure: status={status} "
            f"apiMode={api_mode} apiBase={rest_base} authcHeader={authc_header}\n"
        )
        for name, item in sources.items():
            if isinstance(item, dict) and not item.get("ok"):
                path = safe_log_text(item.get("path", name))
                error = safe_log_text(item.get("error", "failed"))
                upstream_status = item.get("status", "n/a")
                sys.stderr.write(
                    f"  source={safe_log_text(name)} upstreamStatus={upstream_status} "
                    f"path={path} error={error}\n"
                )
```
Replace with:
```python
    def log_dashboard_failure(self, status: int, body: dict[str, Any]) -> None:
        target = body.get("target") if isinstance(body.get("target"), dict) else {}
        sources = body.get("sources") if isinstance(body.get("sources"), dict) else {}
        rest_base = safe_log_text(target.get("restApiBase", "unknown"))
        api_mode = safe_log_text(target.get("apiMode", "rest"))
        authc_header = "enabled" if target.get("authcHeaderEnabled") else "disabled"
        rid = getattr(self, "request_id", "-")
        LOG.warning(
            f"NetWorker dashboard upstream failure: apiMode={api_mode} apiBase={rest_base} authcHeader={authc_header}",
            extra={"request_id": rid, "status": status},
        )
        for name, item in sources.items():
            if isinstance(item, dict) and not item.get("ok"):
                path = safe_log_text(item.get("path", name))
                error = safe_log_text(item.get("error", "failed"))
                upstream_status = item.get("status", "n/a")
                LOG.warning(
                    f"  source={safe_log_text(name)} upstreamStatus={upstream_status} path={path} error={error}",
                    extra={"request_id": rid, "status": status},
                )
```

- [ ] **Step 5: Parse + tests**

Run: `python -c "import ast; ast.parse(open('networker_dashboard.py',encoding='utf-8').read()); print('parse ok')"`
Run: `python -m unittest test_phase1 test_phase2 test_phase2b test_phase3 -v` → all PASS.
Run: `python -c "import networker_dashboard"` (no error; confirms no remaining import-time stderr dependency broke).

- [ ] **Step 6: Commit**
```bash
git add networker_dashboard.py
git commit -m "refactor: route DPAPI/debug/access/failure logs through LOG (M5)"
```

---

### Task 3: Request IDs + 500 ref unification + run() wiring

**Files:**
- Modify: `networker_dashboard.py` (`do_GET`, `do_POST`, `run`)

- [ ] **Step 1: Set request_id at the top of do_GET**

Find:
```python
    def do_GET(self) -> None:
        if not self._require_https():
            return
        try:
            path = urlparse(self.path).path
```
Replace with:
```python
    def do_GET(self) -> None:
        if not self._require_https():
            return
        self.request_id = uuid.uuid4().hex[:8]
        try:
            path = urlparse(self.path).path
```

- [ ] **Step 2: Unify the do_GET 500 ref**

Find:
```python
        except Exception as exc:  # noqa: BLE001
            ref = uuid.uuid4().hex[:8]
            debug_log(f"do_GET unhandled error ref={ref}: {safe_log_text(exc)}")
            try:
                self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"Internal error (ref {ref}).")
            except Exception:  # noqa: BLE001 — headers may already be sent
                pass
```
Replace with:
```python
        except Exception as exc:  # noqa: BLE001
            ref = getattr(self, "request_id", "-")
            LOG.error(f"do_GET unhandled error: {safe_log_text(exc)}", extra={"request_id": ref}, exc_info=True)
            try:
                self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"Internal error (ref {ref}).")
            except Exception:  # noqa: BLE001 — headers may already be sent
                pass
```

- [ ] **Step 3: Set request_id at the top of do_POST**

Find:
```python
    def do_POST(self) -> None:
        if not self._require_https():
            return
        path = urlparse(self.path).path
        # Always-open auth endpoints
```
Replace with:
```python
    def do_POST(self) -> None:
        if not self._require_https():
            return
        self.request_id = uuid.uuid4().hex[:8]
        path = urlparse(self.path).path
        # Always-open auth endpoints
```

- [ ] **Step 4: Unify the do_POST 500 ref**

Find:
```python
        except Exception as exc:
            ref = uuid.uuid4().hex[:8]
            debug_log(f"do_POST unhandled error ref={ref}: {safe_log_text(exc)}")
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"Internal error (ref {ref}).")
```
Replace with:
```python
        except Exception as exc:
            ref = getattr(self, "request_id", "-")
            LOG.error(f"do_POST unhandled error: {safe_log_text(exc)}", extra={"request_id": ref}, exc_info=True)
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"Internal error (ref {ref}).")
```

- [ ] **Step 5: Call configure_logging in run() + startup log line**

Find:
```python
    global APP_DEBUG, REQUEST_TIMEOUT_SECONDS, MAX_CONNECTIONS, MAX_SSE_CLIENTS, AUTH_ENABLED
    args = parse_args(argv or sys.argv[1:])
    APP_DEBUG = bool(args.debug)
```
Replace with:
```python
    global APP_DEBUG, REQUEST_TIMEOUT_SECONDS, MAX_CONNECTIONS, MAX_SSE_CLIENTS, AUTH_ENABLED
    args = parse_args(argv or sys.argv[1:])
    APP_DEBUG = bool(args.debug)
    configure_logging(APP_DEBUG)
    LOG.info(f"{APP_NAME} {APP_VERSION} starting", extra={"event": "startup"})
```

Then find:
```python
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
Replace with:
```python
    if AUTH_ENABLED:
        print("Dashboard authentication is ENABLED (gateway password required).")
        LOG.info("authentication enabled", extra={"event": "auth"})
    elif not _is_loopback_bind(args.bind):
        print("=" * 72)
        print("WARNING: Bound to a non-loopback address with NO authentication.")
        print("Anyone who can reach this port can view all backup data.")
        print("Set DASHBOARD_AUTH_PASSWORD (or --auth-password), or bind to 127.0.0.1.")
        print("=" * 72)
        LOG.warning("exposed to non-loopback with no authentication", extra={"event": "auth"})
    else:
        print("Dashboard authentication is disabled (local loopback bind).")
        LOG.info("authentication disabled (loopback bind)", extra={"event": "auth"})
```

- [ ] **Step 6: Parse + tests**

Run: `python -c "import ast; ast.parse(open('networker_dashboard.py',encoding='utf-8').read()); print('parse ok')"`
Run: `python -m unittest test_phase1 test_phase2 test_phase2b test_phase3 -v` → all PASS.

- [ ] **Step 7: Commit**
```bash
git add networker_dashboard.py
git commit -m "feat: per-request IDs unified with 500 ref + logging in run() (M5)"
```

---

### Task 4: /api/status endpoint

**Files:**
- Modify: `networker_dashboard.py` (`do_GET`)

- [ ] **Step 1: Add the cookie-gated /api/status route**

In `do_GET`, find:
```python
            if path == "/api/current-dashboard":
                self._send_json(HTTPStatus.OK, shared_dashboard_payload())
                return
```
Replace with:
```python
            if path == "/api/status":
                with SHARED_DASHBOARD_LOCK:
                    updated = float(SHARED_DASHBOARD_STATE.get("updatedAt") or 0)
                    last_refresh = SHARED_DASHBOARD_STATE.get("lastRefresh") or ""
                    last_error = SHARED_DASHBOARD_STATE.get("lastError") or ""
                with SSE_CLIENTS_LOCK:
                    sse_count = len(SSE_CLIENTS)
                age = int(time.time() - updated) if updated else None
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "version": APP_VERSION,
                        "uptimeSeconds": int(time.time() - PROCESS_START_TIME),
                        "threads": threading.active_count(),
                        "sessions": len(_session_ids_snapshot()),
                        "automations": len(_automation_keys_snapshot()),
                        "sseClients": sse_count,
                        "sharedDashboard": {
                            "lastRefresh": last_refresh,
                            "lastRefreshAgeSeconds": age,
                            "lastError": last_error,
                        },
                        "authEnabled": AUTH_ENABLED,
                        "allowlistEnabled": ALLOWLIST_ENABLED,
                    },
                )
                return
            if path == "/api/current-dashboard":
                self._send_json(HTTPStatus.OK, shared_dashboard_payload())
                return
```

- [ ] **Step 2: Parse + tests**

Run: `python -c "import ast; ast.parse(open('networker_dashboard.py',encoding='utf-8').read()); print('parse ok')"`
Run: `python -m unittest test_phase1 test_phase2 test_phase2b test_phase3 -v` → all PASS.

- [ ] **Step 3: Commit**
```bash
git add networker_dashboard.py
git commit -m "feat: cookie-gated /api/status operational metrics endpoint (M5)"
```

---

### Task 5: .gitignore logs/ + regression + live smoke

**Files:**
- Modify: `.gitignore`

- [ ] **Step 1: Ignore the logs dir**

In `.gitignore`, find:
```
# Runtime data — contains secrets (master key, auth key, encrypted credentials, sessions)
data/
```
Replace with:
```
# Runtime data — contains secrets (master key, auth key, encrypted credentials, sessions)
data/

# Rotating application logs
logs/
```

- [ ] **Step 2: Full unit suite**
Run: `python -m unittest test_phase1 test_phase2 test_phase2b test_phase3 -v` → all PASS.

- [ ] **Step 3: Live smoke — logs + /api/status**

Boot (background): `python networker_dashboard.py --no-launch --port 18446 --bind 127.0.0.1 --auth-password testpass`

Then run (substitute port if needed):
```python
import ssl, json, urllib.request, urllib.error, http.cookiejar, time
ctx = ssl._create_unverified_context()
base = "https://localhost:18446"
def call(path, data=None, cookies=None):
    cj = cookies if cookies is not None else http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx), urllib.request.HTTPCookieProcessor(cj))
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(base+path, data=body, method="POST" if data is not None else "GET")
    if data is not None: req.add_header("Content-Type","application/json")
    try:
        r = op.open(req, timeout=5); return r.status, r.read(2000), cj
    except urllib.error.HTTPError as e:
        return e.code, e.read(2000), cj
for _ in range(40):
    try:
        if call("/api/health")[0] == 200: break
    except Exception: time.sleep(0.5)
s,_,_ = call("/api/status"); print("unauth_status", s, "(expect 401)"); assert s == 401
s,_,cj = call("/api/login", {"password":"testpass"}); print("login", s); assert s == 200
s,b,_ = call("/api/status", cookies=cj); print("auth_status", s); assert s == 200
data = json.loads(b); print("status_keys", sorted(data.keys()))
assert {"threads","sessions","sseClients","uptimeSeconds","sharedDashboard"} <= set(data.keys())
print("STATUS OK")
```
Expected: `STATUS OK`.

Then confirm the log file exists and is JSON:
```bash
python -c "import json,glob; f=sorted(glob.glob('logs/networker_dashboard.log*'))[0]; line=[l for l in open(f,encoding='utf-8') if l.strip()][-1]; obj=json.loads(line); print('log_json_keys', sorted(obj.keys())); assert 'ts' in obj and 'level' in obj and 'msg' in obj; print('LOG JSON OK')"
```
Expected: `LOG JSON OK`.

Stop the server (PowerShell: `Get-NetTCPConnection -LocalPort 18446 -State Listen | %{ Stop-Process -Id $_.OwningProcess -Force }`). Remove the test password: delete `data/auth.json`.

- [ ] **Step 4: Commit**
```bash
git add .gitignore
git commit -m "chore: gitignore logs/ dir"
```

---

## Self-Review

**Spec coverage:**
- Logging core (JSON formatter, rotating file + console, configure_logging, constants, LOG, PROCESS_START_TIME) → Task 1. ✔
- Migrate debug_log / log_message / log_dashboard_failure / DPAPI → Task 2. ✔
- Request IDs + 500 ref unification + run() configure_logging + startup LOG lines → Task 3. ✔
- /api/status cookie-gated with all listed fields → Task 4. ✔
- /api/health unchanged (open) → untouched. ✔
- .gitignore logs/ + tests + live smoke → Tasks 1, 5. ✔

**Placeholder scan:** none — all steps contain full code and exact commands.

**Type/name consistency:** `LOG`, `LOG_DIR`, `LOG_FILE`, `PROCESS_START_TIME`, `_JsonLogFormatter`, `_LOG_EXTRA_KEYS`, `configure_logging`, and handler attr `request_id` are used consistently. `_session_ids_snapshot`/`_automation_keys_snapshot` (Phase 1) and `SHARED_DASHBOARD_LOCK`/`SSE_CLIENTS_LOCK`/`AUTH_ENABLED`/`ALLOWLIST_ENABLED` exist from earlier phases.

**Ordering note:** the logging core is inserted right after `AUTH_ENABLED = False`, which is ABOVE the DPAPI helpers and the import-time key-loader calls — so `LOG` exists when `_write_protected_key`/`_read_protected_key` reference it during import. Until `configure_logging` runs in `run()`, log calls use Python's default `lastResort` handler (stderr).

**Known limitations (documented):**
- Import-time DPAPI warnings (rare) emit via the default handler, not JSON, because they precede `configure_logging`.
- Startup banner remains `print()` (operator console) with mirrored structured `LOG` lines (intentional mild redundancy).
- `/api/status` thread count is process-wide `threading.active_count()` (includes worker + background threads), intended as a coarse health signal.
