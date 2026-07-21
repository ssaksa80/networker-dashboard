"""Encryption at rest for a report job's NetWorker credential.

The password is sealed with machine-scoped DPAPI (already LOCAL_MACHINE in
secrets.py), so a service-account change on the same host does not invalidate
it. On non-Windows/dev hosts it falls back to the app's Fernet key. Tokens are
prefixed so the reader knows which scheme sealed them."""
from __future__ import annotations

import base64
from typing import Any

from .models import ApiConfig
from .secrets import _dpapi_available, _dpapi_protect, _dpapi_unprotect, WMI_CIPHER, decrypt_process_secret

_DPAPI_PREFIX = "dpapi:"
_FERNET_PREFIX = "fernet:"


def encrypt_credential_password(password: str) -> str:
    if not password:
        return ""
    raw = password.encode("utf-8")
    if _dpapi_available():
        return _DPAPI_PREFIX + base64.b64encode(_dpapi_protect(raw)).decode("ascii")
    if WMI_CIPHER:
        return _FERNET_PREFIX + WMI_CIPHER.encrypt(raw).decode("ascii")
    return ""


def decrypt_credential_password(token: str) -> str:
    if not token:
        return ""
    try:
        if token.startswith(_DPAPI_PREFIX):
            blob = base64.b64decode(token[len(_DPAPI_PREFIX):].encode("ascii"))
            return _dpapi_unprotect(blob).decode("utf-8")
        if token.startswith(_FERNET_PREFIX) and WMI_CIPHER:
            return WMI_CIPHER.decrypt(token[len(_FERNET_PREFIX):].encode("ascii")).decode("utf-8")
    except Exception:
        return ""
    return ""


def credential_to_apiconfig(cred: dict[str, Any]) -> ApiConfig:
    """Build a render-ready ApiConfig from a stored credential dict. The
    password is decrypted here and lives only on the transient ApiConfig."""
    return ApiConfig(
        rest_api_host=str(cred.get("rest_api_host") or ""),
        rest_api_port=int(cred.get("rest_api_port") or 0),
        backup_server_host=str(cred.get("backup_server_host") or cred.get("rest_api_host") or ""),
        backup_server_port=int(cred.get("backup_server_port") or cred.get("rest_api_port") or 0),
        username=str(cred.get("username") or ""),
        password=decrypt_credential_password(str(cred.get("encrypted_password") or "")),
        api_mode=str(cred.get("api_mode") or "nwui"),
        api_version=str(cred.get("api_version") or "auto"),
        report_range=str(cred.get("report_range") or "7d"),
        custom_start_date=str(cred.get("custom_start_date") or ""),
        custom_end_date=str(cred.get("custom_end_date") or ""),
        use_wmi_health=bool(cred.get("use_wmi_health") or False),
        wmi_username=str(cred.get("wmi_username") or ""),
        wmi_password="",
        timeout_seconds=int(cred.get("timeout_seconds") or 30),
        verify_tls=bool(cred.get("verify_tls") or False),
        use_authc_header=bool(cred.get("use_authc_header") or False),
    )


def apiconfig_from_stored(stored: dict[str, Any]) -> ApiConfig:
    """Build an ApiConfig from a stored reporting connection.

    Two shapes are supported:
    * session snapshot (has a "config" block) — every field is carried VERBATIM
      and the password is sealed with the process cipher. Nothing is guessed.
    * legacy flat dict — the older hand-entered shape, via credential_to_apiconfig.
    """
    if not isinstance(stored, dict):
        return credential_to_apiconfig({})
    cfg = stored.get("config")
    if not isinstance(cfg, dict):
        return credential_to_apiconfig(stored)
    return ApiConfig(
        rest_api_host=str(cfg.get("rest_api_host") or ""),
        rest_api_port=int(cfg.get("rest_api_port") or 0),
        backup_server_host=str(cfg.get("backup_server_host") or ""),
        backup_server_port=int(cfg.get("backup_server_port") or 0),
        username=str(cfg.get("username") or ""),
        password=decrypt_process_secret(str(stored.get("encrypted_networker_password") or "")),
        api_mode=str(cfg.get("api_mode") or "nwui"),
        api_version=str(cfg.get("api_version") or "auto"),
        report_range=str(cfg.get("report_range") or "24h"),
        custom_start_date=str(cfg.get("custom_start_date") or ""),
        custom_end_date=str(cfg.get("custom_end_date") or ""),
        use_wmi_health=bool(cfg.get("use_wmi_health") or False),
        wmi_username=str(cfg.get("wmi_username") or ""),
        wmi_password="",
        timeout_seconds=int(cfg.get("timeout_seconds") or 30),
        verify_tls=bool(cfg.get("verify_tls") or False),
        use_authc_header=bool(cfg.get("use_authc_header") or False),
    )
