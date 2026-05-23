# NetWorker Dashboard — Phase 3: Structured Logging + Rotation + /api/status

Date: 2026-05-22
Status: Approved
Target file: `networker_dashboard.py` (single-file by design)
Tests: `test_phase3.py` (stdlib `unittest`)

## Goal

Replace ad-hoc `sys.stderr.write` logging with the stdlib `logging` module:
JSON-structured lines, a size-rotating file handler plus console, per-request
correlation IDs, and a cookie-gated `/api/status` operational endpoint. Closes
audit item **M5** (no logging framework / rotation) and adds the observability
needed for an external watchdog.

## Approved decisions

- **Format:** JSON structured, one object per line.
- **Sink:** `RotatingFileHandler` in a new `logs/` dir (gitignored), 10MB x 5,
  plus a `StreamHandler(stderr)`.
- **Health surface:** `/api/health` stays minimal + open (LB liveness); new
  cookie-gated `/api/status` carries the rich metrics.
- **Scope:** logging + rotation + request IDs + `/api/status`. No Prometheus.
- **Startup banner:** keep the human-readable `print()` banner AND emit
  structured `LOG` lines for the same key events (mild redundancy accepted).

## Components

### 1. Logging core
- New imports: `import logging`, `import logging.handlers`.
- Constants near the other path constants:
  - `LOG_DIR = APP_BASE_DIR / "logs"`
  - `LOG_FILE = LOG_DIR / "networker_dashboard.log"`
  - `PROCESS_START_TIME = time.time()` (module load time, for uptime).
- Module logger: `LOG = logging.getLogger("networker_dashboard")` (defined once,
  near the top after constants so all later functions can use it).
- `_LOG_EXTRA_KEYS = ("request_id", "client", "status", "path", "event")`.
- `class _JsonLogFormatter(logging.Formatter)`: `format(record)` returns
  `json.dumps({...})` with keys: `ts` (UTC ISO from `record.created`), `level`
  (`record.levelname`), `logger` (`record.name`), `msg` (`record.getMessage()`),
  any of `_LOG_EXTRA_KEYS` present on the record, and `exc`
  (`self.formatException(record.exc_info)`) when `record.exc_info` is set.
  `ensure_ascii=True, default=str`.
- `configure_logging(debug: bool) -> None`:
  - `LOG.setLevel(logging.DEBUG if debug else logging.INFO)`.
  - `LOG.handlers.clear()` (idempotent — no pileup on repeat calls).
  - Best-effort `LOG_DIR.mkdir(...)` + `RotatingFileHandler(LOG_FILE,
    maxBytes=10*1024*1024, backupCount=5, encoding="utf-8")` with the JSON
    formatter; wrap in try/except OSError (file handler optional if dir
    unwritable).
  - Always add `StreamHandler(sys.stderr)` with the JSON formatter.
  - `LOG.propagate = False`.
  - Called early in `run()` (right after `APP_DEBUG` is set).

### 2. Migrate existing log sites
- `debug_log(message)` → `LOG.debug(safe_log_text(message, 520))`.
- `DashboardHandler.log_message(format, *args)` →
  `LOG.info(format % args, extra={"request_id": getattr(self, "request_id",
  "-"), "client": self.client_address[0]})`.
- `log_dashboard_failure(status, body)` → emit a `LOG.warning` summary and a
  `LOG.warning` per failed source, carrying `request_id` + `status` extras
  (preserve the same detail content as today).
- DPAPI `sys.stderr.write(...)` warnings in `_write_protected_key` /
  `_read_protected_key` → `LOG.warning(...)`. These may run at import before
  `configure_logging`; with no handlers yet, Python's logging `lastResort`
  emits to stderr (plain). Acceptable (rare, startup-only).
- Startup `print()` banner in `run()` stays as-is for the operator console.
  Additionally emit a single `LOG.info("startup", extra={"event": "startup",
  ...})` and `LOG.warning(...)` mirror of the exposed-without-auth WARNING.

### 3. Request IDs
- `DashboardHandler.request_id` class attribute default `"-"`.
- First line inside both `do_GET` and `do_POST` (after `_require_https`):
  `self.request_id = uuid.uuid4().hex[:8]`.
- The 500 catch-all in both handlers uses `self.request_id` as the `ref` (so the
  `Internal error (ref X)` returned to the client equals the `request_id` in the
  log line — directly correlatable). Replace the current `ref = uuid.uuid4()...`
  with `ref = self.request_id`.

### 4. /api/status (cookie-gated)
- New route in `do_GET`, placed AFTER the auth gate (so it requires login when
  auth is enabled), before the existing protected routes. Returns 200 JSON:
  - `ok`: True
  - `version`: APP_VERSION
  - `uptimeSeconds`: `int(time.time() - PROCESS_START_TIME)`
  - `threads`: `threading.active_count()`
  - `sessions`: `len(_session_ids_snapshot())`
  - `automations`: `len(_automation_keys_snapshot())`
  - `sseClients`: `len(SSE_CLIENTS)` (read under `SSE_CLIENTS_LOCK`)
  - `sharedDashboard`: `{lastRefresh, lastRefreshAgeSeconds, lastError}` read
    under `SHARED_DASHBOARD_LOCK` (`lastRefreshAgeSeconds = int(now -
    SHARED_DASHBOARD_STATE["updatedAt"])` when `updatedAt` > 0, else `None`)
  - `authEnabled`: AUTH_ENABLED
  - `allowlistEnabled`: ALLOWLIST_ENABLED
- `/api/health` unchanged (open, minimal).

### 5. .gitignore + tests
- Add `logs/` to `.gitignore`.
- `test_phase3.py`:
  - `_JsonLogFormatter`: format a `LogRecord` → parse JSON → assert keys
    `ts/level/logger/msg`; with `extra={"request_id": "abc123"}` → assert
    `request_id` present.
  - `configure_logging(True)` then `configure_logging(False)`: assert `LOG`
    has handlers and that repeat calls do NOT accumulate (handler count stable);
    assert `LOG.level` reflects the flag; assert a log call writes a line to the
    rotating file (point `LOG_FILE` at a temp dir via monkeypatch first, then
    re-`configure_logging`).
  - Monkeypatch restores original `LOG` handlers/level + `LOG_DIR`/`LOG_FILE` in
    tearDown so other suites are unaffected.
- `/api/status` payload verified in the live smoke (boot, login, GET
  `/api/status`, assert keys), consistent with prior phases.

## Out of scope

Prometheus `/metrics`, syslog/remote shipping, per-session dashboard isolation.

## Notes

- `LOG` must be defined before any function that calls it; place its definition
  with the logging core near the top of the module. `configure_logging` is what
  attaches handlers — until it runs, log calls use the logging default.
- Test isolation: `test_phase3` mutates global logging state; tearDown must
  restore handlers, level, and `LOG_DIR`/`LOG_FILE` so `test_phase1/2/2b` stay
  green when run together.
