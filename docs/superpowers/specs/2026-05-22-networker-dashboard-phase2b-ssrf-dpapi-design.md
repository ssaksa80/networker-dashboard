# NetWorker Dashboard — Phase 2b: SSRF Allowlist + Windows DPAPI Key Protection

Date: 2026-05-22
Status: Approved
Target file: `networker_dashboard.py` (single-file by design)
Tests: `test_phase2b.py` (stdlib `unittest`)

## Goal

Close the two heavy items deferred from Phase 2:
- **H1 (SSRF):** the server connects to any user-supplied NetWorker host. Add an
  optional allowlist (hostnames / IPs / CIDRs) enforced when configured.
- **H2 (key-at-rest):** the Fernet master key and the auth HMAC key sit on disk
  in plaintext. On Windows, wrap them with DPAPI (machine scope). Keep current
  behavior off-Windows. Auto-migrate existing plaintext keys.

No new third-party dependencies (stdlib `ipaddress`, `ctypes`).

## Approved decisions

- SSRF: **allowlist enforced when configured, else warn** (backward-compatible).
- Match: **exact host + resolved-IP check** (defeats DNS rebinding).
- DPAPI: **wrap `.session_key` + `.auth_key` only**; plaintext fallback off
  Windows; auto-migrate legacy plaintext.
- DPAPI scope: **machine** (`CRYPTPROTECT_LOCAL_MACHINE`).
- Enforcement points for SSRF: **`validate_payload` + `restore_sessions_from_disk`**
  (accept the small validate-vs-connect TOCTOU residual; pinning shrinks it).

## Component 1 — SSRF allowlist

### Config
- New `--allowed-hosts` CLI flag and `DASHBOARD_ALLOWED_HOSTS` env (CLI wins).
  Comma-separated entries: hostnames, IP literals, or CIDRs
  (e.g. `nw1.corp.local,10.0.0.0/24,192.168.1.50`).
- Module globals: `ALLOWED_HOST_NAMES: set[str]` (lowercased hostname entries),
  `ALLOWED_NETWORKS: list[ipaddress._BaseNetwork]` (IP/CIDR entries),
  `ALLOWED_PINNED_IPS: set[str]` (IPs resolved from hostname entries at startup),
  `ALLOWLIST_ENABLED: bool`.

### Startup parse + pin
- `configure_allowed_hosts(raw: str) -> None`: clears + repopulates the globals.
  - Each entry: try `ipaddress.ip_network(entry, strict=False)` → add to
    `ALLOWED_NETWORKS`; on `ValueError` treat as hostname → add lowercased to
    `ALLOWED_HOST_NAMES`, then resolve it (`socket.getaddrinfo`) and add every
    resolved IP to `ALLOWED_PINNED_IPS` (best-effort; resolution failure logs a
    warning, entry still permitted by name only — see match rule).
  - `ALLOWLIST_ENABLED = bool(ALLOWED_HOST_NAMES or ALLOWED_NETWORKS)`.
- Called from `run()` with `args.allowed_hosts or os.environ[...]`.

### Match rule
`_host_allowed(host: str) -> bool`:
- If not `ALLOWLIST_ENABLED`: return `True` (open mode).
- Normalize `host` (strip, lowercase, strip brackets).
- Resolve `host` → `resolved_ips` via `socket.getaddrinfo` (both A/AAAA).
  Resolution failure → return `False` (cannot verify).
- If `host` is an IP literal: allowed iff that IP ∈ some `ALLOWED_NETWORKS`.
- Else (hostname): allowed iff `host in ALLOWED_HOST_NAMES` **AND** every IP in
  `resolved_ips` is in `ALLOWED_PINNED_IPS` ∪ `ALLOWED_NETWORKS`.
  - Rationale: operator lists only hostnames/CIDRs. A hostname that later
    rebinds to an IP outside its startup-pinned set (or allowed CIDRs) is
    rejected. If a hostname entry could not be resolved at startup
    (`ALLOWED_PINNED_IPS` empty for it), the resolved-IP test falls back to the
    CIDR set only — document that pure-name entries with no CIDR + failed
    startup resolution will be rejected at connect time (operator should add a
    CIDR or ensure DNS).

### Enforcement
- `_assert_host_allowed(config: ApiConfig) -> None`: for `rest_api_host` and (if
  different) `backup_server_host`, if not `_host_allowed(...)` raise
  `BadRequest(f"Host '{h}' is not in the configured allow-list.")`.
- Call in `validate_payload` after both hosts are parsed (returns 400 on the
  interactive connect path).
- Call in `restore_sessions_from_disk` inside the per-record `try` (a now-
  disallowed persisted session is skipped via the existing `continue`).
- When `ALLOWLIST_ENABLED` is False, `run()` prints a one-time warning that
  outbound NetWorker targets are unrestricted.

### Out of scope (documented)
- SMTP alert host (`smtplib`) and WMI/PowerShell target are separate outbound
  paths, not covered here.
- Validate-time vs connect-time TOCTOU: residual window remains; startup pinning
  reduces it. Connect-time socket interception is deferred.

## Component 2 — Windows DPAPI key protection

### Primitives
- `_dpapi_available() -> bool`: `sys.platform == "win32"`.
- `_dpapi_protect(data: bytes) -> bytes` and `_dpapi_unprotect(blob: bytes) ->
  bytes`: `ctypes` wrappers over `crypt32.CryptProtectData` /
  `CryptUnprotectData` using a `DATA_BLOB` struct, flag
  `CRYPTPROTECT_LOCAL_MACHINE = 0x4`. Free returned buffers with
  `kernel32.LocalFree`. Raise `OSError` on failure.

### On-disk format + helpers
- Marker constant `DPAPI_MARKER = b"DPAPI1\n"`.
- `_write_protected_key(path: Path, key: bytes) -> None`: on Windows, write
  `DPAPI_MARKER + _dpapi_protect(key)`; off-Windows (or on DPAPI failure) write
  raw `key`. `chmod(0o600)`. Use tmp + replace for atomicity.
- `_read_protected_key(path: Path) -> bytes | None`: read raw bytes; if it starts
  with `DPAPI_MARKER` → `_dpapi_unprotect(rest)`; else return the legacy bytes
  as-is. Return `None` on missing/error.

### Integration (auto-migrate)
- `_load_or_create_stable_key` (Fernet key):
  - Read via `_read_protected_key(SESSION_KEY_FILE)`. Legacy path may have a
    trailing newline → `.strip()` ONLY when the file was not DPAPI-wrapped.
    Validate with `Fernet(candidate)`.
  - If valid AND the on-disk file was legacy plaintext AND `_dpapi_available()`:
    rewrite via `_write_protected_key` (migration). Return the key.
  - If absent/invalid: generate, `_write_protected_key`, return.
- `_load_or_create_auth_key` (32-byte HMAC key): same pattern, length check
  `>= 32`, migrate legacy → wrapped on Windows.
- DPAPI failure anywhere → fall back to plaintext write + `debug_log`/stderr
  warning; never crash.

### Documented limit
Machine-scope DPAPI protects against offline disk theft / copying key files to
another machine. It does NOT protect against a local attacker already running
code (any process on the host can `CryptUnprotectData`).

## Testing (`test_phase2b.py`, stdlib unittest)

### SSRF (cross-platform)
- Monkeypatch `socket.getaddrinfo` to a fake resolver for deterministic IPs.
- `configure_allowed_hosts("")` → `ALLOWLIST_ENABLED False`; `_host_allowed`
  returns True for anything.
- CIDR entry `10.0.0.0/24`: target IP `10.0.0.5` allowed; `10.0.1.5` rejected.
- Hostname entry `nw1.local` resolving to a pinned IP allowed; same name
  resolving (rebinding) to an unpinned/out-of-CIDR IP rejected.
- IP literal not in any network rejected.
- `_assert_host_allowed` raises `BadRequest` for a blocked host;
  `validate_payload` with allowlist set + bad host raises.

### DPAPI (Windows-only, `unittest.skipUnless(sys.platform == "win32")`)
- `_dpapi_protect`/`_dpapi_unprotect` round-trip arbitrary bytes.
- `_read_protected_key` reads a `DPAPI_MARKER`-prefixed file written by
  `_write_protected_key`.
- Legacy plaintext key file (no marker) is read correctly and, after a load,
  rewritten with the marker (migration).
- Cross-platform: `_read_protected_key` on a non-marker file returns the raw
  bytes unchanged.

Manual smoke (Windows): delete `data/.session_key` + `data/.auth_key`, boot,
confirm both files start with `DPAPI1` bytes; restart, confirm sessions/cookies
still validate (key survived). With `--allowed-hosts 127.0.0.1`, a connect to a
non-listed host returns 400; with no allowlist, a startup warning prints.

## Out of scope (later)

SMTP/WMI host vetting, connect-time SSRF interception, structured logging,
per-session dashboard isolation.
