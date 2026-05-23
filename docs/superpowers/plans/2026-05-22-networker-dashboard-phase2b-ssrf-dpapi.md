# NetWorker Dashboard — Phase 2b: SSRF Allowlist + DPAPI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an optional SSRF allowlist (hostnames/IPs/CIDRs with rebinding-resistant resolved-IP checks) enforced on every connect-config path, and protect the on-disk `.session_key` + `.auth_key` with Windows DPAPI (machine scope, auto-migrating legacy plaintext).

**Architecture:** All in `networker_dashboard.py`. SSRF: module globals + `configure_allowed_hosts`/`_host_allowed`/`_assert_host_allowed`, enforced in `validate_payload` and `restore_sessions_from_disk`, configured in `run()`. DPAPI: stdlib `ctypes` wrappers + marker-prefixed key files + migration inside the two key loaders. Pure functions unit-tested in `test_phase2b.py`; DPAPI tests are Windows-only.

**Tech Stack:** Python 3 stdlib only — `ipaddress`, `ctypes`, `socket`. No new third-party deps.

---

### Task 1: SSRF allowlist core functions

**Files:**
- Modify: `networker_dashboard.py` (imports + new functions)
- Test: `test_phase2b.py` (create)

- [ ] **Step 1: Write the failing test**

Create `test_phase2b.py`:
```python
import sys
import unittest

import networker_dashboard as nd


class _FakeResolver:
    """Monkeypatch target for socket.getaddrinfo returning fixed IPs per host."""
    def __init__(self, mapping):
        self.mapping = mapping

    def __call__(self, host, *args, **kwargs):
        ips = self.mapping.get(host)
        if ips is None:
            import socket as _s
            raise _s.gaierror(f"no fake entry for {host}")
        # getaddrinfo returns 5-tuples; sockaddr is element [4], IP at [0]
        return [(None, None, None, "", (ip, 0)) for ip in ips]


class SsrfAllowlistTests(unittest.TestCase):
    def setUp(self):
        import socket
        self._orig_gai = socket.getaddrinfo
        nd.configure_allowed_hosts("")  # reset

    def tearDown(self):
        import socket
        socket.getaddrinfo = self._orig_gai
        nd.configure_allowed_hosts("")

    def _patch(self, mapping):
        import socket
        socket.getaddrinfo = _FakeResolver(mapping)

    def test_disabled_allows_anything(self):
        self.assertFalse(nd.ALLOWLIST_ENABLED)
        self.assertTrue(nd._host_allowed("anything.example.com"))

    def test_cidr_entry(self):
        self._patch({})
        nd.configure_allowed_hosts("10.0.0.0/24")
        self.assertTrue(nd.ALLOWLIST_ENABLED)
        self.assertTrue(nd._host_allowed("10.0.0.5"))
        self.assertFalse(nd._host_allowed("10.0.1.5"))

    def test_hostname_pinned_ip_allowed(self):
        self._patch({"nw1.local": ["10.0.0.5"]})
        nd.configure_allowed_hosts("nw1.local")
        self.assertTrue(nd._host_allowed("nw1.local"))

    def test_hostname_rebinding_rejected(self):
        # Pin at configure time to 10.0.0.5; later resolve to a different IP.
        self._patch({"nw1.local": ["10.0.0.5"]})
        nd.configure_allowed_hosts("nw1.local")
        self._patch({"nw1.local": ["66.66.66.66"]})  # rebinding
        self.assertFalse(nd._host_allowed("nw1.local"))

    def test_unlisted_hostname_rejected(self):
        self._patch({"evil.local": ["10.0.0.5"], "nw1.local": ["10.0.0.5"]})
        nd.configure_allowed_hosts("nw1.local")
        self.assertFalse(nd._host_allowed("evil.local"))

    def test_assert_raises_on_blocked(self):
        self._patch({"nw1.local": ["10.0.0.5"]})
        nd.configure_allowed_hosts("10.0.0.0/24")
        cfg = nd.ApiConfig(
            rest_api_host="9.9.9.9", rest_api_port=9090, backup_server_host="9.9.9.9",
            backup_server_port=9090, username="u", password="p", api_mode="nwui",
            api_version="auto", report_range="24h", custom_start_date="", custom_end_date="",
            use_wmi_health=False, wmi_username="", wmi_password="", timeout_seconds=30,
            verify_tls=False, use_authc_header=False,
        )
        with self.assertRaises(nd.BadRequest):
            nd._assert_host_allowed(cfg)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m unittest test_phase2b -v`
Expected: FAIL with `AttributeError` on `configure_allowed_hosts` / `ALLOWLIST_ENABLED`.

- [ ] **Step 3: Add imports**

Find:
```python
import hashlib
import hmac
import os
```
Replace with:
```python
import ctypes
import hashlib
import hmac
import ipaddress
import os
```

- [ ] **Step 4: Add SSRF allowlist functions**

Find:
```python
def parse_host(value: Any, field_name: str) -> tuple[str, int | None]:
```
Insert this block IMMEDIATELY ABOVE that line:
```python
ALLOWED_HOST_NAMES: set[str] = set()
ALLOWED_NETWORKS: list[Any] = []
ALLOWED_PINNED_IPS: set[str] = set()
ALLOWLIST_ENABLED = False


def _normalize_host(host: str) -> str:
    return (host or "").strip().lower().strip("[]")


def _resolve_ips(host: str) -> set[str]:
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, OSError, UnicodeError):
        return set()
    ips: set[str] = set()
    for info in infos:
        sockaddr = info[4]
        if sockaddr and sockaddr[0]:
            ips.add(sockaddr[0])
    return ips


def configure_allowed_hosts(raw: str) -> None:
    """Parse a comma-separated allowlist of hostnames / IPs / CIDRs.
    Hostname entries are resolved once here and their IPs pinned (rebinding guard).
    """
    global ALLOWLIST_ENABLED
    ALLOWED_HOST_NAMES.clear()
    ALLOWED_NETWORKS.clear()
    ALLOWED_PINNED_IPS.clear()
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            ALLOWED_NETWORKS.append(ipaddress.ip_network(entry, strict=False))
            continue
        except ValueError:
            pass
        name = _normalize_host(entry)
        if name:
            ALLOWED_HOST_NAMES.add(name)
            for ip in _resolve_ips(name):
                ALLOWED_PINNED_IPS.add(ip)
    ALLOWLIST_ENABLED = bool(ALLOWED_HOST_NAMES or ALLOWED_NETWORKS)


def _ip_in_networks(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in ALLOWED_NETWORKS)


def _host_allowed(host: str) -> bool:
    if not ALLOWLIST_ENABLED:
        return True
    h = _normalize_host(host)
    if not h:
        return False
    try:
        literal_ip = ipaddress.ip_address(h)
    except ValueError:
        literal_ip = None
    if literal_ip is not None:
        return _ip_in_networks(h)
    if h not in ALLOWED_HOST_NAMES:
        return False
    resolved = _resolve_ips(h)
    if not resolved:
        return False
    return all(ip in ALLOWED_PINNED_IPS or _ip_in_networks(ip) for ip in resolved)


def _assert_host_allowed(config: "ApiConfig") -> None:
    for host in {config.rest_api_host, config.backup_server_host or config.rest_api_host}:
        if host and not _host_allowed(host):
            raise BadRequest(f"Host '{host}' is not in the configured allow-list.")
```

- [ ] **Step 5: Run + parse**

Run: `python -m unittest test_phase2b -v` → all PASS.
Run: `python -c "import ast; ast.parse(open('networker_dashboard.py',encoding='utf-8').read()); print('parse ok')"`

- [ ] **Step 6: Commit**
```bash
git add networker_dashboard.py test_phase2b.py
git commit -m "feat: SSRF allowlist core (host/IP/CIDR, rebinding-resistant) (H1)"
```
(Use `git -c user.name="dev" -c user.email="dev@local" commit ...` if needed.)

---

### Task 2: Wire SSRF into validate_payload, restore, run()

**Files:**
- Modify: `networker_dashboard.py` (`validate_payload`, `restore_sessions_from_disk`, `parse_args`, `run`)

- [ ] **Step 1: Enforce in validate_payload**

In `validate_payload`, find:
```python
    backup_server_port = parse_port(
        payload.get("backupServerPort") or embedded_backup_port,
        DEFAULT_API_PORT,
        "AuthC port",
    )

    username = str(payload.get("username") or "").strip()
```
Replace with:
```python
    backup_server_port = parse_port(
        payload.get("backupServerPort") or embedded_backup_port,
        DEFAULT_API_PORT,
        "AuthC port",
    )

    if ALLOWLIST_ENABLED:
        for host in {rest_api_host, backup_server_host}:
            if host and not _host_allowed(host):
                raise BadRequest(f"Host '{host}' is not in the configured allow-list.")

    username = str(payload.get("username") or "").strip()
```

- [ ] **Step 2: Enforce in restore_sessions_from_disk**

In `restore_sessions_from_disk`, find:
```python
            if not config.rest_api_host or not config.username:
                continue
```
Replace with:
```python
            if not config.rest_api_host or not config.username:
                continue
            if ALLOWLIST_ENABLED and not (
                _host_allowed(config.rest_api_host)
                and _host_allowed(config.backup_server_host or config.rest_api_host)
            ):
                continue
```

- [ ] **Step 3: Add the `--allowed-hosts` CLI flag**

In `parse_args`, find:
```python
    parser.add_argument(
        "--auth-password",
        default="",
        help="Dashboard access password. May also be supplied via the DASHBOARD_AUTH_PASSWORD environment variable. Stored only as a salted hash.",
    )
```
Insert IMMEDIATELY BELOW it:
```python
    parser.add_argument(
        "--allowed-hosts",
        default="",
        help="Comma-separated allow-list of NetWorker hosts/IPs/CIDRs the server may connect to (e.g. nw1.corp.local,10.0.0.0/24). May also be set via DASHBOARD_ALLOWED_HOSTS. If unset, any host is permitted.",
    )
```

- [ ] **Step 4: Configure + warn in run()**

In `run`, find:
```python
    auth_password = args.auth_password or os.environ.get("DASHBOARD_AUTH_PASSWORD") or ""
    if auth_password:
        set_auth_password(auth_password)
    AUTH_ENABLED = auth_password_configured()
```
Replace with:
```python
    auth_password = args.auth_password or os.environ.get("DASHBOARD_AUTH_PASSWORD") or ""
    if auth_password:
        set_auth_password(auth_password)
    AUTH_ENABLED = auth_password_configured()
    configure_allowed_hosts(args.allowed_hosts or os.environ.get("DASHBOARD_ALLOWED_HOSTS") or "")
```

Then find:
```python
    print("Credentials are encrypted at rest in the data directory and are never stored in plaintext or placed in URLs.")
```
Insert IMMEDIATELY BELOW it:
```python
    if ALLOWLIST_ENABLED:
        print(f"NetWorker host allow-list ENABLED ({len(ALLOWED_HOST_NAMES) + len(ALLOWED_NETWORKS)} entr(y/ies)).")
    else:
        print("NetWorker host allow-list is disabled (the server may connect to any host you enter). Set --allowed-hosts to restrict.")
```

- [ ] **Step 5: Parse + tests + help**

Run: `python -c "import ast; ast.parse(open('networker_dashboard.py',encoding='utf-8').read()); print('parse ok')"`
Run: `python -m unittest test_phase1 test_phase2 test_phase2b -v` → all PASS.
Run: `python networker_dashboard.py --help` → shows `--allowed-hosts`.

- [ ] **Step 6: Commit**
```bash
git add networker_dashboard.py
git commit -m "feat: enforce SSRF allowlist in validate/restore + CLI/env wiring (H1)"
```

---

### Task 3: DPAPI primitives + key file helpers

**Files:**
- Modify: `networker_dashboard.py` (new functions placed ABOVE `_load_or_create_stable_key`)
- Test: `test_phase2b.py`

- [ ] **Step 1: Write the failing test**

Append to `test_phase2b.py`:
```python
class DpapiHelperTests(unittest.TestCase):
    def test_read_plaintext_passthrough(self):
        import tempfile, os
        from pathlib import Path
        d = Path(tempfile.mkdtemp())
        p = d / ".somekey"
        p.write_bytes(b"raw-legacy-key-bytes")
        self.assertEqual(nd._read_protected_key(p), b"raw-legacy-key-bytes")
        self.assertFalse(nd._key_file_is_wrapped(p))

    def test_read_missing_returns_none(self):
        from pathlib import Path
        import tempfile
        self.assertIsNone(nd._read_protected_key(Path(tempfile.mkdtemp()) / "nope"))


@unittest.skipUnless(sys.platform == "win32", "DPAPI is Windows-only")
class DpapiWindowsTests(unittest.TestCase):
    def test_protect_unprotect_roundtrip(self):
        data = b"\x00\x01secret-key-bytes\xff\x0a"
        blob = nd._dpapi_protect(data)
        self.assertNotEqual(blob, data)
        self.assertEqual(nd._dpapi_unprotect(blob), data)

    def test_write_then_read_wrapped(self):
        import tempfile
        from pathlib import Path
        d = Path(tempfile.mkdtemp())
        p = d / ".wrapkey"
        nd._write_protected_key(p, b"the-key")
        self.assertTrue(p.read_bytes().startswith(nd.DPAPI_MARKER))
        self.assertTrue(nd._key_file_is_wrapped(p))
        self.assertEqual(nd._read_protected_key(p), b"the-key")
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m unittest test_phase2b.DpapiHelperTests -v`
Expected: FAIL with `AttributeError` on `_read_protected_key` / `DPAPI_MARKER`.

- [ ] **Step 3: Add DPAPI primitives + key helpers**

Find:
```python
def _load_or_create_stable_key() -> bytes:
```
Insert this block IMMEDIATELY ABOVE that line:
```python
DPAPI_MARKER = b"DPAPI1\n"
_CRYPTPROTECT_LOCAL_MACHINE = 0x4


def _dpapi_available() -> bool:
    return sys.platform == "win32"


if sys.platform == "win32":
    class _DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_uint), ("pbData", ctypes.c_void_p)]


def _dpapi_protect(data: bytes) -> bytes:
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.c_void_p))
    blob_out = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None,
        _CRYPTPROTECT_LOCAL_MACHINE, ctypes.byref(blob_out),
    )
    if not ok:
        raise OSError("CryptProtectData failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _dpapi_unprotect(blob: bytes) -> bytes:
    buf = ctypes.create_string_buffer(blob, len(blob))
    blob_in = _DATA_BLOB(len(blob), ctypes.cast(buf, ctypes.c_void_p))
    blob_out = _DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None,
        _CRYPTPROTECT_LOCAL_MACHINE, ctypes.byref(blob_out),
    )
    if not ok:
        raise OSError("CryptUnprotectData failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _key_file_is_wrapped(path: Path) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(len(DPAPI_MARKER)) == DPAPI_MARKER
    except OSError:
        return False


def _write_protected_key(path: Path, key: bytes) -> None:
    payload = key
    if _dpapi_available():
        try:
            payload = DPAPI_MARKER + _dpapi_protect(key)
        except OSError as exc:
            sys.stderr.write(f"DPAPI protect failed; storing key unwrapped: {exc}\n")
            payload = key
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / (path.name + ".tmp")
        tmp.write_bytes(payload)
        tmp.replace(path)
        path.chmod(0o600)
    except OSError:
        pass


def _read_protected_key(path: Path) -> bytes | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if raw.startswith(DPAPI_MARKER):
        try:
            return _dpapi_unprotect(raw[len(DPAPI_MARKER):])
        except OSError as exc:
            sys.stderr.write(f"DPAPI unprotect failed for {path.name}: {exc}\n")
            return None
    return raw
```

- [ ] **Step 4: Run + parse**

Run: `python -m unittest test_phase2b -v` → all PASS (DpapiWindowsTests skip off-Windows).
Run: `python -c "import ast; ast.parse(open('networker_dashboard.py',encoding='utf-8').read()); print('parse ok')"`

- [ ] **Step 5: Commit**
```bash
git add networker_dashboard.py test_phase2b.py
git commit -m "feat: DPAPI protect/unprotect + marker key-file helpers (H2)"
```

---

### Task 4: Integrate DPAPI into key loaders (with migration)

**Files:**
- Modify: `networker_dashboard.py` (`_load_or_create_stable_key`, `_load_or_create_auth_key`)

- [ ] **Step 1: Replace `_load_or_create_stable_key`**

Replace the ENTIRE current function with:
```python
def _load_or_create_stable_key() -> bytes:
    """Load persisted Fernet key (DPAPI-wrapped on Windows); create if absent.
    Legacy plaintext keys are migrated to wrapped form on first read.
    """
    if Fernet is None:
        return b""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    raw = _read_protected_key(SESSION_KEY_FILE)
    if raw is not None:
        candidate = raw.strip()
        try:
            Fernet(candidate)  # raises if malformed
            if _dpapi_available() and not _key_file_is_wrapped(SESSION_KEY_FILE):
                _write_protected_key(SESSION_KEY_FILE, candidate)  # migrate
            return candidate
        except Exception:
            pass
    key = Fernet.generate_key()
    _write_protected_key(SESSION_KEY_FILE, key)
    return key
```

- [ ] **Step 2: Replace `_load_or_create_auth_key`**

Replace the ENTIRE current function with:
```python
def _load_or_create_auth_key() -> bytes:
    """Stable 32-byte HMAC key for signing auth cookies (DPAPI-wrapped on Windows).
    Legacy plaintext keys are migrated to wrapped form on first read.
    """
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    raw = _read_protected_key(AUTH_KEY_FILE)
    if raw is not None and len(raw) >= 32:
        if _dpapi_available() and not _key_file_is_wrapped(AUTH_KEY_FILE):
            _write_protected_key(AUTH_KEY_FILE, raw)  # migrate
        return raw
    key = os.urandom(32)
    _write_protected_key(AUTH_KEY_FILE, key)
    return key
```

- [ ] **Step 3: Parse + tests + import sanity**

Run: `python -c "import ast; ast.parse(open('networker_dashboard.py',encoding='utf-8').read()); print('parse ok')"`
Run: `python -m unittest test_phase1 test_phase2 test_phase2b -v` → all PASS.
Run: `python -c "import networker_dashboard as nd; print('keys', len(nd.AUTH_SECRET_KEY) >= 32, bool(nd.WMI_CREDENTIAL_KEY) or nd.Fernet is None)"`
Expected: `keys True True`.

- [ ] **Step 4: Commit**
```bash
git add networker_dashboard.py
git commit -m "feat: wrap session/auth keys with DPAPI + migrate legacy plaintext (H2)"
```

---

### Task 5: Full regression + smoke

**Files:** none (verification only)

- [ ] **Step 1: Full unit suite**
Run: `python -m unittest test_phase1 test_phase2 test_phase2b -v` → all PASS.

- [ ] **Step 2: SSRF live smoke**

Boot (background): `python networker_dashboard.py --no-launch --port 18445 --bind 127.0.0.1 --allowed-hosts 10.0.0.0/24`
Then (substitute port if needed) confirm a connect to a non-listed host is rejected with 400:
```python
import ssl, json, urllib.request, urllib.error, time
ctx = ssl._create_unverified_context()
def post(path, data):
    op = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
    req = urllib.request.Request("https://localhost:18445"+path, data=json.dumps(data).encode(), method="POST")
    req.add_header("Content-Type","application/json")
    try:
        return op.open(req, timeout=5).status
    except urllib.error.HTTPError as e:
        return e.code
# wait for boot
for _ in range(40):
    try:
        urllib.request.urlopen("https://localhost:18445/api/health", context=ctx, timeout=2); break
    except Exception: time.sleep(0.5)
code = post("/api/dashboard", {"restApiHost":"8.8.8.8","username":"u","password":"p"})
print("blocked_host_status", code, "(expect 400)")
assert code == 400
print("SSRF BLOCK OK")
```
Stop the server afterward (PowerShell: `Get-NetTCPConnection -LocalPort 18445 -State Listen | %{ Stop-Process -Id $_.OwningProcess -Force }`).

- [ ] **Step 3: DPAPI key smoke (Windows)**

Delete `data/.session_key` and `data/.auth_key` (back them up first if you want to keep existing sessions). Boot once, stop, then:
```bash
python -c "from pathlib import Path; m=b'DPAPI1'; import sys; d=Path('data'); print('session_wrapped', d.joinpath('.session_key').read_bytes().startswith(m) if d.joinpath('.session_key').exists() else 'n/a'); print('auth_wrapped', d.joinpath('.auth_key').read_bytes().startswith(m) if d.joinpath('.auth_key').exists() else 'n/a')"
```
Expected on Windows: both `True`. Boot again and confirm it still starts (key unwrapped successfully → no regeneration). On non-Windows this step is N/A (keys stay raw).

- [ ] **Step 4: Final commit (optional, empty)**
```bash
git commit --allow-empty -m "test: phase 2b regression green"
```

---

## Self-Review

**Spec coverage:**
- SSRF config (CLI/env, parse, pin) → Task 1 (`configure_allowed_hosts`) + Task 2 (wiring). ✔
- SSRF match rule (literal IP vs hostname + pinned/CIDR resolved-IP check) → Task 1 (`_host_allowed`). ✔
- SSRF enforcement (validate_payload + restore) → Task 2. ✔
- SSRF disabled → warn → Task 2 Step 4. ✔
- DPAPI primitives (machine scope) → Task 3. ✔
- Marker format + read/write/is_wrapped → Task 3. ✔
- Key loader integration + legacy migration → Task 4. ✔
- DPAPI failure fallback to plaintext → Task 3 (`_write_protected_key`/`_read_protected_key`). ✔
- Tests (SSRF cross-platform, DPAPI Windows-only skip) → Tasks 1, 3, 5. ✔

**Placeholder scan:** none — all steps contain full code and exact commands.

**Type/name consistency:** `ALLOWED_HOST_NAMES`, `ALLOWED_NETWORKS`, `ALLOWED_PINNED_IPS`, `ALLOWLIST_ENABLED`, `configure_allowed_hosts`, `_host_allowed`, `_assert_host_allowed`, `_normalize_host`, `_resolve_ips`, `_ip_in_networks`, `DPAPI_MARKER`, `_dpapi_available`, `_dpapi_protect`, `_dpapi_unprotect`, `_key_file_is_wrapped`, `_write_protected_key`, `_read_protected_key` are used consistently across tasks. The DPAPI/key helpers are defined ABOVE `_load_or_create_stable_key` (Task 3) so they exist when the key loaders call them at import time (Task 4).

**Known limitations (documented):**
- Validate-time vs connect-time TOCTOU window remains (pinning reduces it).
- SMTP alert host and WMI target are not allowlist-checked (separate paths).
- Machine-scope DPAPI does not defend against a local attacker already executing code on the host; it defends against offline disk/file theft.
- `_assert_host_allowed` exists for reuse/tests; `validate_payload` uses an inline equivalent check to keep the early-validation error message close to the field parsing. Both raise `BadRequest` with the same message text.
