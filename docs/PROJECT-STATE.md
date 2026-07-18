# Project State

Snapshot of the hardening effort completed on 2026-07-18. Read this
before making changes — it records why things are shaped the way they
are and what was deliberately deferred.

## History (one session, 7 commits)

| Commit | Change |
|---|---|
| `9cd6f40` | Initial import; `.gitignore` fences `data/`, `logs/`, `.certs/`, `debug_test/` (live keys and session state — never commit) |
| `87dab91` | CSRF protection + fail-closed TLS verification |
| `7d0b028` | Monolith split into the `nwdash/` package; 27-test smoke suite |
| `73630ed` | UI extracted from Python string literals to `nwdash/assets/` |
| `55003e9` | README, pyproject, GitHub Actions CI |
| `29ba8a5` | Swallowed exceptions logged; DPAPI key-regeneration warning |

Result: overall report-card grade moved from B- to A-. All rendered
pages were verified byte-identical across the split and the asset
extraction (SHA-256 against the prior revision).

## Architecture rules

- `networker_dashboard.py` is a thin launcher; all code lives in
  `nwdash/`. Keep it that way.
- **Mutable runtime flags** (`AUTH_ENABLED`, `APP_DEBUG`,
  `MAX_CONNECTIONS`, `ALLOWLIST_ENABLED`, …) are owned by
  `nwdash/config.py` and are mutated by `run()` after argument parsing.
  Always access them as module attributes (`from . import config as
  _cfg; _cfg.AUTH_ENABLED`), never `from .config import AUTH_ENABLED` —
  a from-import freezes the value at import time and silently ignores
  runtime changes.
- **UI pages are real files** in `nwdash/assets/` (`dashboard.html` +
  `app.css` + `app.js`, `login.html`, `view.html`), reassembled at
  import via marker replacement (`__APP_CSS__`, `__APP_JS__`,
  `__SHARE_TOKEN__`, `__NETWORKER_LOGO_SRC__`). Edit the assets, not
  Python strings. `.gitattributes` pins them to LF; the loader uses
  universal-newline decoding so served bytes are checkout-invariant.
- Six deliberate function-level imports break circular module edges;
  each is commented at the import site. Don't "clean them up" into
  top-level imports.

## Security model

- Login: PBKDF2-SHA256 password hash, rate-limited, HMAC-signed
  HttpOnly session cookie (`SameSite=Strict; Secure`).
- CSRF: stateless synchronizer token = HMAC(auth key, session-cookie
  payload). Issued in the login response and via `GET /api/csrf`; the
  fetch wrapper in `app.js` injects `X-CSRF-Token` on every non-GET
  `/api` call and re-bootstraps once on 403. `do_POST` enforces it for
  all state-changing routes; `/api/login` and `/api/logout` are exempt.
- Outbound TLS to NetWorker servers verifies certificates by default,
  controlled per connection profile; restored sessions missing the
  flag default to verify (fail closed). The startup self-test pins the
  server's own certificate instead of disabling verification.

## Verification

```
python -m unittest discover -s tests -v   # 27 tests, must pass
ruff check .                              # must be clean
```

CI (`.github/workflows/tests.yml`) runs both on windows-latest /
Python 3.14 for every push and PR. Run the suite locally before any
release claim — `py_compile` or a clean boot is not evidence.

## Operational gotchas

- `data/.session_key` and `data/.auth_key` are DPAPI-protected per
  Windows account. Copies to another machine/account are unreadable;
  the app warns at startup and regenerates, which invalidates saved
  sessions and profile passwords (re-enter once).
- The dev TLS certificate in `.certs/` is self-signed; browsers warn
  once. Provide `--cert`/`--key` for production.
- Headless-browser screenshot rendering (email reports) needs a local
  Chrome or Edge; on some managed hosts Edge silently refuses
  `--screenshot` while Chrome works.

## Deferred work (in priority order)

1. **Split the large collector functions** — `build_dashboard_nwui`
   (~349 lines), `handle_alert_automation`, `dashboard_report_email`.
   Deliberately not attempted: they are only exercisable against a
   real NetWorker server, and refactoring them blind risks silent
   breakage behind fail-soft guards. Do this when a test server is
   reachable, with live-data validation before and after.
2. Live-data validation of REST/NWUI/WMI collectors (same dependency).
3. Nice-to-haves: release tagging + offline bundle, CSP tightening
   pass, ubuntu CI leg, narrowing the ~23 broad `except Exception`
   sites.
