# NetWorker Dashboard — Phase 1 Stability Hardening

Date: 2026-05-22
Status: Approved
Target file: `networker_dashboard.py` (single-file by design)

## Goal

Make the dashboard survive 24x7x365 operation. Fix the concurrency and
resource-lifecycle defects that cause hard crashes, frozen refresh threads, and
thread/FD exhaustion. No change to API contracts or the single-file layout.

## Background (defects this addresses)

- **C1** `DASHBOARD_SESSIONS` and `ALERT_AUTOMATIONS` are global dicts mutated
  from HTTP threads, the shared-refresh loop, Timer threads, and the
  session-restore background thread, with **no lock**. Iterate-while-mutate ->
  `RuntimeError: dictionary changed size during iteration`.
- **C2** `shared_dashboard_refresh_loop` and `auto_snapshot_worker` run their
  iteration bodies with no exception guard. A single unhandled exception kills
  the thread permanently -> dashboard frozen until process restart. C1 is a
  concrete trigger for C2.
- **C3** No per-request socket timeout; `ThreadingHTTPServer` spawns an
  uncapped thread per connection; HTTP/1.1 keep-alive holds threads; SSE handler
  holds one thread per client forever with no client cap. Slowloris / connection
  exhaustion over long uptime.
- **M2** `sse_broadcast` holds the client lock during blocking write+flush; one
  slow client stalls all broadcasts.
- **M3** `write_dashboard_snapshots` writes non-atomically -> corruption on crash.
- **M4** Only `KeyboardInterrupt` handled; SIGTERM (service stop) bypasses
  graceful shutdown.

## Design decisions (approved)

- **Locking:** one `threading.RLock()` (`REGISTRY_LOCK`) guarding both global
  registries. Reentrant so nested calls (cleanup -> cancel_session_automations
  -> cancel_alert_automation) do not self-deadlock. Low contention at this scale.
- **Threading model:** keep `ThreadingHTTPServer`. Add per-request timeout +
  connection/SSE caps. Bounded worker pool deferred to a later phase.
- **Caps as CLI flags** with defaults: `--request-timeout 30`,
  `--max-connections 200`, `--max-sse 50`.

## Changes

### 1. Lock the global registries
- Add `REGISTRY_LOCK = threading.RLock()` near the registry definitions.
- Guard every read/write/iterate of `DASHBOARD_SESSIONS` and
  `ALERT_AUTOMATIONS`. Iterations operate on `list(...)` snapshots taken under
  the lock.
- Call sites covered: `create_dashboard_session`, `cleanup_dashboard_sessions`,
  `persist_sessions`, `restore_sessions_from_disk`, `build_dashboard_from_session`,
  `build_server_health_from_session`, `shared_dashboard_refresh_loop` session
  lookups, `cancel_alert_automation`, `cancel_session_automations`,
  `session_automation_keys`, `existing_smtp_automation`, `active_automation_summary`,
  `schedule_alert_automation`, `run_alert_automation` rescheduling, the share
  endpoint's `session_id in DASHBOARD_SESSIONS` check, `handle_alert_automation`,
  and the `_restore_sessions_bg` priming block.
- **Invariant:** never hold `REGISTRY_LOCK` across network I/O. Snapshot the
  needed objects under the lock, release, then perform I/O.

### 2. Guard long-lived loop bodies
- `shared_dashboard_refresh_loop`: wrap the per-iteration body in
  `try/except Exception` -> `debug_log(...)` + continue. Exit still only via
  `SHARED_REFRESH_STOP`.
- `auto_snapshot_worker`: same guard around the full body after the wait.

### 3. Request timeout + connection/SSE caps
- `DashboardHandler.timeout = REQUEST_TIMEOUT` so idle/slowloris sockets are
  dropped by the handler.
- Connection cap: `BoundedSemaphore(MAX_CONNECTIONS)`. Override
  `process_request` on `ExclusiveThreadingHTTPServer` to `acquire(blocking=False)`;
  on failure, refuse the connection (close socket) without spawning a worker.
  Release the semaphore in the worker teardown path
  (`process_request_thread` / `shutdown_request`).
- SSE cap: in `/api/stream`, when `len(SSE_CLIENTS) >= MAX_SSE_CLIENTS`, respond
  503 and return instead of registering the client.
- New argparse flags wired into `run()` and module-level config:
  `--request-timeout`, `--max-connections`, `--max-sse`.

### 4. SSE broadcast outside the lock
- `sse_broadcast`: copy the client list under `SSE_CLIENTS_LOCK`, perform
  write+flush outside the lock, then re-acquire to prune dead clients.

### 5. Graceful shutdown + atomic snapshot writes
- Install `signal.signal` handlers for SIGINT and (where supported) SIGTERM in
  `run()`. Handler sets `SHARED_REFRESH_STOP` and calls `server.shutdown()`; the
  existing `finally` block performs automation cancel + persist + close.
- `write_dashboard_snapshots`: write to `.tmp` then `Path.replace()` (atomic),
  matching `persist_sessions` / `save_profiles`.

## Testing

No test harness exists in the repo. Add `test_phase1.py` (stdlib `unittest`):
- Concurrent session create/cleanup across N threads completes with no
  `RuntimeError` (regression for C1).
- Injected exception inside one refresh-loop iteration does not stop the loop
  (regression for C2) — tested against the guarded body via a seam.
- SSE registration refused once the cap is reached.
- `write_dashboard_snapshots` leaves no `.tmp` and writes valid JSON; simulated
  mid-write failure does not corrupt the existing file.

Manual smoke: `--help` shows new flags; server boots; `GET /api/health` returns
ok; `SIGINT` shuts down cleanly.

## Out of scope (later phases)

Authentication, SSRF host allowlist, Windows DPAPI key protection, logging
framework + rotation, per-session/per-server dashboard isolation, bounded worker
pool.

## Notes

Working directory is not a git repository, so this spec is written but not
committed. Run `git init` if version control of the spec/changes is wanted.
