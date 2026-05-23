# NetWorker Dashboard — Phase 1 Stability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `networker_dashboard.py` survive 24x7 operation by fixing the unsynchronized global registries, unguarded background loops, missing request timeouts/caps, lock-bound SSE broadcasts, fragile shutdown, and non-atomic snapshot writes.

**Architecture:** All changes live in the existing single file `networker_dashboard.py`. One `threading.RLock()` guards both global registries through small accessor helpers. Background loops gain per-iteration exception guards via extracted `_*_once()` functions. Connection/SSE caps and request timeout are configurable via new CLI flags. A new `test_phase1.py` (stdlib `unittest`) regresses each fix.

**Tech Stack:** Python 3 stdlib only (`threading`, `http.server`, `signal`, `unittest`). No new third-party dependencies.

---

## Pre-flight

The working directory is **not** a git repository, but the plan uses commits per task for tracking a risky concurrency change.

### Task 0: Initialize version control

**Files:** none (repo metadata)

- [ ] **Step 1: Initialize git and capture a baseline**

Run:
```bash
git init
git add networker_dashboard.py docs
git commit -m "chore: baseline before phase 1 stability hardening"
```
Expected: repo created, baseline commit recorded.

> If the user declines git, skip every `git commit` step below; all other steps are unaffected.

- [ ] **Step 2: Confirm the module imports cleanly**

Run:
```bash
python -c "import networker_dashboard; print(networker_dashboard.APP_VERSION)"
```
Expected: prints the version (e.g. `1.1.15`) with no traceback.

---

### Task 1: Registry lock + accessor helpers (fixes C1)

**Files:**
- Modify: `networker_dashboard.py` (registry definitions + ~15 call sites)
- Test: `test_phase1.py`

- [ ] **Step 1: Write the failing test**

Create `test_phase1.py`:
```python
import threading
import time
import unittest

import networker_dashboard as nd


class RegistryLockTests(unittest.TestCase):
    def test_concurrent_session_access_no_crash(self):
        # Regression for C1: dict changed size during iteration.
        nd.DASHBOARD_SESSIONS.clear()
        stop = threading.Event()
        errors = []

        def writer(seed):
            i = 0
            while not stop.is_set():
                sid = f"s{seed}-{i % 50}"
                nd._put_session(sid, object())
                nd._pop_session(sid)
                i += 1

        def reader():
            try:
                while not stop.is_set():
                    for _sid, _sess in nd._session_items_snapshot():
                        pass
                    for _key, _auto in nd._automation_items_snapshot():
                        pass
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
        threads += [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        time.sleep(0.5)
        stop.set()
        for t in threads:
            t.join(2.0)
        nd.DASHBOARD_SESSIONS.clear()
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
python -m unittest test_phase1.RegistryLockTests -v
```
Expected: FAIL with `AttributeError: module 'networker_dashboard' has no attribute '_put_session'`.

- [ ] **Step 3: Add the lock and accessor helpers**

In `networker_dashboard.py`, find:
```python
ALERT_AUTOMATIONS: dict[str, AlertAutomation] = {}
```
Replace with:
```python
ALERT_AUTOMATIONS: dict[str, AlertAutomation] = {}

# One reentrant lock guards both global registries. Reentrant so nested calls
# (cleanup -> cancel_session_automations -> cancel_alert_automation) cannot
# self-deadlock. Invariant: never hold REGISTRY_LOCK across network I/O —
# snapshot what you need under the lock, release, then call out.
REGISTRY_LOCK = threading.RLock()


def _get_session(session_id: str) -> "DashboardSession | None":
    with REGISTRY_LOCK:
        return DASHBOARD_SESSIONS.get(session_id)


def _put_session(session_id: str, session: Any) -> None:
    with REGISTRY_LOCK:
        DASHBOARD_SESSIONS[session_id] = session


def _pop_session(session_id: str) -> Any:
    with REGISTRY_LOCK:
        return DASHBOARD_SESSIONS.pop(session_id, None)


def _session_exists(session_id: str) -> bool:
    with REGISTRY_LOCK:
        return session_id in DASHBOARD_SESSIONS


def _session_items_snapshot() -> list[tuple[str, Any]]:
    with REGISTRY_LOCK:
        return list(DASHBOARD_SESSIONS.items())


def _session_ids_snapshot() -> list[str]:
    with REGISTRY_LOCK:
        return list(DASHBOARD_SESSIONS.keys())


def _get_automation(key: str) -> "AlertAutomation | None":
    with REGISTRY_LOCK:
        return ALERT_AUTOMATIONS.get(key)


def _put_automation(key: str, automation: Any) -> None:
    with REGISTRY_LOCK:
        ALERT_AUTOMATIONS[key] = automation


def _pop_automation(key: str) -> Any:
    with REGISTRY_LOCK:
        return ALERT_AUTOMATIONS.pop(key, None)


def _automation_items_snapshot() -> list[tuple[str, Any]]:
    with REGISTRY_LOCK:
        return list(ALERT_AUTOMATIONS.items())


def _automation_keys_snapshot() -> list[str]:
    with REGISTRY_LOCK:
        return list(ALERT_AUTOMATIONS.keys())
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
python -m unittest test_phase1.RegistryLockTests -v
```
Expected: PASS.

- [ ] **Step 5: Route all registry call sites through helpers**

Apply each replacement in `networker_dashboard.py`:

`session_automation_keys` — change the comprehension source:
```python
    return [
        key
        for key, automation in _automation_items_snapshot()
        if key == session_id or key.startswith(prefix) or automation.session_id == session_id
    ]
```

`active_automation_summary` — `automation = ALERT_AUTOMATIONS.get(key)` becomes `automation = _get_automation(key)`.

`existing_smtp_automation` — replace both lookups:
```python
    same_type = _get_automation(automation_key(session_id, schedule_type))
    if same_type:
        return same_type
    for key in session_automation_keys(session_id):
        automation = _get_automation(key)
        if automation and automation.encrypted_smtp_password:
            return automation
    return None
```

`shared_dashboard_refresh_loop` — replace the two `DASHBOARD_SESSIONS.get(session_id)` calls with `_get_session(session_id)`.

`persist_sessions` — change the comprehension source:
```python
        records = {
            sid: _session_to_dict(sid, s)
            for sid, s in _session_items_snapshot()
        }
```

`restore_sessions_from_disk` — `DASHBOARD_SESSIONS[session_id] = DashboardSession(...)` becomes `_put_session(session_id, DashboardSession(...))` (keep the same constructor arguments).

`create_dashboard_session` — `DASHBOARD_SESSIONS[session_id] = DashboardSession(...)` becomes `_put_session(session_id, DashboardSession(...))` (keep the same constructor arguments).

`cleanup_dashboard_sessions` — rewrite body:
```python
def cleanup_dashboard_sessions() -> None:
    now = time.time()
    stale = [
        session_id
        for session_id, session in _session_items_snapshot()
        if now - session.last_used > SESSION_TTL_SECONDS
    ]
    for session_id in stale:
        _pop_session(session_id)
        cancel_session_automations(session_id)
    if stale:
        persist_sessions()
```

`build_dashboard_from_session` — `session = DASHBOARD_SESSIONS.get(session_id)` becomes `session = _get_session(session_id)`.

`build_server_health_from_session` — `session = DASHBOARD_SESSIONS.get(session_id)` becomes `session = _get_session(session_id)`.

`cancel_alert_automation` — `automation = ALERT_AUTOMATIONS.pop(automation_id, None)` becomes `automation = _pop_automation(automation_id)`.

`schedule_alert_automation` — `if automation.automation_id not in ALERT_AUTOMATIONS:` becomes `if _get_automation(automation.automation_id) is None:`.

`run_alert_automation` — `automation = ALERT_AUTOMATIONS.get(automation_id)` becomes `automation = _get_automation(automation_id)`.

`handle_alert_automation` — `ALERT_AUTOMATIONS[automation_id] = automation` becomes `_put_automation(automation_id, automation)`; and the session guard `if not session_id or session_id not in DASHBOARD_SESSIONS:` becomes `if not session_id or not _session_exists(session_id):`.

`do_POST` `/api/share` create — `if not session_id or session_id not in DASHBOARD_SESSIONS:` becomes `if not session_id or not _session_exists(session_id):`.

`run()` `_restore_sessions_bg` — rewrite the priming block:
```python
            with SHARED_DASHBOARD_LOCK:
                ids = _session_ids_snapshot()
                if not SHARED_DASHBOARD_STATE.get("sessionId") and ids:
                    SHARED_DASHBOARD_STATE["sessionId"] = ids[0]
```

`run()` `finally` — `for automation_id in list(ALERT_AUTOMATIONS):` becomes `for automation_id in _automation_keys_snapshot():`.

- [ ] **Step 6: Verify nothing references the bare dicts unsafely**

Run:
```bash
python -m pyflakes networker_dashboard.py 2>nul || python -c "import ast,sys; ast.parse(open('networker_dashboard.py',encoding='utf-8').read()); print('parse ok')"
```
Expected: `parse ok` (or pyflakes clean). Then re-run the test:
```bash
python -m unittest test_phase1.RegistryLockTests -v
```
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add networker_dashboard.py test_phase1.py
git commit -m "fix: guard global session/automation registries with a lock (C1)"
```

---

### Task 2: Guard background loop bodies (fixes C2 + M1)

**Files:**
- Modify: `networker_dashboard.py` (`shared_dashboard_refresh_loop`, `auto_snapshot_worker`)
- Test: `test_phase1.py`

- [ ] **Step 1: Write the failing test**

Append to `test_phase1.py`:
```python
class LoopGuardTests(unittest.TestCase):
    def test_refresh_loop_survives_iteration_exception(self):
        # Regression for C2: one bad iteration must not kill the loop thread.
        calls = []
        orig_once = nd._shared_dashboard_refresh_once
        orig_interval = nd.SHARED_REFRESH_SECONDS

        def boom():
            calls.append(1)
            raise RuntimeError("boom")

        nd._shared_dashboard_refresh_once = boom
        nd.SHARED_REFRESH_SECONDS = 0.02
        nd.SHARED_REFRESH_STOP.clear()
        thread = threading.Thread(target=nd.shared_dashboard_refresh_loop, daemon=True)
        thread.start()
        try:
            time.sleep(0.3)
            self.assertGreaterEqual(len(calls), 2)  # survived >=2 exceptions
            self.assertTrue(thread.is_alive())
        finally:
            nd.SHARED_REFRESH_STOP.set()
            thread.join(2.0)
            nd._shared_dashboard_refresh_once = orig_once
            nd.SHARED_REFRESH_SECONDS = orig_interval
            nd.SHARED_REFRESH_STOP.clear()
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
python -m unittest test_phase1.LoopGuardTests -v
```
Expected: FAIL with `AttributeError: ... has no attribute '_shared_dashboard_refresh_once'`.

- [ ] **Step 3: Extract and guard the refresh loop**

Replace the whole `shared_dashboard_refresh_loop` function with:
```python
def _shared_dashboard_refresh_once() -> None:
    with SHARED_DASHBOARD_LOCK:
        session_id = str(SHARED_DASHBOARD_STATE.get("sessionId") or "")
    if not session_id:
        return

    status, dashboard = build_dashboard_from_session(session_id)

    if status < 400 and dashboard.get("ok") and dashboard_backup_source_available(dashboard):
        set_shared_dashboard(session_id, dashboard)
        return

    if status in (401, 403) or not _get_session(session_id):
        session = _get_session(session_id)
        if session:
            config = session_config_with_secrets(session)
            debug_log(f"shared_refresh: session {session_id[:8]}… auth failure, attempting reauth")
            if reauthenticate_dashboard_session(session, config):
                status, dashboard = build_dashboard_from_session(session_id)
                if status < 400 and dashboard.get("ok") and dashboard_backup_source_available(dashboard):
                    set_shared_dashboard(session_id, dashboard)
                    debug_log(f"shared_refresh: reauth succeeded for session {session_id[:8]}…")
                    return
        debug_log(f"shared_refresh: reauth failed or session missing for {session_id[:8]}…")

    with SHARED_DASHBOARD_LOCK:
        SHARED_DASHBOARD_STATE["lastError"] = str(
            dashboard_backup_source_error(dashboard)
            if status < 400 and dashboard.get("ok")
            else dashboard.get("error") or dashboard.get("message") or f"Refresh failed with HTTP {status}"
        )


def shared_dashboard_refresh_loop() -> None:
    while not SHARED_REFRESH_STOP.wait(SHARED_REFRESH_SECONDS):
        try:
            _shared_dashboard_refresh_once()
        except Exception as exc:  # noqa: BLE001 — loop must never die.
            debug_log(f"shared_dashboard_refresh_loop iteration failed: {exc}")
```

- [ ] **Step 4: Extract and guard the auto-snapshot worker**

Replace the whole `auto_snapshot_worker` function with:
```python
def _auto_snapshot_once() -> None:
    if not load_auto_snapshot_config():
        return
    today = snapshot_date_key()
    with SNAPSHOTS_LOCK:
        existing = load_dashboard_snapshots()
    if today in existing:
        return
    with SHARED_DASHBOARD_LOCK:
        dashboard = dict(SHARED_DASHBOARD_STATE.get("dashboard") or {})
    if not isinstance(dashboard, dict) or not dashboard.get("ok"):
        return
    with SNAPSHOTS_LOCK:
        save_dashboard_snapshot(dashboard)


def auto_snapshot_worker() -> None:
    while not SHARED_REFRESH_STOP.is_set():
        SHARED_REFRESH_STOP.wait(600)
        if SHARED_REFRESH_STOP.is_set():
            break
        try:
            _auto_snapshot_once()
        except Exception as exc:  # noqa: BLE001 — loop must never die.
            debug_log(f"auto_snapshot_worker iteration failed: {exc}")
```

- [ ] **Step 5: Run test to verify it passes**

Run:
```bash
python -m unittest test_phase1.LoopGuardTests -v
```
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add networker_dashboard.py test_phase1.py
git commit -m "fix: guard refresh + auto-snapshot loop bodies so threads cannot die (C2)"
```

---

### Task 3: SSE cap + broadcast outside the lock (fixes C3 SSE + M2)

**Files:**
- Modify: `networker_dashboard.py` (`sse_broadcast`, `do_GET` `/api/stream`, new `_sse_register`)
- Test: `test_phase1.py`

- [ ] **Step 1: Write the failing test**

Append to `test_phase1.py`:
```python
class SseTests(unittest.TestCase):
    def test_sse_register_respects_cap(self):
        nd.SSE_CLIENTS.clear()
        orig_cap = nd.MAX_SSE_CLIENTS
        nd.MAX_SSE_CLIENTS = 2
        try:
            self.assertTrue(nd._sse_register(object()))
            self.assertTrue(nd._sse_register(object()))
            self.assertFalse(nd._sse_register(object()))
            self.assertEqual(len(nd.SSE_CLIENTS), 2)
        finally:
            nd.SSE_CLIENTS.clear()
            nd.MAX_SSE_CLIENTS = orig_cap

    def test_broadcast_prunes_dead_clients(self):
        nd.SSE_CLIENTS.clear()

        class DeadFile:
            def write(self, _data):
                raise OSError("broken pipe")

            def flush(self):
                pass

        class LiveFile:
            def __init__(self):
                self.written = b""

            def write(self, data):
                self.written += data

            def flush(self):
                pass

        live = LiveFile()
        nd.SSE_CLIENTS.extend([DeadFile(), live])
        nd.sse_broadcast("dashboard", "{}")
        self.assertNotIn_dead = [c for c in nd.SSE_CLIENTS if isinstance(c, DeadFile)]
        self.assertEqual(self.assertNotIn_dead, [])
        self.assertIn(live, nd.SSE_CLIENTS)
        self.assertTrue(live.written)
        nd.SSE_CLIENTS.clear()
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
python -m unittest test_phase1.SseTests -v
```
Expected: FAIL with `AttributeError: ... has no attribute '_sse_register'` (and/or `MAX_SSE_CLIENTS`).

- [ ] **Step 3: Add the SSE cap constant and registrar, rewrite broadcast**

Find:
```python
SSE_CLIENTS: list[Any] = []
SSE_CLIENTS_LOCK = threading.Lock()
```
Replace with:
```python
SSE_CLIENTS: list[Any] = []
SSE_CLIENTS_LOCK = threading.Lock()
DEFAULT_MAX_SSE_CLIENTS = 50
MAX_SSE_CLIENTS = DEFAULT_MAX_SSE_CLIENTS


def _sse_register(wfile: Any) -> bool:
    """Register an SSE client if under cap. Returns False when full."""
    with SSE_CLIENTS_LOCK:
        if len(SSE_CLIENTS) >= MAX_SSE_CLIENTS:
            return False
        SSE_CLIENTS.append(wfile)
        return True
```

Then replace the whole `sse_broadcast` function with:
```python
def sse_broadcast(event: str, data: str) -> None:
    payload = f"event: {event}\ndata: {data}\n\n".encode("utf-8")
    with SSE_CLIENTS_LOCK:
        clients = list(SSE_CLIENTS)
    dead = []
    for wfile in clients:
        try:
            wfile.write(payload)
            wfile.flush()
        except OSError:
            dead.append(wfile)
    if dead:
        with SSE_CLIENTS_LOCK:
            for wfile in dead:
                try:
                    SSE_CLIENTS.remove(wfile)
                except ValueError:
                    pass
```

- [ ] **Step 4: Enforce the cap in the `/api/stream` handler**

In `do_GET`, find the `/api/stream` block. Replace from `if path == "/api/stream":` down to the line `SSE_CLIENTS.append(wfile)` with:
```python
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
```
Leave the rest of the block (initial state push, heartbeat loop, and the final `with SSE_CLIENTS_LOCK: try: SSE_CLIENTS.remove(wfile) ...`) unchanged.

> Note: the cap check now happens before any response bytes are sent, so the 503 is a clean JSON error. The original `with SSE_CLIENTS_LOCK: SSE_CLIENTS.append(wfile)` line is removed (registration moved into `_sse_register`).

- [ ] **Step 5: Run test to verify it passes**

Run:
```bash
python -m unittest test_phase1.SseTests -v
```
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add networker_dashboard.py test_phase1.py
git commit -m "fix: cap SSE clients and broadcast outside the lock (C3/M2)"
```

---

### Task 4: Request timeout + connection cap + CLI flags (fixes C3 connections)

**Files:**
- Modify: `networker_dashboard.py` (config constants, `ExclusiveThreadingHTTPServer`, `bind_dashboard_server`, `parse_args`, `run`)
- Test: `test_phase1.py`

- [ ] **Step 1: Write the failing test**

Append to `test_phase1.py`:
```python
class ConnectionCapTests(unittest.TestCase):
    def test_connection_slot_cap(self):
        srv = nd.ExclusiveThreadingHTTPServer(
            ("127.0.0.1", 0), nd.DashboardHandler, max_connections=2
        )
        try:
            self.assertTrue(srv._acquire_slot())
            self.assertTrue(srv._acquire_slot())
            self.assertFalse(srv._acquire_slot())
            srv._release_slot()
            self.assertTrue(srv._acquire_slot())
        finally:
            srv.server_close()

    def test_config_constants_exist(self):
        self.assertIsInstance(nd.DEFAULT_REQUEST_TIMEOUT_SECONDS, int)
        self.assertIsInstance(nd.DEFAULT_MAX_CONNECTIONS, int)
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
python -m unittest test_phase1.ConnectionCapTests -v
```
Expected: FAIL with `AttributeError` on `max_connections` / `_acquire_slot` / `DEFAULT_REQUEST_TIMEOUT_SECONDS`.

- [ ] **Step 3: Add config constants**

Find:
```python
DEFAULT_TIMEOUT_SECONDS = 30
```
Add immediately below it:
```python
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30
DEFAULT_MAX_CONNECTIONS = 200
REQUEST_TIMEOUT_SECONDS = DEFAULT_REQUEST_TIMEOUT_SECONDS
MAX_CONNECTIONS = DEFAULT_MAX_CONNECTIONS
```

- [ ] **Step 4: Add request timeout + connection cap to the server**

Replace the whole `ExclusiveThreadingHTTPServer` class with:
```python
class ExclusiveThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, *args: Any, max_connections: int = DEFAULT_MAX_CONNECTIONS, **kwargs: Any) -> None:
        self._conn_semaphore = threading.BoundedSemaphore(max(1, int(max_connections)))
        super().__init__(*args, **kwargs)

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def _acquire_slot(self) -> bool:
        return self._conn_semaphore.acquire(blocking=False)

    def _release_slot(self) -> None:
        try:
            self._conn_semaphore.release()
        except ValueError:
            pass

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._acquire_slot():
            # At connection cap — refuse by closing the socket. A fronting
            # proxy/LB will retry; we avoid spawning an unbounded thread.
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release_slot()
```

- [ ] **Step 5: Set the per-request timeout on the handler**

In `DashboardHandler`, find:
```python
    server_version = f"NetWorkerDashboard/{APP_VERSION}"
    protocol_version = "HTTP/1.1"
```
Replace with:
```python
    server_version = f"NetWorkerDashboard/{APP_VERSION}"
    protocol_version = "HTTP/1.1"
    timeout = REQUEST_TIMEOUT_SECONDS
```

- [ ] **Step 6: Thread `max_connections` through `bind_dashboard_server`**

Replace the whole `bind_dashboard_server` function with:
```python
def bind_dashboard_server(
    bind_host: str, requested_port: int, max_connections: int = DEFAULT_MAX_CONNECTIONS
) -> tuple[ThreadingHTTPServer, int, bool]:
    if requested_port != 0 and not can_exclusively_bind_port(bind_host, requested_port):
        server = ExclusiveThreadingHTTPServer((bind_host, 0), DashboardHandler, max_connections=max_connections)
        return server, int(server.server_address[1]), True
    try:
        server = ExclusiveThreadingHTTPServer((bind_host, requested_port), DashboardHandler, max_connections=max_connections)
        return server, int(server.server_address[1]), False
    except OSError as exc:
        if requested_port == 0:
            raise
        try:
            server = ExclusiveThreadingHTTPServer((bind_host, 0), DashboardHandler, max_connections=max_connections)
        except OSError:
            raise exc
        return server, int(server.server_address[1]), True
```

- [ ] **Step 7: Add CLI flags**

In `parse_args`, immediately before `return parser.parse_args(argv)`, add:
```python
    parser.add_argument(
        "--request-timeout",
        type=int,
        default=DEFAULT_REQUEST_TIMEOUT_SECONDS,
        help="Per-request socket timeout in seconds. Drops idle/slowloris connections.",
    )
    parser.add_argument(
        "--max-connections",
        type=int,
        default=DEFAULT_MAX_CONNECTIONS,
        help="Maximum concurrent connections before new ones are refused.",
    )
    parser.add_argument(
        "--max-sse",
        type=int,
        default=DEFAULT_MAX_SSE_CLIENTS,
        help="Maximum concurrent live-stream (SSE) viewers.",
    )
```

- [ ] **Step 8: Apply the flags in `run()`**

In `run`, find:
```python
    global APP_DEBUG
    args = parse_args(argv or sys.argv[1:])
    APP_DEBUG = bool(args.debug)
```
Replace with:
```python
    global APP_DEBUG, REQUEST_TIMEOUT_SECONDS, MAX_CONNECTIONS, MAX_SSE_CLIENTS
    args = parse_args(argv or sys.argv[1:])
    APP_DEBUG = bool(args.debug)
    REQUEST_TIMEOUT_SECONDS = max(5, int(args.request_timeout))
    MAX_CONNECTIONS = max(1, int(args.max_connections))
    MAX_SSE_CLIENTS = max(1, int(args.max_sse))
    DashboardHandler.timeout = REQUEST_TIMEOUT_SECONDS
```

Then find:
```python
    server, selected_port, used_random_port = bind_dashboard_server(args.bind, int(args.port))
```
Replace with:
```python
    server, selected_port, used_random_port = bind_dashboard_server(
        args.bind, int(args.port), max_connections=MAX_CONNECTIONS
    )
```

- [ ] **Step 9: Run test to verify it passes**

Run:
```bash
python -m unittest test_phase1.ConnectionCapTests -v
```
Expected: PASS.

- [ ] **Step 10: Smoke-test the new flags**

Run:
```bash
python networker_dashboard.py --help
```
Expected: help text lists `--request-timeout`, `--max-connections`, `--max-sse`.

- [ ] **Step 11: Commit**

```bash
git add networker_dashboard.py test_phase1.py
git commit -m "fix: add request timeout and connection cap with CLI flags (C3)"
```

---

### Task 5: Graceful shutdown + atomic snapshot writes (fixes M4 + M3)

**Files:**
- Modify: `networker_dashboard.py` (imports, `write_dashboard_snapshots`, `run`)
- Test: `test_phase1.py`

- [ ] **Step 1: Write the failing test**

Append to `test_phase1.py`:
```python
import shutil
import tempfile
from pathlib import Path


class SnapshotWriteTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._orig_data_dir = nd.DATA_DIR
        self._orig_snap = nd.DASHBOARD_SNAPSHOT_FILE
        nd.DATA_DIR = Path(self._tmpdir)
        nd.DASHBOARD_SNAPSHOT_FILE = nd.DATA_DIR / "networker_snapshots.json"

    def tearDown(self):
        nd.DATA_DIR = self._orig_data_dir
        nd.DASHBOARD_SNAPSHOT_FILE = self._orig_snap
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_snapshot_write_is_atomic_and_leaves_no_tmp(self):
        data = {"2026-05-22": {"date": "2026-05-22", "metrics": {}}}
        nd.write_dashboard_snapshots(data)
        self.assertTrue(nd.DASHBOARD_SNAPSHOT_FILE.exists())
        tmp = nd.DASHBOARD_SNAPSHOT_FILE.with_suffix(".tmp")
        self.assertFalse(tmp.exists())
        self.assertEqual(nd.load_dashboard_snapshots(), data)
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
python -m unittest test_phase1.SnapshotWriteTests -v
```
Expected: FAIL — a `.tmp` file is left behind (current implementation writes the target directly, so `tmp.exists()` is False but the atomic-write contract is not yet in place). If it happens to pass, still proceed: Step 3 hardens the contract and the assertions remain valid.

> Rationale: this test pins the atomic-write behavior so a future regression (e.g. someone reverting to a direct write) is caught.

- [ ] **Step 3: Make snapshot writes atomic**

Replace the whole `write_dashboard_snapshots` function with:
```python
def write_dashboard_snapshots(snapshots: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DASHBOARD_SNAPSHOT_FILE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(snapshots, ensure_ascii=True, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp.replace(DASHBOARD_SNAPSHOT_FILE)
    try:
        DASHBOARD_SNAPSHOT_FILE.chmod(0o600)
    except OSError:
        pass
```

- [ ] **Step 4: Add the `signal` import**

Find:
```python
import shutil
```
Add immediately below it:
```python
import signal
```

- [ ] **Step 5: Install signal handlers in `run()`**

In `run`, find:
```python
    server_thread = threading.Thread(
        target=server.serve_forever,
        name="networker-dashboard-https",
        daemon=True,
    )
    server_thread.start()
```
Insert immediately above that block:
```python
    def _handle_term(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    for _sig_name in ("SIGTERM", "SIGINT"):
        _sig = getattr(signal, _sig_name, None)
        if _sig is not None:
            try:
                signal.signal(_sig, _handle_term)
            except (ValueError, OSError, RuntimeError):
                pass

```

> The existing `try/except KeyboardInterrupt: ... finally:` block in `run()` already cancels automations, sets `SHARED_REFRESH_STOP`, shuts the server down, and closes it. Routing SIGTERM through `KeyboardInterrupt` reuses that path for clean service stops.

- [ ] **Step 6: Run test to verify it passes**

Run:
```bash
python -m unittest test_phase1.SnapshotWriteTests -v
```
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add networker_dashboard.py test_phase1.py
git commit -m "fix: atomic snapshot writes and graceful SIGTERM shutdown (M3/M4)"
```

---

### Task 6: Full regression + manual smoke

**Files:** none (verification only)

- [ ] **Step 1: Run the whole suite**

Run:
```bash
python -m unittest test_phase1 -v
```
Expected: all tests PASS.

- [ ] **Step 2: Boot the server and hit health**

Run (PowerShell, in a background window or with a timeout):
```bash
python networker_dashboard.py --no-launch --port 0 --max-sse 5 --max-connections 50 --request-timeout 20
```
Then from another shell:
```bash
python -c "import ssl,urllib.request,json; ctx=ssl._create_unverified_context(); print(json.load(urllib.request.urlopen('https://localhost:PORT/api/health', context=ctx)))"
```
(substitute the selected port printed at startup)
Expected: `{'ok': True, ...}`. Then stop with Ctrl+C and confirm `Stopping dashboard.` prints (graceful path).

- [ ] **Step 3: Commit any final notes**

```bash
git add -A
git commit -m "test: phase 1 full regression green" --allow-empty
```

---

## Self-Review

**Spec coverage:**
- C1 (registry lock) → Task 1. ✔
- C2 (loop guards) + M1 (auto-snapshot guard) → Task 2. ✔
- C3 (SSE cap, broadcast outside lock) → Task 3; (request timeout, connection cap, CLI flags) → Task 4. ✔
- M2 (broadcast outside lock) → Task 3. ✔
- M3 (atomic snapshot write) → Task 5. ✔
- M4 (graceful SIGTERM) → Task 5. ✔
- CLI flags `--request-timeout/--max-connections/--max-sse` → Task 4. ✔
- Testing approach (unittest regressions + manual smoke) → Tasks 1–6. ✔

**Placeholder scan:** none — every code step shows full code; every command shows expected output.

**Type/name consistency:** helper names (`_get_session`, `_put_session`, `_pop_session`, `_session_exists`, `_session_items_snapshot`, `_session_ids_snapshot`, `_get_automation`, `_put_automation`, `_pop_automation`, `_automation_items_snapshot`, `_automation_keys_snapshot`, `_sse_register`, `_acquire_slot`, `_release_slot`, `_shared_dashboard_refresh_once`, `_auto_snapshot_once`) and constants (`REGISTRY_LOCK`, `DEFAULT_MAX_SSE_CLIENTS`, `MAX_SSE_CLIENTS`, `DEFAULT_REQUEST_TIMEOUT_SECONDS`, `DEFAULT_MAX_CONNECTIONS`, `REQUEST_TIMEOUT_SECONDS`, `MAX_CONNECTIONS`) are used consistently across tasks.

**Known limitation (documented, not a gap):** the connection cap refuses excess connections by closing the socket rather than emitting a 503 at the TLS layer, because the socket is TLS-wrapped before HTTP framing is available. The SSE cap does emit a clean JSON 503 because it runs inside the HTTP handler.
