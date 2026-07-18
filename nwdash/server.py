"""HTTP request handler, HTTPS server, and listener self-test helpers.

Split from networker_dashboard.py (v2.5.0); behavior unchanged.
"""
from __future__ import annotations

import json
import re
import socket
import ssl
import sys
import threading
import time
import uuid
import webbrowser
from http.cookies import SimpleCookie
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlparse
from urllib.request import Request, urlopen

from . import config as _cfg
from .config import (
    APP_NAME,
    APP_VERSION,
    AUTH_TTL_SECONDS,
    COOKIE_NAME,
    DEFAULT_MAX_CONNECTIONS,
    DEFAULT_PORT,
    DEFAULT_REPORT_RANGE,
    LOG,
    MAX_POST_BYTES,
    PROCESS_START_TIME,
    _PROFILE_PW_SAVED,
    _PROFILE_PW_SENTINEL,
    debug_log,
    safe_log_text,
)
from .secrets import encrypt_profile_secret
from .auth import (
    _clear_login_failures,
    _login_rate_limited,
    _make_auth_cookie,
    _make_csrf_token,
    _record_login_failure,
    _verify_auth_cookie,
    _verify_csrf_token,
    verify_auth_password,
)
from .ui import FAVICON_SVG, NETWORKER_LOGO_PATH, dashboard_html, login_page_html, read_only_view_html
from .models import (
    BadRequest,
    SHARED_DASHBOARD_LOCK,
    SHARED_DASHBOARD_STATE,
    SHARED_REFRESH_STOP,
    SSE_CLIENTS,
    SSE_CLIENTS_LOCK,
    _automation_keys_snapshot,
    _session_exists,
    _session_ids_snapshot,
    _sse_register,
    cached_reliable_dashboard_for_session,
    create_share_token,
    json_clone,
    revoke_share_token,
    set_shared_dashboard,
    shared_dashboard_payload,
    validate_share_token,
)
from .profiles import (
    PROFILES_LOCK,
    SNAPSHOTS_LOCK,
    _mask_profiles,
    _resolve_profile_password,
    load_profiles,
    save_profiles,
    validate_payload,
)
from .sessions import (
    build_dashboard,
    build_dashboard_from_session,
    build_multi_server_dashboard,
    build_server_health_from_session,
)
from .snapshots import (
    _auto_snapshot_once,
    annotate_snapshot,
    compare_dashboard_snapshots,
    delete_snapshot_by_date,
    email_config_public,
    list_snapshot_summary,
    load_auto_snapshot_config,
    load_ui_theme,
    save_auto_snapshot_config,
    save_dashboard_snapshot,
    save_ui_theme,
    snapshot_history_all,
    snapshot_summary_text,
    snapshots_to_csv,
)
from .reports import build_excel_report
from .emailer import handle_alert_automation

class DashboardHandler(BaseHTTPRequestHandler):
    server_version = f"NetWorkerDashboard/{APP_VERSION}"
    protocol_version = "HTTP/1.1"
    timeout = _cfg.REQUEST_TIMEOUT_SECONDS
    request_id = "-"

    def log_message(self, format: str, *args: Any) -> None:
        LOG.info(
            format % args,
            extra={"request_id": getattr(self, "request_id", "-"), "client": self.client_address[0]},
        )

    def log_dashboard_failure(self, status: int, body: dict[str, Any]) -> None:
        target = body.get("target") if isinstance(body.get("target"), dict) else {}
        sources = body.get("sources") if isinstance(body.get("sources"), dict) else {}
        rest_base = safe_log_text(target.get("restApiBase", "unknown"))
        api_mode = safe_log_text(target.get("apiMode", "rest"))
        authc_header = "enabled" if target.get("authcHeaderEnabled") else "disabled"
        rid = getattr(self, "request_id", "-")
        LOG.warning(
            f"NetWorker dashboard upstream failure: apiMode={api_mode} apiBase={rest_base} authcHeader={authc_header}",
            extra={"request_id": rid, "status": status},
        )
        for name, item in sources.items():
            if isinstance(item, dict) and not item.get("ok"):
                path = safe_log_text(item.get("path", name))
                error = safe_log_text(item.get("error", "failed"))
                upstream_status = item.get("status", "n/a")
                LOG.warning(
                    f"  source={safe_log_text(name)} upstreamStatus={upstream_status} path={path} error={error}",
                    extra={"request_id": rid, "status": status},
                )

    def _is_https(self) -> bool:
        return isinstance(self.request, ssl.SSLSocket)

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; form-action 'self'; frame-ancestors 'none'",
        )
        if extra_headers:
            for name, value in extra_headers.items():
                self.send_header(name, value)
        try:
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        except OSError as exc:
            # Client disconnected mid-response (TLS EOF / reset / broken pipe).
            # ssl.SSLError is an OSError subclass, so this covers SSLEOFError too.
            # Nothing to recover; abort this connection quietly instead of letting
            # the failure bubble into a second (also-failing) error write.
            self.close_connection = True
            LOG.debug(
                f"client disconnected during response: {exc}",
                extra={"request_id": getattr(self, "request_id", "-"), "client": self.client_address[0]},
            )

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _send_error_json(self, status: int, message: str) -> None:
        self._send_json(status, {"ok": False, "error": message})

    def _read_json_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError as exc:
            raise BadRequest("Invalid Content-Length.") from exc
        if length <= 0 or length > MAX_POST_BYTES:
            raise BadRequest("Request body size is invalid.")
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise BadRequest("JSON body must be an object.")
        return payload

    def _require_https(self) -> bool:
        if self._is_https():
            return True
        self._send_error_json(HTTPStatus.FORBIDDEN, "HTTPS is required.")
        return False

    def _session_cookie_value(self) -> str:
        raw = self.headers.get("Cookie")
        if not raw:
            return ""
        try:
            jar = SimpleCookie()
            jar.load(raw)
        except Exception:  # noqa: BLE001 — malformed cookie header
            return ""
        morsel = jar.get(COOKIE_NAME)
        return morsel.value if morsel else ""

    def _authenticated(self) -> bool:
        if not _cfg.AUTH_ENABLED:
            return True
        cookie_value = self._session_cookie_value()
        return bool(cookie_value) and _verify_auth_cookie(cookie_value)

    def _csrf_ok(self) -> bool:
        token = self.headers.get("X-CSRF-Token") or ""
        return _verify_csrf_token(self._session_cookie_value(), token)

    def _send_json_with_cookie(self, status: int, payload: dict[str, Any], cookie_value: str, max_age: int) -> None:
        body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        cookie = (
            f"{COOKIE_NAME}={cookie_value}; HttpOnly; Secure; SameSite=Strict; "
            f"Path=/; Max-Age={max_age}"
        )
        self._send_bytes(status, body, "application/json; charset=utf-8", {"Set-Cookie": cookie})

    def _handle_login(self) -> None:
        ip = self.client_address[0]
        if _login_rate_limited(ip):
            self._send_error_json(HTTPStatus.TOO_MANY_REQUESTS, "Too many login attempts. Wait and try again.")
            return
        try:
            payload = self._read_json_body()
        except (BadRequest, json.JSONDecodeError):
            self._send_error_json(HTTPStatus.BAD_REQUEST, "Invalid login request.")
            return
        password = str(payload.get("password") or "")
        if not _cfg.AUTH_ENABLED:
            self._send_json(HTTPStatus.OK, {"ok": True, "authDisabled": True})
            return
        if verify_auth_password(password):
            _clear_login_failures(ip)
            cookie_value = _make_auth_cookie()
            self._send_json_with_cookie(
                HTTPStatus.OK,
                {"ok": True, "csrfToken": _make_csrf_token(cookie_value)},
                cookie_value,
                AUTH_TTL_SECONDS,
            )
        else:
            _record_login_failure(ip)
            self._send_error_json(HTTPStatus.UNAUTHORIZED, "Invalid password.")

    def _handle_token_dashboard(self, path: str) -> None:
        token = path[len("/api/view/"):].strip("/")
        if not token or not re.fullmatch(r"[0-9a-f]{32}", token):
            self._send_error_json(HTTPStatus.NOT_FOUND, "Not found.")
            return
        session_id = validate_share_token(token)
        if not session_id:
            self._send_error_json(HTTPStatus.GONE, "This share link has expired or been revoked.")
            return
        dashboard = cached_reliable_dashboard_for_session(session_id)
        if not isinstance(dashboard, dict):
            with SHARED_DASHBOARD_LOCK:
                if SHARED_DASHBOARD_STATE.get("sessionId") == session_id:
                    candidate = SHARED_DASHBOARD_STATE.get("dashboard")
                    dashboard = candidate if isinstance(candidate, dict) else None
        if not isinstance(dashboard, dict):
            self._send_json(HTTPStatus.OK, {"ok": False, "message": "No dashboard data available for this share link yet."})
            return
        self._send_json(
            HTTPStatus.OK,
            {"ok": True, "dashboard": json_clone(dashboard), "updatedAt": dashboard.get("generatedAt", "")},
        )

    def do_GET(self) -> None:
        if not self._require_https():
            return
        self.request_id = uuid.uuid4().hex[:8]
        try:
            path = urlparse(self.path).path

            # --- Always-open routes (no auth) ---
            if path == "/favicon.ico":
                self._send_bytes(HTTPStatus.OK, FAVICON_SVG, "image/svg+xml")
                return
            if path == "/networker-logo.png":
                if NETWORKER_LOGO_PATH.exists():
                    self._send_bytes(HTTPStatus.OK, NETWORKER_LOGO_PATH.read_bytes(), "image/png")
                else:
                    self._send_bytes(HTTPStatus.OK, FAVICON_SVG, "image/svg+xml")
                return
            if path == "/api/health":
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "app": APP_NAME,
                        "version": APP_VERSION,
                        "https": True,
                        "debug": _cfg.APP_DEBUG,
                        "time": datetime.now().astimezone().isoformat(),
                    },
                )
                return

            # --- Token-gated share routes (capability URL, no cookie) ---
            if path.startswith("/view/"):
                token = path[6:].strip("/")
                if not token or not re.fullmatch(r"[0-9a-f]{32}", token):
                    self._send_error_json(HTTPStatus.NOT_FOUND, "Not found.")
                    return
                session_id = validate_share_token(token)
                if not session_id:
                    self._send_bytes(
                        HTTPStatus.GONE,
                        b"<html><body><p>This share link has expired or been revoked.</p></body></html>",
                        "text/html; charset=utf-8",
                    )
                    return
                self._send_bytes(
                    HTTPStatus.OK,
                    read_only_view_html(token).encode("utf-8"),
                    "text/html; charset=utf-8",
                )
                return
            if path.startswith("/api/view/"):
                self._handle_token_dashboard(path)
                return

            # --- Root: login page when auth required and not authenticated ---
            if path in ("/", "/index.html"):
                if _cfg.AUTH_ENABLED and not self._authenticated():
                    self._send_bytes(HTTPStatus.OK, login_page_html().encode("utf-8"), "text/html; charset=utf-8")
                else:
                    self._send_bytes(HTTPStatus.OK, dashboard_html().encode("utf-8"), "text/html; charset=utf-8")
                return

            # --- Everything below requires authentication ---
            if _cfg.AUTH_ENABLED and not self._authenticated():
                self._send_error_json(HTTPStatus.UNAUTHORIZED, "Authentication required.")
                return

            if path == "/api/csrf":
                # Token bootstrap for an already-authenticated page load: the
                # session cookie is HttpOnly, so the UI cannot derive this itself.
                token = _make_csrf_token(self._session_cookie_value()) if _cfg.AUTH_ENABLED else ""
                self._send_json(HTTPStatus.OK, {"ok": True, "csrfToken": token})
                return

            if path == "/api/status":
                with SHARED_DASHBOARD_LOCK:
                    updated = float(SHARED_DASHBOARD_STATE.get("updatedAt") or 0)
                    last_refresh = SHARED_DASHBOARD_STATE.get("lastRefresh") or ""
                    last_error = SHARED_DASHBOARD_STATE.get("lastError") or ""
                with SSE_CLIENTS_LOCK:
                    sse_count = len(SSE_CLIENTS)
                age = int(time.time() - updated) if updated else None
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "version": APP_VERSION,
                        "uptimeSeconds": int(time.time() - PROCESS_START_TIME),
                        "threads": threading.active_count(),
                        "sessions": len(_session_ids_snapshot()),
                        "automations": len(_automation_keys_snapshot()),
                        "sseClients": sse_count,
                        "sharedDashboard": {
                            "lastRefresh": last_refresh,
                            "lastRefreshAgeSeconds": age,
                            "lastError": last_error,
                        },
                        "authEnabled": _cfg.AUTH_ENABLED,
                        "allowlistEnabled": _cfg.ALLOWLIST_ENABLED,
                    },
                )
                return
            if path == "/api/current-dashboard":
                self._send_json(HTTPStatus.OK, shared_dashboard_payload())
                return
            if path == "/api/profiles":
                with PROFILES_LOCK:
                    self._send_json(HTTPStatus.OK, {"ok": True, "profiles": _mask_profiles(load_profiles())})
                return
            if path == "/api/email-config":
                self._send_json(HTTPStatus.OK, email_config_public())
                return
            if path == "/api/ui-theme":
                self._send_json(HTTPStatus.OK, {"ok": True, "theme": load_ui_theme() or "default"})
                return
            if path == "/api/stream":
                wfile = self.wfile
                if not _sse_register(wfile):
                    self._send_error_json(HTTPStatus.SERVICE_UNAVAILABLE, "Too many live viewers connected. Try again shortly.")
                    return
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Accel-Buffering", "no")
                self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
                self.end_headers()
                payload = shared_dashboard_payload()
                dash = payload.get("dashboard")
                if isinstance(dash, dict):
                    try:
                        wfile.write(f"event: dashboard\ndata: {json.dumps(dash, separators=(',', ':'))}\n\n".encode("utf-8"))
                        wfile.flush()
                    except OSError:
                        pass
                while not SHARED_REFRESH_STOP.is_set():
                    try:
                        wfile.write(b": heartbeat\n\n")
                        wfile.flush()
                        SHARED_REFRESH_STOP.wait(25)
                    except OSError:
                        break
                with SSE_CLIENTS_LOCK:
                    try:
                        SSE_CLIENTS.remove(wfile)
                    except ValueError:
                        pass
                return
            if path == "/api/snapshots":
                query = dict(parse_qsl(urlparse(self.path).query, keep_blank_values=True))
                action = query.get("action", "compare")
                if action == "list":
                    with SNAPSHOTS_LOCK:
                        self._send_json(HTTPStatus.OK, {"ok": True, "snapshots": list_snapshot_summary()})
                elif action == "history":
                    with SNAPSHOTS_LOCK:
                        self._send_json(HTTPStatus.OK, snapshot_history_all())
                elif action == "export":
                    with SNAPSHOTS_LOCK:
                        csv_data = snapshots_to_csv()
                    self._send_bytes(HTTPStatus.OK, csv_data.encode("utf-8"), "text/csv; charset=utf-8")
                elif action == "auto-config":
                    self._send_json(HTTPStatus.OK, {"ok": True, "enabled": load_auto_snapshot_config()})
                else:
                    with SNAPSHOTS_LOCK:
                        self._send_json(HTTPStatus.OK, compare_dashboard_snapshots(query.get("range", "7d")))
                return

            self._send_error_json(HTTPStatus.NOT_FOUND, "Not found.")
        except Exception as exc:  # noqa: BLE001
            ref = getattr(self, "request_id", "-")
            LOG.error(f"do_GET unhandled error: {safe_log_text(exc)}", extra={"request_id": ref}, exc_info=True)
            try:
                self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"Internal error (ref {ref}).")
            except Exception as exc2:  # noqa: BLE001 — headers may already be sent
                LOG.debug(f"failed to send error response (ref {ref}): {exc2}")

    def do_POST(self) -> None:
        if not self._require_https():
            return
        self.request_id = uuid.uuid4().hex[:8]
        path = urlparse(self.path).path
        # Always-open auth endpoints
        if path == "/api/login":
            self._handle_login()
            return
        if path == "/api/logout":
            self._send_json_with_cookie(HTTPStatus.OK, {"ok": True}, "", 0)
            return
        # Auth gate for all other POST routes
        if _cfg.AUTH_ENABLED and not self._authenticated():
            self._send_error_json(HTTPStatus.UNAUTHORIZED, "Authentication required.")
            return
        # CSRF gate: every state-changing route requires the per-session token.
        # Only meaningful when cookie auth is on (no session cookie, no CSRF).
        if _cfg.AUTH_ENABLED and not self._csrf_ok():
            self._send_error_json(HTTPStatus.FORBIDDEN, "CSRF token missing or invalid.")
            return
        allowed = {"/api/dashboard", "/api/export", "/api/server-health",
                   "/api/alert-automation", "/api/snapshots",
                   "/api/share", "/api/multi-server", "/api/profiles",
                   "/api/ui-theme"}
        if path not in allowed:
            self._send_error_json(HTTPStatus.NOT_FOUND, "Not found.")
            return

        try:
            payload = self._read_json_body()
            if path == "/api/ui-theme":
                theme = save_ui_theme(payload.get("theme"))
                self._send_json(HTTPStatus.OK, {"ok": True, "theme": theme})
                return
            if path == "/api/snapshots":
                snap_action = str(payload.get("action") or "save").strip().lower()
                if snap_action == "delete":
                    date_str = str(payload.get("date") or "").strip()
                    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str):
                        raise BadRequest("Invalid date format. Use YYYY-MM-DD.")
                    with SNAPSHOTS_LOCK:
                        delete_snapshot_by_date(date_str)
                        summaries = list_snapshot_summary()
                    self._send_json(HTTPStatus.OK, {"ok": True, "snapshots": summaries})
                    return
                if snap_action == "annotate":
                    date_str = str(payload.get("date") or "").strip()
                    note = str(payload.get("note") or "").strip()
                    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str):
                        raise BadRequest("Invalid date format. Use YYYY-MM-DD.")
                    with SNAPSHOTS_LOCK:
                        annotate_snapshot(date_str, note)
                    self._send_json(HTTPStatus.OK, {"ok": True})
                    return
                if snap_action == "auto-config":
                    enabled = bool(payload.get("enabled", False))
                    save_auto_snapshot_config(enabled)
                    # Capture immediately on enable so the first snapshot does not
                    # wait up to 10 minutes for the next worker tick, and report
                    # the outcome so the UI can confirm it actually saved.
                    result = _auto_snapshot_once() if enabled else "disabled"
                    self._send_json(HTTPStatus.OK, {
                        "ok": True,
                        "enabled": enabled,
                        "result": result,
                        "summary": snapshot_summary_text(),
                    })
                    return
                # default: save
                dashboard = payload.get("dashboard") if isinstance(payload.get("dashboard"), dict) else None
                if not dashboard:
                    with SHARED_DASHBOARD_LOCK:
                        dashboard = SHARED_DASHBOARD_STATE.get("dashboard")
                if not isinstance(dashboard, dict):
                    raise BadRequest("No dashboard data is available to save as a snapshot.")
                with SNAPSHOTS_LOCK:
                    record = save_dashboard_snapshot(dashboard)
                    comparison = compare_dashboard_snapshots(payload.get("range", "7d"))
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "message": f"Snapshot saved for {record['date']}.",
                        "snapshot": record,
                        "summary": snapshot_summary_text(),
                        "comparison": comparison,
                    },
                )
                return
            if path == "/api/profiles":
                action = str(payload.get("action") or "").strip().lower()
                with PROFILES_LOCK:
                    profiles = load_profiles()
                    if action == "save":
                        name = str(payload.get("name") or "").strip()
                        data = payload.get("data")
                        if not name:
                            raise BadRequest("Profile name required.")
                        if not isinstance(data, dict):
                            raise BadRequest("Profile data must be an object.")
                        # Encrypt password fields; keep all other fields as-is
                        safe: dict[str, Any] = {}
                        for k, v in data.items():
                            if k in ("password", "wmiPassword"):
                                raw = str(v or "").strip()
                                if raw and raw not in (_PROFILE_PW_SENTINEL, _PROFILE_PW_SAVED):
                                    safe[f"_enc_{k}"] = encrypt_profile_secret(raw)
                                elif k in profiles.get(name, {}):
                                    # Keep existing encrypted value if no new one provided
                                    safe[f"_enc_{k}"] = profiles[name].get(f"_enc_{k}", "")
                            else:
                                safe[k] = v
                        # Preserve existing encrypted passwords not touched this save
                        existing = profiles.get(name, {})
                        for enc_key in ("_enc_password", "_enc_wmiPassword"):
                            if enc_key not in safe and enc_key in existing:
                                safe[enc_key] = existing[enc_key]
                        profiles[name] = safe
                        save_profiles(profiles)
                        self._send_json(HTTPStatus.OK, {"ok": True, "profiles": _mask_profiles(profiles)})
                    elif action == "delete":
                        name = str(payload.get("name") or "").strip()
                        profiles.pop(name, None)
                        save_profiles(profiles)
                        self._send_json(HTTPStatus.OK, {"ok": True, "profiles": _mask_profiles(profiles)})
                    else:
                        self._send_json(HTTPStatus.OK, {"ok": True, "profiles": _mask_profiles(profiles)})
                return

            if path == "/api/share":
                action = str(payload.get("action") or "").strip().lower()
                if action == "create":
                    session_id = str(payload.get("sessionId") or "").strip()
                    if not session_id or not _session_exists(session_id):
                        raise BadRequest("Valid session ID required to create share link.")
                    token = create_share_token(session_id)
                    self._send_json(HTTPStatus.OK, {"ok": True, "token": token})
                elif action == "revoke":
                    token = str(payload.get("token") or "").strip()
                    revoke_share_token(token)
                    self._send_json(HTTPStatus.OK, {"ok": True})
                else:
                    raise BadRequest("action must be 'create' or 'revoke'.")
                return

            if path == "/api/multi-server":
                raw_ids = payload.get("sessionIds")
                if not isinstance(raw_ids, list) or not raw_ids:
                    raise BadRequest("sessionIds must be a non-empty array.")
                session_ids = [str(s).strip() for s in raw_ids if str(s).strip()]
                ms_status, ms_body = build_multi_server_dashboard(session_ids)
                self._send_json(ms_status, ms_body)
                return

            if path == "/api/alert-automation":
                status, body = handle_alert_automation(payload)
                self._send_json(status, body)
                return

            if path == "/api/server-health":
                session_id = str(payload.get("sessionId") or "").strip()
                if not session_id:
                    raise BadRequest("Dashboard session id is required for server health refresh.")
                status, body = build_server_health_from_session(session_id)
                self._send_json(status, body)
                return

            cached_dashboard = payload.get("dashboard") if path == "/api/export" else None
            if isinstance(cached_dashboard, dict) and cached_dashboard.get("ok"):
                status, body = HTTPStatus.OK, cached_dashboard
            else:
                session_id = str(payload.get("sessionId") or "").strip()
                if session_id and not payload.get("password"):
                    report_range = str(payload.get("reportRange") or DEFAULT_REPORT_RANGE).strip().lower()
                    status, body = build_dashboard_from_session(
                        session_id,
                        report_range,
                        str(payload.get("customStartDate") or "").strip(),
                        str(payload.get("customEndDate") or "").strip(),
                    )
                else:
                    payload = _resolve_profile_password(payload)
                    config = validate_payload(payload)
                    status, body = build_dashboard(config)

            if path == "/api/export":
                if status >= 400:
                    self._send_json(status, body)
                else:
                    report_bytes = build_excel_report(body)
                    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    filename = f"networker_dashboard_{body.get('summary', {}).get('range', DEFAULT_REPORT_RANGE)}_{stamp}.xlsx"
                    self._send_bytes(
                        HTTPStatus.OK,
                        report_bytes,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        {"Content-Disposition": f'attachment; filename="{filename}"'},
                    )
            else:
                if path == "/api/dashboard" and status < 400 and body.get("ok"):
                    set_shared_dashboard(str(body.get("sessionId") or session_id or ""), body)
                self._send_json(status, body)
            if status >= 400:
                self.log_dashboard_failure(status, body)
        except BadRequest as exc:
            self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        except json.JSONDecodeError:
            self._send_error_json(HTTPStatus.BAD_REQUEST, "Request body must be valid JSON.")
        except Exception as exc:
            ref = getattr(self, "request_id", "-")
            LOG.error(f"do_POST unhandled error: {safe_log_text(exc)}", extra={"request_id": ref}, exc_info=True)
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"Internal error (ref {ref}).")




class ExclusiveThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, *args: Any, max_connections: int = DEFAULT_MAX_CONNECTIONS, **kwargs: Any) -> None:
        self._conn_semaphore = threading.BoundedSemaphore(max(1, int(max_connections)))
        super().__init__(*args, **kwargs)

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def _acquire_slot(self) -> bool:
        return self._conn_semaphore.acquire(blocking=False)

    def _release_slot(self) -> None:
        try:
            self._conn_semaphore.release()
        except ValueError:
            # Over-release means a slot was released without a matching acquire —
            # a logic bug. Log it rather than crash the worker thread.
            debug_log("ExclusiveThreadingHTTPServer._release_slot over-release ignored")

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._acquire_slot():
            # At connection cap — refuse by closing the socket. A fronting
            # proxy/LB will retry; we avoid spawning an unbounded thread.
            self.shutdown_request(request)
            return
        try:
            # super() spawns the worker thread, which releases the slot in
            # process_request_thread's finally. If thread creation itself fails
            # (e.g. OS thread exhaustion), release here so the slot is not leaked.
            super().process_request(request, client_address)
        except Exception:
            self._release_slot()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release_slot()

    def handle_error(self, request: Any, client_address: Any) -> None:
        # Replace socketserver's raw stderr traceback dump. Client-side transport
        # errors (disconnects, TLS EOF, resets) are benign -> DEBUG; anything else
        # is a real server fault -> ERROR with traceback, as structured JSON.
        exc = sys.exc_info()[1]
        if isinstance(exc, OSError):
            LOG.debug(f"client {client_address} connection error: {exc}")
        else:
            LOG.error(f"request error from {client_address}: {exc}", extra={"client": client_address[0] if client_address else "-"}, exc_info=True)


def local_port_probe_hosts(bind_host: str) -> list[str]:
    normalized = (bind_host or "").strip().lower()
    hosts = ["127.0.0.1", "localhost"]
    if normalized in ("", "0.0.0.0", "::"):
        hosts.extend(local_ipv4_addresses())
    elif normalized not in ("127.0.0.1", "localhost", "::1"):
        hosts.append(bind_host)
    seen: set[str] = set()
    result: list[str] = []
    for host in hosts:
        if host and host not in seen:
            seen.add(host)
            result.append(host)
    return result


def port_has_active_listener(bind_host: str, port: int) -> bool:
    for host in local_port_probe_hosts(bind_host):
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return True
        except OSError:
            continue
    return False


def can_exclusively_bind_port(bind_host: str, port: int) -> bool:
    if port == 0:
        return True
    if port_has_active_listener(bind_host, port):
        return False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind((bind_host, port))
        return True
    except OSError:
        return False


def bind_dashboard_server(
    bind_host: str, requested_port: int, max_connections: int = DEFAULT_MAX_CONNECTIONS
) -> tuple[ThreadingHTTPServer, int, bool]:
    if requested_port != 0 and not can_exclusively_bind_port(bind_host, requested_port):
        server = ExclusiveThreadingHTTPServer((bind_host, 0), DashboardHandler, max_connections=max_connections)
        return server, int(server.server_address[1]), True
    try:
        server = ExclusiveThreadingHTTPServer((bind_host, requested_port), DashboardHandler, max_connections=max_connections)
        return server, int(server.server_address[1]), False
    except OSError as exc:
        if requested_port == 0:
            raise
        try:
            server = ExclusiveThreadingHTTPServer((bind_host, 0), DashboardHandler, max_connections=max_connections)
        except OSError as fallback_exc:
            raise exc from fallback_exc
        return server, int(server.server_address[1]), True


def port_self_test_script_block(bind_host: str, requested_port: int) -> str:
    if requested_port == 0:
        return (
            f"Port self-test script block: request an OS-selected random HTTPS port on {bind_host}; "
            "validate the selected listener before launch."
        )
    return (
        f"Port self-test script block: validate {bind_host}:{requested_port}; "
        "if unavailable, bind to an OS-selected random HTTPS port."
    )


def preferred_launch_url(urls: list[tuple[str, str]]) -> str:
    for label, url in urls:
        if label == "Localhost":
            return url
    return urls[0][1] if urls else ""


def self_test_dashboard_listener(
    url: str, timeout_seconds: float = 8.0, cert_path: Path | None = None
) -> tuple[bool, str]:
    health_url = f"{url.rstrip('/')}/api/health"
    deadline = time.monotonic() + max(0.5, timeout_seconds)
    if cert_path and cert_path.exists():
        # Pin our own serving certificate as the sole trust anchor. The CN
        # rarely matches the probe host (localhost/IP), so hostname checking
        # stays off — chain verification against the exact cert is the point.
        tls_context = ssl.create_default_context(cafile=str(cert_path))
        tls_context.check_hostname = False
    else:
        tls_context = ssl._create_unverified_context()
    last_error = "listener did not respond"

    while time.monotonic() < deadline:
        try:
            request = Request(health_url, headers={"Accept": "application/json"})
            with urlopen(request, timeout=1.5, context=tls_context) as response:
                payload = json.loads(response.read(64 * 1024).decode("utf-8"))
            if payload.get("ok") is True and payload.get("https") is True:
                port = urlparse(url).port or DEFAULT_PORT
                return True, f"HTTPS listener is healthy on port {port}."
            last_error = f"health endpoint returned unexpected payload: {payload!r}"
        except Exception as exc:
            last_error = str(exc)
        time.sleep(0.2)

    return False, f"HTTPS listener self-test failed at {health_url}: {last_error}"


def auto_launch_dashboard(url: str, enabled: bool = True) -> tuple[bool, str]:
    if not enabled:
        return False, "Auto-launch disabled by --no-launch."
    if not url:
        return False, "No dashboard URL available to launch."
    try:
        opened = webbrowser.open_new_tab(url)
    except Exception as exc:
        return False, f"Unable to auto-launch browser: {exc}"
    if opened:
        return True, f"Opened dashboard page: {url}"
    return False, f"Browser auto-launch was requested but no browser accepted: {url}"


def local_ipv4_addresses() -> list[str]:
    addresses: set[str] = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            address = probe.getsockname()[0]
            if address and not address.startswith("127."):
                addresses.add(address)
    except OSError:
        pass
    for name in {socket.gethostname(), socket.getfqdn()}:
        try:
            for family, _, _, _, sockaddr in socket.getaddrinfo(name, None, socket.AF_INET):
                if family == socket.AF_INET:
                    address = sockaddr[0]
                    if address and not address.startswith("127."):
                        addresses.add(address)
        except socket.gaierror:
            continue
    return sorted(addresses)


def service_access_urls(bind_host: str, port: int) -> list[tuple[str, str]]:
    normalized = (bind_host or "").strip().lower()
    urls: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(label: str, host: str) -> None:
        url = f"https://{host}:{port}/"
        if url not in seen:
            seen.add(url)
            urls.append((label, url))

    if normalized in ("", "0.0.0.0", "::"):
        add("Localhost", "localhost")
        for address in local_ipv4_addresses():
            add("Local server IP", address)
    elif normalized in ("127.0.0.1", "localhost", "::1"):
        add("Localhost", "localhost")
    else:
        add("Configured bind address", bind_host)
        if not normalized.startswith("127."):
            add("Localhost", "localhost")
    return urls
