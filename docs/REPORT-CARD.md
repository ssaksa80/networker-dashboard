# Report Card — Hardening Effort (2026-07-18)

Assessment of the codebase before and after the hardening effort.
Companion to [PROJECT-STATE.md](PROJECT-STATE.md), which records the
architecture rules and deferred work in detail.

**Repo:** https://github.com/ssaksa80/networker-dashboard —
`main` (hardened line) + `legacy/v2.5.0-history` (original 120-commit
development history) · CI green.

## Grades

| Category | Before | After | What changed |
|---|---|---|---|
| Correctness | A | **A** | Was already clean; now proven by tests + CI instead of asserted |
| Security | B+ | **A-** | CSRF added, TLS fail-closed, secrets fenced |
| Code quality | B | **A-** | Silent failures logged, lint-clean, error chaining |
| Architecture | C+ | **A-** | 16-module package, real asset files |
| Maintainability | C | **A-** | 27 tests, CI, README, state doc |
| Ops hygiene | B- | **A** | Secrets gitignored, DPAPI warning, history archived |
| **Overall** | **B-** | **A-** | |

## Fixed, item by item

### Security — CSRF (was: zero protection on 21 POST routes) — `87dab91`
- Stateless token = HMAC(auth key, session-cookie payload) — no server
  storage, rotates per login
- Issued in the login response and via `GET /api/csrf`; the fetch
  wrapper injects `X-CSRF-Token` on every non-GET `/api` call and
  auto-recovers once on 403
- `do_POST` rejects tokenless state changes; proven live:
  no-token 403, bad-token 403, good-token 200

### Security — TLS fail-open leftovers (3) — `87dab91`
- Restored sessions missing `verify_tls` defaulted **false** → now **true**
- Add-Server dialog hardcoded `verifyTls: false` → user checkbox,
  default checked
- Startup self-test used an unverified context → pins the server's own
  certificate as trust anchor

### Secrets exposure (was: live keys sitting next to code) — `9cd6f40`
- `data/` (auth/session keys, sessions), `.certs/` (TLS private key),
  `logs/`, `debug_test/` gitignored before the first push — verified
  only two files were ever staged

### Architecture (was: 770 KB / 13.6k-line single file) — `7d0b028`, `73630ed`
- 16-module `nwdash/` package; a thin launcher keeps
  `python networker_dashboard.py` unchanged
- Runtime-mutated flags are read as config-module attributes
  (a from-import would freeze them at import time)
- UI extracted to real files (`app.js` 114 KB, `app.css` 47 KB, three
  HTML pages) — every step verified byte-identical via SHA-256 of all
  three rendered pages against the prior revision

### Testing (was: zero tests) — `7d0b028`
- 27 stdlib unittest tests: module imports, auth/cookie/CSRF primitives
  including tamper and expiry cases, TLS fail-closed defaults, page
  wiring, and a subprocess end-to-end HTTPS test that drives the real
  entry script through the login/CSRF matrix

### CI / docs / metadata (was: none) — `55003e9`, `fa9a3b9`
- GitHub Actions: ruff + full suite on windows-latest / Python 3.14 for
  every push and PR
- README (quick start, flags, layout), pyproject,
  `docs/PROJECT-STATE.md` (architecture rules + deferred list)

### Code quality — `29ba8a5`
- Six silent `except: pass` sites now debug-logged; the port-fallback
  bind error is exception-chained (B904); ruff clean

### Operations — `29ba8a5` + housekeeping
- A DPAPI key copied across accounts/machines now produces a loud
  startup warning ("re-enter sessions") instead of silent key
  regeneration — proven with a forged key file
- Original 120-commit history preserved as `legacy/v2.5.0-history`;
  the hardened line's initial import was verified byte-identical to
  that branch's tip, so no original work was lost

## Not fixed (deliberate — requires a live NetWorker server)

- Splitting the 300+ line collector functions
  (`build_dashboard_nwui` ~349 lines) — refactoring them blind risks
  silent breakage behind fail-soft guards
- Live-data validation of the REST/NWUI/WMI collectors
- Minor: ~23 broad `except Exception` sites (deliberate fail-soft),
  CSP tightening, release tagging, an ubuntu CI leg
