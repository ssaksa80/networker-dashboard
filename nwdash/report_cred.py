"""Encryption at rest for a report job's NetWorker credential.

The password is sealed with machine-scoped DPAPI (already LOCAL_MACHINE in
secrets.py), so a service-account change on the same host does not invalidate
it. On non-Windows/dev hosts it falls back to the app's Fernet key. Tokens are
prefixed so the reader knows which scheme sealed them."""
from __future__ import annotations

import base64
from typing import Any

from .models import ApiConfig
from .secrets import _dpapi_available, _dpapi_protect, _dpapi_unprotect, WMI_CIPHER

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
