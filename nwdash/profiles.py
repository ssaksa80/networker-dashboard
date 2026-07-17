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
    PROFILES_FILE,
    REPORT_RANGES,
    _PROFILE_PW_SAVED,
    _PROFILE_PW_SENTINEL,
)
from .secrets import decrypt_profile_secret
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
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = PROFILES_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(profiles, separators=(",", ":")), encoding="utf-8")
        tmp.replace(PROFILES_FILE)
    except OSError:
        pass


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


def _resolve_profile_password(payload: dict[str, Any]) -> dict[str, Any]:
    """If password is sentinel, look up and decrypt from saved profile."""
    profile_name = str(payload.get("profileName") or "").strip()
    pw   = str(payload.get("password")    or "")
    wpw  = str(payload.get("wmiPassword") or "")
    if not profile_name:
        return payload
    if pw not in (_PROFILE_PW_SENTINEL, _PROFILE_PW_SAVED) and wpw not in (_PROFILE_PW_SENTINEL, _PROFILE_PW_SAVED):
        return payload
    with PROFILES_LOCK:
        profiles = load_profiles()
    prof = profiles.get(profile_name, {})
    result = dict(payload)
    if pw in (_PROFILE_PW_SENTINEL, _PROFILE_PW_SAVED):
        result["password"] = decrypt_profile_secret(prof.get("_enc_password", ""))
    if wpw in (_PROFILE_PW_SENTINEL, _PROFILE_PW_SAVED):
        result["wmiPassword"] = decrypt_profile_secret(prof.get("_enc_wmiPassword", ""))
    return result




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
