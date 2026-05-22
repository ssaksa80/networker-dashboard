# NetWorker Dashboard — Phase 2 (core) Authentication + Security Hardening

Date: 2026-05-22
Status: Approved
Target file: `networker_dashboard.py` (single-file by design)
Tests: `test_phase2.py` (stdlib `unittest`)

## Goal

Close the unauthenticated-access gap (C4) and adjacent issues. Add a shared
gateway password with signed-cookie sessions gating every data endpoint, keep
share links working as token-scoped capability URLs, and land a set of smaller
security fixes. Heavy items (SSRF allowlist, Windows DPAPI) are deferred to
Phase 2b.

## Background (defects this addresses)

- **C4** No authentication on data endpoints. `/api/current-dashboard`,
  `/api/stream`, `/api/profiles`, `/api/snapshots`, etc. serve full backup data
  to anyone who reaches the port. Share tokens are decorative because the
  read-only page pulls the global unauthenticated `/api/current-dashboard`.
- **H3** Catch-all `500` returns `str(exc)` to the client (info disclosure).
- **H2 (partial)** Startup prints "Passwords ... are not written to disk", which
  is false — encrypted credentials are persisted. Fix the message. (DPAPI
  key protection itself is Phase 2b.)
- Default bind `0.0.0.0` exposes the app to the whole network out of the box.

## Approved design decisions

- **Auth model:** one shared gateway password (separate from NetWorker creds) →
  HMAC-signed `HttpOnly`/`Secure`/`SameSite=Strict` cookie.
- **Secret source:** `DASHBOARD_AUTH_PASSWORD` env or `--auth-password`; only a
  salted hash is stored at rest.
- **Scope:** auth + cookies, bind default `127.0.0.1`, `500` redaction, fix the
  false startup line, login rate-limit. Defer SSRF allowlist + DPAPI to 2b.
- **Share links:** keep as capability URLs; add a token-scoped data endpoint so
  the read-only page no longer uses the global unauthenticated endpoint.
- **Exposed-without-password:** boot with a loud warning (do NOT refuse).

## Components

### 1. Auth secret key (stdlib-only)
- `AUTH_KEY_FILE = DATA_DIR / ".auth_key"`. On load: if present and >= 32 bytes,
  use it; else generate `os.urandom(32)`, write, `chmod(0o600)`.
- Stdlib only (no `cryptography`) so cookie signing works regardless of optional
  deps. Module global `AUTH_SECRET_KEY: bytes`.

### 2. Password hash at rest
- `AUTH_CONFIG_FILE = DATA_DIR / "auth.json"` = `{"salt": hex, "hash": hex,
  "iterations": int}`.
- `_hash_password(pw, salt, iterations)` -> `hashlib.pbkdf2_hmac("sha256",
  pw.encode(), salt, iterations)`. Default iterations = 200_000.
- `set_auth_password(pw)`: new random 16-byte salt, write config (chmod 600).
- `verify_auth_password(pw)`: read config; `hmac.compare_digest` of recomputed
  hash vs stored. Returns False if no config.
- `auth_password_configured()`: True if `AUTH_CONFIG_FILE` exists with a hash.

### 3. Startup wiring (in `run()`)
- Read password from `args.auth_password` or `os.environ["DASHBOARD_AUTH_PASSWORD"]`.
- If a password is supplied: call `set_auth_password` (creates/updates the hash).
- `AUTH_ENABLED = auth_password_configured()` (module global; True when a hash
  exists, whether just set or pre-existing).
- If `AUTH_ENABLED` is False AND bind is non-loopback (not in
  {"", "127.0.0.1", "localhost", "::1"} — `0.0.0.0`/`::`/external all count as
  exposed): print a loud multi-line WARNING that the dashboard is reachable on
  the network with no authentication and how to set a password. Still boot.

### 4. Cookie sign/verify
- `COOKIE_NAME = "nwdash_auth"`, `AUTH_TTL_SECONDS = 43200` (12h).
- `_make_auth_cookie()` -> payload = base64url(json `{"iat": now, "exp": now+TTL}`),
  sig = base64url(HMAC-SHA256(AUTH_SECRET_KEY, payload)); value = `f"{payload}.{sig}"`.
- `_verify_auth_cookie(value)` -> split on last ".", recompute sig,
  `hmac.compare_digest`, decode payload, check `exp > now`. Returns bool.
- `DashboardHandler._authenticated()` -> parse `Cookie` header (use
  `http.cookies.SimpleCookie`), read `COOKIE_NAME`, return
  `_verify_auth_cookie(value)`. If `AUTH_ENABLED` is False, returns True
  (open mode).

### 5. Endpoint gating
- **Always open:** `/api/login`, `/api/health`, `/favicon.ico`,
  `/networker-logo.png`.
- **Token-gated (no cookie):** `GET /view/<token>` (existing) and new
  `GET /api/view/<token>` — validates via `validate_share_token`; returns the
  token's session dashboard payload (reuse `shared_dashboard_payload` /
  `cached_reliable_dashboard_for_session` scoped to that session_id). Invalid /
  expired token -> 404 (api) or 410 (html, existing behavior).
- **Cookie-gated:** all other `do_GET`/`do_POST` routes. Enforced by a guard at
  the top of each dispatcher: after the always-open + token-gated routes are
  handled, if `AUTH_ENABLED and not self._authenticated()` -> respond 401
  (`_send_error_json` for `/api/*`, or redirect/login page for `/`).

### 6. Login UI (minimal template risk)
- New `login_page_html()` (small standalone page, modeled on
  `read_only_view_html`): password field, posts JSON to `/api/login`, on success
  `location.reload()`.
- `GET /` and `/index.html`: if `AUTH_ENABLED and not self._authenticated()` ->
  serve `login_page_html()`; else serve the existing `dashboard_html()`.
- The large `dashboard_html()` template gets ONE minimal addition: a global
  `fetch` wrapper (or a `window.addEventListener` on fetch responses) that, on
  any `401` from an `/api/*` call, calls `location.reload()` so an expired
  session returns to the login page. Keep this snippet tiny and self-contained.

### 7. Login endpoints
- `POST /api/login {password}`: rate-limit check (see 8); if
  `verify_auth_password(password)` -> set cookie via `Set-Cookie` header on a
  200 JSON `{ok: true}` and clear the IP's failure record; else record failure
  and return 401 `{ok: false}`.
- `POST /api/logout`: respond 200 and `Set-Cookie: nwdash_auth=; Max-Age=0`.

### 8. Login rate-limit
- `LOGIN_ATTEMPTS: dict[str, list[float]]` + `LOGIN_ATTEMPTS_LOCK`.
- `_login_rate_limited(ip)`: prune timestamps older than 300s; if >= 5 remain ->
  True. `_record_login_failure(ip)`: append now (under lock).
  `_clear_login_failures(ip)`: drop the IP on success.
- Limited request -> 429 `{ok: false, error: "Too many login attempts. Wait and
  try again."}`.

### 9. 500 redaction
- In both `do_GET` and `do_POST` catch-all `except Exception as exc`: generate
  `ref = uuid.uuid4().hex[:8]`; `debug_log` (or stderr) the full
  `safe_log_text(exc)` with the ref; return 500 `{ok: false, error: f"Internal
  error (ref {ref})."}`.

### 10. Startup message fix
- Replace the line `"Passwords are encrypted in process memory ... not written
  to disk."` with: `"Credentials are encrypted at rest in the data directory and
  are never stored in plaintext or placed in URLs."`

## Testing (`test_phase2.py`, stdlib unittest)

Use a temp data dir (monkeypatch `nd.DATA_DIR`, `nd.AUTH_KEY_FILE`,
`nd.AUTH_CONFIG_FILE`) in setUp/tearDown.

- Password set/verify: correct password verifies; wrong password fails; no config
  -> verify False.
- Cookie round-trip: `_verify_auth_cookie(_make_auth_cookie())` True.
- Tampered cookie rejected; expired cookie rejected (craft payload with past
  `exp`, sign it, expect False).
- Rate-limit: 5 failures -> `_login_rate_limited` True; after clear -> False.
- `auth_password_configured` reflects file presence.
- Exposed-without-password detection helper returns True for `0.0.0.0`, False for
  `127.0.0.1` (extract the loopback test into a small pure helper, e.g.
  `_is_loopback_bind(host)`, so it is unit-testable).

Manual smoke: with `--auth-password test --bind 127.0.0.1`, `GET /` returns the
login page; `POST /api/login` wrong -> 401; correct -> sets cookie; `GET
/api/current-dashboard` with the cookie -> 200, without -> 401; `--bind 0.0.0.0`
with no password prints the warning and still boots.

## Out of scope (Phase 2b)

SSRF host allowlist for NetWorker targets; Windows DPAPI protection of the
master/auth keys.

## Notes

`/api/health` stays open for load-balancer probes (it exposes only app name,
version, and time). Static favicon/logo stay open. Repo is local git only; spec
committed there.
