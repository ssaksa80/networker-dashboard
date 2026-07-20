# Scheduled Reports subsystem — greenfield redesign

**Date:** 2026-07-20
**Status:** Approved (design)
**Supersedes:** the `AlertAutomation` email-automation code path in `nwdash/emailer.py`

## Problem

Scheduled email notifications silently stop working. Root cause, confirmed on the
production host:

- A scheduled automation renders its dashboard at fire time by **borrowing a
  browser's NetWorker session**. The session is captured as a "connection
  snapshot" onto the automation at arm time via
  `connection_snapshot_for_session(session_id)`, which only reads the
  **in-memory** session registry.
- After a service restart (or session TTL eviction) the browser's session is
  dead server-side. Arming a schedule then captures `connection: {}` — an empty
  snapshot. The schedule persists in that broken state but the UI still shows it
  "Active".
- At fire time `_ensure_automation_session` finds no session and no usable
  snapshot, so it **skips the run and sends nothing** — silently.
- "Send test" works because it emails dashboard JSON supplied by the browser in
  the request and never needs a live server-side session, masking the failure.

Observed on the production host: two enabled schedules (`daily_report`, `alert`)
both persisted with `connection: {}`; three credentialed sessions existed only
on disk (`sessions.json`, days old, not in memory); a steady
`POST /api/server-health 401` loop confirmed the browser session was dead while
the UI badge still read "Connected".

## Goals

1. A scheduled report never depends on an interactive browser session.
2. A schedule cannot exist in a broken ("Active" but non-functional) state.
3. A fire-time failure is never silent.
4. Credentials survive restarts and service-account changes on the same host.

## Non-goals

- Redesigning SMTP delivery itself. The existing `send_smtp_email` core is sound
  and is reused unchanged.
- Multi-host credential portability (a host move requires re-entering the
  credential — acceptable, see Credential-at-rest).

## Core principle

A scheduled report is a **self-contained, credentialed, validated-on-save,
observable job**. It carries its own NetWorker credential and every setting it
needs to run. Fire time connects fresh with that credential — no session store,
no snapshot borrowing.

## Data model

New record `ReportJob`, persisted to a new `data/report_jobs.json` (atomic
write). The legacy `data/automations.json` is read once for migration detection,
then retired.

```
ReportJob:
  id: str
  kind: "digest" | "alert"          # digest = scheduled report; alert = threshold-triggered
  enabled: bool
  credential:                        # the NetWorker connection this job OWNS
    rest_api_host, rest_api_port
    backup_server_host, backup_server_port
    username
    encrypted_password               # machine-scoped DPAPI (Fernet key-file fallback)
    api_mode, api_version
    verify_tls
    report_range
  schedule:
    # digest:
    report_time: "HH:MM"
    cadence: "daily"                 # daily to start; weekly/monthly deferred
    # alert:
    interval_minutes: int
    trigger: "critical" | "warning" | ...
  recipients: [str]
  quiet_start, quiet_end: "HH:MM" | ""
  digest: bool                       # one email per interval (alert kind)
  theme: str
  health:
    last_run: float
    last_result: str
    last_success: float
    next_run: float
    consecutive_failures: int
    state: "healthy" | "unhealthy" | "never_run"
```

SMTP settings and the `ops_alert_address` live in one app-level config
(`email_config.json`), not per job — SMTP is already global.

### Credential at rest

Encrypt `credential.encrypted_password` with **machine-scoped DPAPI**
(`CryptProtectData` with `CRYPTPROTECT_LOCAL_MACHINE`). Any account on the host
can decrypt, so a service-account change (e.g. the scheduled-task → NSSM-service
switch) does not invalidate it. On non-Windows/dev, fall back to the app's
persisted Fernet key. A host move requires re-entering the credential (by
design — machine-scoped keys do not travel).

## Components

Each is a small, independently testable unit.

1. **CredentialStore** (`nwdash/report_cred.py`) — encrypt/decrypt the NetWorker
   credential. `save(job_id, cred) -> token`, `load(job_id) -> cred | None`.
   Machine-DPAPI with Fernet fallback. No other responsibility.

2. **ReportRenderer** (`nwdash/report_render.py`) — given a credential, connect
   fresh to NetWorker, build the dashboard dict, render plain + HTML + PNG.
   `render(cred) -> RenderResult(ok, dashboard, error)`. No session registry.
   This is the decoupling.

3. **LastGoodCache** — per-job last successful dashboard snapshot, for fallback
   emails. `put(job_id, dashboard)`, `get(job_id) -> dashboard | None`. Stored
   under `data/` so it survives restarts.

4. **Notifier** (`nwdash/report_notify.py`) — reuses `send_smtp_email`.
   `send_report(job, dashboard, stale=False)`, `send_ops_alert(job, error)`.

5. **JobValidator** — the hard save gate: connect + render + SMTP verify.
   `validate(job) -> ValidationResult(ok, checks)` with per-check detail
   (`credential`, `render`, `smtp`).

6. **Scheduler** (`nwdash/report_jobs.py`) — single background tick loop (keep
   the existing robust `next_run_at` model), computes `next_run`, fires due
   jobs, records health. Session-free.

7. **JobStore** (`nwdash/report_jobs.py`) — atomic persistence + boot restore of
   `ReportJob`s. Independent of sessions.

8. **API** — new `/api/report-jobs`: `list`, `create`, `update`, `delete`,
   `enable`/`disable`, `test`, `health`. `/api/alert-automation` becomes a thin
   deprecation shim returning a clear "moved" message.

9. **UI** — new **Scheduled Reports** panel in the dashboard: per-job health
   line (last run / last result / next run + healthy badge), a create/edit form
   with NetWorker credential capture and a live **Test** button (runs
   JobValidator), the ops-alert address, and the SMTP config section.

## Data flow

### Save / enable (hard gate)

```
POST /api/report-jobs (create|update)
  -> JobValidator.validate(job):
       1. connect to NetWorker with the job's credential
       2. render the report once
       3. SMTP connect (EHLO/STARTTLS/login as configured)
  -> all pass:  store job (enabled), CredentialStore.save, LastGoodCache.put(first render)
  -> any fail:  HTTP 400 naming the failed check; job NOT stored as enabled
```

A schedule can never reach "Active" without proving all three legs work.

### Fire time

```
Scheduler tick -> job due ->
  RenderResult = ReportRenderer.render(CredentialStore.load(job.id))
  if ok:
    Notifier.send_report(job, dashboard)
    LastGoodCache.put(job.id, dashboard)
    health: state=healthy, last_success=now, consecutive_failures=0
  else:
    cached = LastGoodCache.get(job.id)
    if cached: Notifier.send_report(job, cached, stale=True)   # visible STALE banner
    Notifier.send_ops_alert(job, error)
    health: state=unhealthy, consecutive_failures += 1
  reschedule next_run; JobStore.persist()
```

Never silent; never session-dependent.

## Migration

On boot, if legacy `automations.json` exists, do **not** import its records
(they have no owned credential). Surface a one-time UI notice: *"N legacy
schedules need re-creation with a NetWorker credential."* The operator recreates
each job once in the new UI; the save gate validates it. After migration the old
`AlertAutomation`, its scheduler/arm code, and the session-snapshot path are
removed. Only `send_smtp_email` is retained.

Concretely for production: the two existing jobs (`daily_report` at 07:30 to 5
recipients; `alert` every 1440 min to 2 recipients) are recreated once with the
NetWorker `administrator` credential.

## Error handling

- Save gate returns structured per-check failures; the UI shows exactly which
  leg failed (bad credential vs NetWorker unreachable vs SMTP).
- Fire-time failures always produce either a fallback report (with a STALE
  banner) and/or an ops alert, plus a structured log line at each stage
  (INFO/WARNING, not gated behind a debug flag).
- `consecutive_failures` drives the UI health state and can gate escalation
  (e.g. louder ops alert after N failures).

## Testing

- **Unit:** CredentialStore round-trip (machine-DPAPI + Fernet fallback);
  Scheduler due-time calc; LastGoodCache put/get; JobValidator each check
  pass/fail; Notifier message building; stale-banner rendering.
- **Integration:** fake NetWorker + fake SMTP — save gate rejects a bad
  credential, an unreachable NetWorker, and a bad SMTP target; a successful fire
  sends the report and primes the cache; a failing fire sends the cached report
  with a STALE banner and an ops alert.
- **Regression (the exact original bug):** after a simulated restart with **zero
  in-memory sessions**, an enabled job **still fires and sends** — asserted. A
  job can never be saved as enabled with an empty/unusable credential.
- **E2E (SPA harness):** create a job → health shows healthy → next run is
  displayed; toggling enabled persists across a restart.

## File layout

- `nwdash/report_cred.py` — CredentialStore
- `nwdash/report_render.py` — ReportRenderer
- `nwdash/report_notify.py` — Notifier
- `nwdash/report_jobs.py` — ReportJob model, JobStore, JobValidator, Scheduler
- `nwdash/emailer.py` — shrinks to the SMTP core (`send_smtp_email` and helpers)
- assets — new Scheduled Reports panel (dashboard.html / app.js / app.css)

## Rollout

Ships as a new minor version with a service restart. Because credentials are now
job-owned and machine-DPAPI-encrypted, the subsystem is restart- and
account-change-proof from first save. Operator re-creates the two production jobs
once post-deploy.
