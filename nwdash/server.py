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
from datetime import datetime
from http import HTTPStatus
from http.client import HTTPException
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlparse
from urllib.request import Request, urlopen

from . import admin as _admin
from . import config as _cfg
from .auth import (
    LOGIN_SESSIONS,
    _clear_login_failures,
    _login_rate_limited,
    _make_auth_cookie,
    _make_csrf_token,
    _record_login_failure,
    _verify_auth_cookie,
    _verify_csrf_token,
    revoke_auth_cookie,
    session_id_of_cookie,
    verify_auth_password,
)
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
    debug_log,
    safe_log_text,
)
from .emailer import handle_alert_automation
from .models import (
    SHARED_DASHBOARD_LOCK,
    SHARED_DASHBOARD_STATE,
    SHARED_REFRESH_STOP,
    SSE_CLIENTS,
    SSE_CLIENTS_LOCK,
    BadRequest,
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
    profile_record_for_save,
    save_profiles,
    validate_payload,
)
from .reports import build_excel_report
from .sessions import (
    build_dashboard,
    build_dashboard_from_session,
    build_multi_server_dashboard,
    build_server_health_from_session,
    purge_sessions_for_credential,
)
from .snapshots import (
    _auto_snapshot_once,
    annotate_snapshot,
    compare_dashboard_snapshots,
    delete_snapshot_by_date,
    email_config_public,
    list_snapshot_summary,
    load_auto_snapshot_config,
    load_ui_prefs,
    load_ui_theme,
    save_auto_snapshot_config,
    save_dashboard_snapshot,
    save_ui_prefs,
    save_ui_theme,
    snapshot_history_all,
    snapshot_summary_text,
    snapshots_to_csv,
)
from .ui import (
    FAVICON_SVG,
    NETWORKER_LOGO_PATH,
    admin_page_html,
    dashboard_html,
    login_page_html,
    read_only_view_html,
    tv_page_html,
)

# Alert-automation actions that change something (the rest only read), for the
# audit log; the value is how the audit names the change.
_AUDITED_AUTOMATION_ACTIONS = {
    "save": "Email settings saved",
    "start": "Schedule started",
    "stop": "Schedule stopped",
    "set_enabled": "Schedule paused or resumed",
    "save-profile": "Email profile saved",
    "delete-profile": "Email profile deleted",
    "toggle-profile": "Email profile switched",
}

# Sent only with the gateway's own "not signed in" 401. The dashboard reloads to
# its sign-in page on a 401 that carries it, and on no other (review F2).
LOGIN_REQUIRED_HEADER = "X-NWDash-Login"


def upstream_auth_failure(status: int, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Answer a NetWorker server's 401 or 403 as 502 with a code.

    The collectors pass on the NetWorker server's own 401 or 403 when it refuses
    the credentials. Sent unchanged, it looked like this gateway's login
    expiring: the dashboard reloaded instead of showing the error (review F2).
    """
    if status not in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
        return status, body
    detail = str(body.get("error") or "").strip() or "Check the user name, password and account access."
    return HTTPStatus.BAD_GATEWAY, {
        **body,
        "code": "networker_auth_failed",
        "upstreamStatus": int(status),
        "error": f"The NetWorker server refused the credentials (HTTP {int(status)}). {detail}",
    }


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

    def _send_json(self, status: int, payload: dict[str, Any], extra_headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8", extra_headers)

    def _send_error_json(self, status: int, message: str) -> None:
        self._send_json(status, {"ok": False, "error": message})

    def _send_login_required(self) -> None:
        """The gateway's 401: this request has no valid login."""
        self._send_json(
            HTTPStatus.UNAUTHORIZED,
            {"ok": False, "code": "login_required", "error": "Authentication required."},
            {LOGIN_REQUIRED_HEADER: "required"},
        )

    def _read_json_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError as exc:
            raise BadRequest("Invalid Content-Length.") from exc
        if length <= 0 or length > MAX_POST_BYTES:
            raise BadRequest("Request body size is invalid.")
        raw = self.rfile.read(length)
        self._body_consumed = True
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise BadRequest("JSON body must be an object.")
        return payload

    def _drain_request_body(self) -> None:
        """Consume an unread request body before an early error response.

        On a keep-alive connection the unread bytes would otherwise be parsed
        as the start of the NEXT request line (e.g. '{"theme":..}GET /x' -> 501),
        poisoning every subsequent request on that connection. Idempotent:
        does nothing if _read_json_body already consumed the body (a blind
        re-read would block on the open socket until the request timeout).
        """
        if getattr(self, "_body_consumed", False):
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            self.close_connection = True
            return
        if length <= 0:
            return
        if length > MAX_POST_BYTES:
            # Refuse to swallow oversized bodies; drop the connection instead.
            self.close_connection = True
            return
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 65536))
            if not chunk:
                self.close_connection = True
                return
            remaining -= len(chunk)
        self._body_consumed = True

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

    def _json_body_declared(self) -> bool:
        # A cross-site page can POST text/plain, form-urlencoded or multipart
        # without a CORS preflight; application/json forces one, and this server
        # never answers a preflight. Our UI always sends JSON.
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0]
        return content_type.strip().lower() == "application/json"

    def _same_origin(self) -> bool:
        # Browsers attach Sec-Fetch-Site and Origin to cross-site POSTs; clients
        # that send neither (curl, scripts, the tests) are not browsers acting
        # for the operator, so they are let through.
        site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if site and site != "same-origin":
            return False
        origin = (self.headers.get("Origin") or "").strip().lower()
        if not origin:
            return True
        host = (self.headers.get("Host") or "").strip().lower()
        return bool(host) and origin == f"https://{host}"

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
            self._drain_request_body()
            self._send_error_json(HTTPStatus.TOO_MANY_REQUESTS, "Too many login attempts. Wait and try again.")
            return
        try:
            payload = self._read_json_body()
        except (BadRequest, json.JSONDecodeError):
            self._drain_request_body()
            self._send_error_json(HTTPStatus.BAD_REQUEST, "Invalid login request.")
            return
        password = str(payload.get("password") or "")
        if not _cfg.AUTH_ENABLED:
            self._send_json(HTTPStatus.OK, {"ok": True, "authDisabled": True})
            return
        if verify_auth_password(password):
            _clear_login_failures(ip)
            cookie_value = _make_auth_cookie(ip, str(self.headers.get("User-Agent") or ""))
            sid = session_id_of_cookie(cookie_value)
            body: dict[str, Any] = {"ok": True, "csrfToken": _make_csrf_token(cookie_value)}
            if payload.get("admin") is True:
                # Signing in on /admin types the password just now, which is
                # what the admin window asks for, so it opens with the login.
                body["unlockedUntil"] = int(LOGIN_SESSIONS.unlock_admin(sid, _admin.ADMIN_UNLOCK_SECONDS))
            _admin.audit("Signed in", ip=ip, sid=sid, item="admin console" if payload.get("admin") is True else "dashboard")
            self._send_json_with_cookie(HTTPStatus.OK, body, cookie_value, AUTH_TTL_SECONDS)
        else:
            _record_login_failure(ip)
            _admin.audit("Sign-in failed", ip=ip, item="dashboard", field="password", new="wrong")
            self._send_error_json(HTTPStatus.UNAUTHORIZED, "Invalid password.")

    def _sid(self) -> str:
        """This request's login session id ("" with no password or no login)."""
        return session_id_of_cookie(self._session_cookie_value()) if _cfg.AUTH_ENABLED else ""

    def _audit(self, action: str, **fields: Any) -> None:
        _admin.audit(action, ip=self.client_address[0], sid=self._sid(), **fields)

    def _setup_code(self) -> str:
        """The setup code the console sends while no password is set."""
        return str(self.headers.get("X-NWDash-Setup-Code") or "")

    def _console_reachable(self) -> bool:
        """Signed in, or (no password) a browser on the server itself and not
        through a proxy. On failure the 403 has been sent and the body drained."""
        access = _admin.console_access(self.client_address[0], self._authenticated(), self.headers)
        if access in ("ok", "open"):
            return True
        self._drain_request_body()
        self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "code": access,
                                               "error": "The admin console is not available from this address."})
        return False

    def _admin_gate(self, activity: bool) -> bool:
        """The admin console's checks on top of the dashboard's sign-in: the
        console reachable, then the admin window open (the setup code when no
        password is set). Only `activity` (a change, or a read the person asked
        for) extends the window; background refreshes do not, so an unattended
        console still locks. On failure the error has been sent (body drained)."""
        if not self._console_reachable():
            return False
        if not _cfg.AUTH_ENABLED:
            status, problem = _admin.setup_code_problem(self._setup_code(), self.client_address[0])
            if status:
                self._drain_request_body()
                self._send_json(status, {"ok": False, "code": "setup_code_required", "error": problem})
                return False
            return True
        sid = self._sid()
        if not _admin.admin_window_open(sid):
            self._drain_request_body()
            self._send_json(HTTPStatus.FORBIDDEN, {"ok": False, "code": "unlock_required",
                                                   "error": "Type the password again to make admin changes."})
            return False
        if activity:
            _admin.touch_admin_window(sid)
        return True

    def _listener_probe(self) -> tuple[bool, str]:
        host = str(self.server.server_address[0])
        if host in ("0.0.0.0", "::", ""):
            host = "127.0.0.1"
        port = int(self.server.server_address[1])
        url = f"https://[{host}]:{port}" if ":" in host else f"https://{host}:{port}"
        cert = Path(_cfg.TLS_CERT_FILE) if _cfg.TLS_CERT_FILE else None
        return self_test_dashboard_listener(url, timeout_seconds=3.0, cert_path=cert)

    def _use_tls_context(self, context: ssl.SSLContext) -> bool:
        """Serve new connections with `context`; open ones keep theirs."""
        if getattr(self.server, "ssl_context", None) is None:
            return False
        self.server.ssl_context = context
        return True

    def _handle_admin_get(self, path: str, query: dict[str, str]) -> None:
        # The console marks reads a person asked for; its timed refreshes don't.
        if not self._admin_gate(activity=self.headers.get("X-NWDash-Activity") == "1"):
            return
        sid = self._sid()
        route = path[len("/api/admin/"):]
        if route == "overview":
            self._send_json(HTTPStatus.OK, _admin.overview())
        elif route == "sessions":
            self._send_json(HTTPStatus.OK, {"ok": True, "sessions": _admin.list_sessions(sid),
                                            "lifetimeHours": _admin.session_lifetime_hours()})
        elif route == "audit":
            date = query.get("date", "")
            if date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                self._send_error_json(HTTPStatus.BAD_REQUEST, "date must be YYYY-MM-DD.")
                return
            rows = _admin.read_audit(date=date, action=query.get("action", ""), query=query.get("q", ""))
            self._send_json(HTTPStatus.OK, {"ok": True, "entries": rows, "actions": _admin.audit_actions()})
        elif route == "logs":
            try:
                lines = int(query.get("lines") or 300)
            except ValueError:
                lines = 300
            self._send_json(HTTPStatus.OK, _admin.read_log(query.get("level", "INFO"), query.get("q", ""), lines))
        elif route == "tls":
            self._send_json(HTTPStatus.OK, _admin.tls_info())
        elif route == "security":
            self._send_json(HTTPStatus.OK, {
                "ok": True,
                "passwordSet": bool(_cfg.AUTH_ENABLED),
                "passwordFromStartup": bool(_cfg.AUTH_PASSWORD_FROM_STARTUP),
                "passwordMinLength": _admin.PASSWORD_MIN_LENGTH,
                "lifetimeHours": _admin.session_lifetime_hours(),
                "unlockMinutes": _admin.ADMIN_UNLOCK_SECONDS // 60,
                "allowlist": _admin.allowlist_view(),
            })
        elif route == "shares":
            self._send_json(HTTPStatus.OK, {"ok": True, "links": _admin.list_share_links()})
        elif route == "appearance":
            self._send_json(HTTPStatus.OK, {"ok": True, **load_ui_prefs()})
        else:
            self._send_error_json(HTTPStatus.NOT_FOUND, "Not found.")

    def _handle_admin_post(self, path: str) -> None:
        route = path[len("/api/admin/"):]
        ip = self.client_address[0]
        if route in ("unlock", "lock"):
            if not self._console_reachable():
                return
            payload = self._read_json_body()
            if route == "lock":
                self._send_json(HTTPStatus.OK, _admin.lock(self._sid(), ip))
                return
            status, body = _admin.unlock(self._sid(), str(payload.get("password") or ""), ip)
            self._send_json(status, body)
            return
        if not self._admin_gate(activity=True):
            return
        sid = self._sid()
        payload = self._read_json_body()
        if route == "self-test":
            result = _admin.run_self_test(self._listener_probe)
            _admin.audit("Self-test run", ip=ip, sid=sid, item="health",
                         new=f"{result['failures']} failed, {result['warnings']} warnings")
            self._send_json(HTTPStatus.OK, result)
        elif route == "sessions/revoke":
            status, body = _admin.revoke_session(str(payload.get("id") or ""), sid, ip)
            self._send_json(status, body)
        elif route == "sessions/revoke-others":
            self._send_json(HTTPStatus.OK, _admin.revoke_other_sessions(sid, ip))
        elif route == "password":
            status, body = _admin.change_password(sid, str(payload.get("current") or ""), str(payload.get("new") or ""), ip)
            self._send_json(status, body)
        elif route == "allowlist":
            status, body = _admin.save_allowlist(payload.get("entries"), sid, ip)
            self._send_json(status, body)
        elif route == "shares/revoke":
            status, body = _admin.revoke_share_link(str(payload.get("id") or ""), sid, ip)
            self._send_json(status, body)
        elif route == "shares/revoke-all":
            self._send_json(HTTPStatus.OK, _admin.revoke_all_share_links(sid, ip))
        elif route == "tls":
            status, body = _admin.install_certificate(
                payload.get("certificate"), payload.get("key"), host=_admin.request_host(self.headers.get("Host")),
                confirm_name=payload.get("confirmName") is True, sid=sid, client_ip=ip, apply=self._use_tls_context)
            self._send_json(status, body)
        elif route == "tls/restore":
            status, body = _admin.restore_previous_certificate(sid=sid, client_ip=ip, apply=self._use_tls_context)
            self._send_json(status, body)
        elif route == "appearance":
            before = load_ui_prefs()
            changes = {k: payload[k] for k in ("theme", "design", "motion", "sidebar") if k in payload}
            after = save_ui_prefs(changes)
            for key in changes:
                if before.get(key) != after.get(key):
                    _admin.audit("Appearance changed", ip=ip, sid=sid, item="default for every viewer",
                                 field=key, old=before.get(key), new=after.get(key))
            self._send_json(HTTPStatus.OK, {"ok": True, **after})
        else:
            self._send_error_json(HTTPStatus.NOT_FOUND, "Not found.")

    def _handle_first_password(self) -> None:
        """POST /api/admin/setup: the first gateway password, from the server
        itself. Answers like a login, so this browser stays signed in."""
        ip = self.client_address[0]
        try:
            payload = self._read_json_body()
        except (BadRequest, json.JSONDecodeError):
            self._drain_request_body()
            self._send_error_json(HTTPStatus.BAD_REQUEST, "Invalid request.")
            return
        status, body = _admin.first_password(str(payload.get("password") or ""), ip, self.headers,
                                             str(payload.get("setupCode") or ""))
        if status != HTTPStatus.OK:
            self._send_json(status, body)
            return
        cookie_value = _make_auth_cookie(ip, str(self.headers.get("User-Agent") or ""))
        sid = session_id_of_cookie(cookie_value)
        body["csrfToken"] = _make_csrf_token(cookie_value)
        body["unlockedUntil"] = int(LOGIN_SESSIONS.unlock_admin(sid, _admin.ADMIN_UNLOCK_SECONDS))
        self._send_json_with_cookie(HTTPStatus.OK, body, cookie_value, AUTH_TTL_SECONDS)

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

            # --- Admin console: the page carries its own sign-in, and asks
            # /api/admin/state which screen to show; every admin API below the
            # auth gate re-checks access and the admin window. ---
            if path in ("/admin", "/admin/"):
                self._send_bytes(HTTPStatus.OK, admin_page_html().encode("utf-8"), "text/html; charset=utf-8")
                return
            if path == "/api/admin/state":
                authenticated = self._authenticated()
                sid = self._sid() if authenticated else ""
                self._send_json(HTTPStatus.OK, _admin.console_state(sid, self.client_address[0], authenticated,
                                                                    self.headers, self._setup_code()))
                return

            # --- TV wall-display mode: auth-gated exactly like / ---
            if path == "/tv":
                if _cfg.AUTH_ENABLED and not self._authenticated():
                    self._send_bytes(HTTPStatus.OK, login_page_html().encode("utf-8"), "text/html; charset=utf-8")
                else:
                    self._send_bytes(HTTPStatus.OK, tv_page_html().encode("utf-8"), "text/html; charset=utf-8")
                return

            # --- Everything below requires authentication ---
            if _cfg.AUTH_ENABLED and not self._authenticated():
                self._send_login_required()
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
                prefs = load_ui_prefs()
                self._send_json(HTTPStatus.OK, {"ok": True, **prefs, "theme": load_ui_theme() or "default"})
                return
            if path.startswith("/api/admin/"):
                self._handle_admin_get(path, dict(parse_qsl(urlparse(self.path).query, keep_blank_values=True)))
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
                        wfile.write(f"event: dashboard\ndata: {json.dumps(dash, separators=(',', ':'))}\n\n".encode())
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
        except Exception:
            ref = getattr(self, "request_id", "-")
            LOG.exception("do_GET unhandled error", extra={"request_id": ref})
            try:
                self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"Internal error (ref {ref}).")
            except Exception as exc2:  # noqa: BLE001 — headers may already be sent
                LOG.debug(f"failed to send error response (ref {ref}): {exc2}")

    def do_POST(self) -> None:
        if not self._require_https():
            return
        self.request_id = uuid.uuid4().hex[:8]
        self._body_consumed = False  # one handler instance serves many keep-alive requests
        path = urlparse(self.path).path
        # Cross-site gate for EVERY POST, with or without a password. The CSRF
        # token below exists only when auth is on, so without these a page the
        # operator visits could drive the API of a password-less install.
        if not self._json_body_declared():
            self._drain_request_body()
            self._send_error_json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Content-Type must be application/json.")
            return
        if not self._same_origin():
            self._drain_request_body()
            self._send_error_json(HTTPStatus.FORBIDDEN, "Cross-origin request refused.")
            return
        # Always-open auth endpoints
        if path == "/api/login":
            self._handle_login()
            return
        if path == "/api/logout":
            self._drain_request_body()
            sid = self._sid()
            # Revoke the session itself: expiring the cookie only removes this
            # browser's copy, and any other copy would stay valid until expiry.
            revoked = revoke_auth_cookie(self._session_cookie_value())
            if revoked:
                _admin.audit("Signed out", ip=self.client_address[0], sid=sid)
            self._send_json_with_cookie(HTTPStatus.OK, {"ok": True, "revoked": revoked}, "", 0)
            return
        if path == "/api/admin/setup":
            self._handle_first_password()
            return
        # Every early-error path below MUST drain the request body first, or
        # the unread bytes poison the next request on this keep-alive socket.
        # Auth gate for all other POST routes
        if _cfg.AUTH_ENABLED and not self._authenticated():
            self._drain_request_body()
            self._send_login_required()
            return
        # CSRF gate: every state-changing route requires the per-session token.
        # Only meaningful when cookie auth is on (no session cookie, no CSRF).
        if _cfg.AUTH_ENABLED and not self._csrf_ok():
            self._drain_request_body()
            self._send_error_json(HTTPStatus.FORBIDDEN, "CSRF token missing or invalid.")
            return
        if path.startswith("/api/admin/"):
            try:
                self._handle_admin_post(path)
            except (BadRequest, json.JSONDecodeError) as exc:
                self._drain_request_body()
                message = str(exc) if isinstance(exc, BadRequest) else "Request body must be valid JSON."
                self._send_error_json(HTTPStatus.BAD_REQUEST, message)
            except Exception:
                ref = getattr(self, "request_id", "-")
                LOG.exception("admin POST unhandled error", extra={"request_id": ref})
                self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"Internal error (ref {ref}).")
            return
        allowed = {"/api/dashboard", "/api/export", "/api/server-health",
                   "/api/alert-automation", "/api/snapshots",
                   "/api/share", "/api/multi-server", "/api/profiles",
                   "/api/ui-theme"}
        if path not in allowed:
            self._drain_request_body()
            self._send_error_json(HTTPStatus.NOT_FOUND, "Not found.")
            return

        try:
            payload = self._read_json_body()
            if path == "/api/ui-theme":
                # Every dashboard load re-posts its theme, so only a real
                # change is worth an audit line.
                before = load_ui_theme() or "default"
                theme = save_ui_theme(payload.get("theme"))
                if theme != before:
                    self._audit("Theme changed", item="appearance", field="theme", old=before, new=theme)
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
                    self._audit("Snapshot deleted", item=date_str, old="kept", new="deleted")
                    self._send_json(HTTPStatus.OK, {"ok": True, "snapshots": summaries})
                    return
                if snap_action == "annotate":
                    date_str = str(payload.get("date") or "").strip()
                    note = str(payload.get("note") or "").strip()
                    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str):
                        raise BadRequest("Invalid date format. Use YYYY-MM-DD.")
                    with SNAPSHOTS_LOCK:
                        annotate_snapshot(date_str, note)
                    self._audit("Snapshot note changed", item=date_str, field="note", new=note)
                    self._send_json(HTTPStatus.OK, {"ok": True})
                    return
                if snap_action == "auto-config":
                    enabled = bool(payload.get("enabled", False))
                    was_enabled = load_auto_snapshot_config()
                    save_auto_snapshot_config(enabled)
                    if was_enabled != enabled:
                        self._audit("Auto-snapshot " + ("on" if enabled else "off"), item="snapshots",
                                    field="enabled", old="on" if was_enabled else "off", new="on" if enabled else "off")
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
                self._audit("Snapshot saved", item=record.get("date", ""))
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
                        # Encrypts typed passwords; keeps stored ones only while
                        # the profile still targets the same connection.
                        existed = name in profiles
                        profiles[name] = profile_record_for_save(name, data, profiles.get(name, {}))
                        save_profiles(profiles)
                        self._audit("Profile saved", item=name, field="server",
                                    new=str(profiles[name].get("restApiHost") or ""), old="" if existed else "(new)")
                        self._send_json(HTTPStatus.OK, {"ok": True, "profiles": _mask_profiles(profiles)})
                    elif action == "delete":
                        name = str(payload.get("name") or "").strip()
                        removed = profiles.pop(name, None) or {}
                        save_profiles(profiles)
                        # Deleting the profile must also destroy the copy of its
                        # password that any session it established left behind.
                        purged = purge_sessions_for_credential(
                            str(removed.get("restApiHost") or ""),
                            str(removed.get("username") or ""),
                        )
                        if removed:
                            self._audit("Profile deleted", item=name, old=str(removed.get("restApiHost") or ""), new="deleted")
                        self._send_json(HTTPStatus.OK, {
                            "ok": True,
                            "profiles": _mask_profiles(profiles),
                            "sessionsPurged": purged["sessions"],
                            "automationsHoldingCredential": purged["automations"],
                        })
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
                    self._audit("Share link created", item=f"{token[:4]}…{token[-4:]}", field="expires", new="in 24 hours")
                    self._send_json(HTTPStatus.OK, {"ok": True, "token": token})
                elif action == "revoke":
                    token = str(payload.get("token") or "").strip()
                    if revoke_share_token(token):
                        self._audit("Share link revoked", item=f"{token[:4]}…{token[-4:]}", old="active", new="revoked")
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
                action = str(payload.get("action") or "").strip().lower()
                if status < 400 and action in _AUDITED_AUTOMATION_ACTIONS:
                    item = (payload.get("profileName") or payload.get("scheduleType")
                            or str(payload.get("automationId") or "")[:12] or "email")
                    detail = ("on" if payload.get("enabled") else "off") if "enabled" in payload else ""
                    self._audit(_AUDITED_AUTOMATION_ACTIONS[action], item=item, new=detail)
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
            status, body = upstream_auth_failure(status, body)

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
            self._drain_request_body()
            self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        except json.JSONDecodeError:
            self._drain_request_body()
            self._send_error_json(HTTPStatus.BAD_REQUEST, "Request body must be valid JSON.")
        except Exception:
            ref = getattr(self, "request_id", "-")
            LOG.exception("do_POST unhandled error", extra={"request_id": ref})
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"Internal error (ref {ref}).")




class ExclusiveThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True
    # Set by run(). The LISTENING socket stays plain TCP: wrapping it made
    # accept() run each TLS handshake on the serve_forever thread, so one client
    # that connected and never sent a ClientHello blocked every new connection.
    # Each accepted socket is wrapped instead, and handshakes in its worker.
    ssl_context: ssl.SSLContext | None = None

    def __init__(self, *args: Any, max_connections: int = DEFAULT_MAX_CONNECTIONS, **kwargs: Any) -> None:
        self._conn_semaphore = threading.BoundedSemaphore(max(1, int(max_connections)))
        super().__init__(*args, **kwargs)

    def get_request(self) -> tuple[Any, Any]:
        request, client_address = super().get_request()
        if self.ssl_context is not None:
            # No I/O here: the handshake is deferred to the worker thread.
            request = self.ssl_context.wrap_socket(request, server_side=True, do_handshake_on_connect=False)
        return request, client_address

    def _tls_handshake(self, request: Any, client_address: Any) -> bool:
        """Complete a deferred handshake, bounded by the request timeout. A failed
        or stalled handshake drops only this connection."""
        request.settimeout(_cfg.REQUEST_TIMEOUT_SECONDS)
        try:
            request.do_handshake()
        except (OSError, ValueError) as exc:  # SSLError and timeouts are OSErrors
            LOG.debug(f"TLS handshake with {client_address} failed: {exc}")
            self.shutdown_request(request)
            return False
        return True

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
            if isinstance(request, ssl.SSLSocket) and not self._tls_handshake(request, client_address):
                return
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
            # socketserver calls handle_error from inside its except block, so
            # exc is the in-flight exception; pass it rather than exc_info=True.
            LOG.error(f"request error from {client_address}: {exc}", extra={"client": client_address[0] if client_address else "-"}, exc_info=exc)


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
) -> tuple[ExclusiveThreadingHTTPServer, int, bool]:
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


def listener_trust_context(cert_path: Path) -> ssl.SSLContext:
    """Pin our own serving certificate file as the sole trust anchor. The CN
    rarely matches the probe host (localhost/IP), so hostname checking stays
    off — chain verification against the exact cert is the point."""
    context = ssl.create_default_context(cafile=str(cert_path))
    context.check_hostname = False
    # A CA-issued file holds the server certificate and intermediates but not
    # the root: without PARTIAL_CHAIN (off by default before Python 3.13)
    # OpenSSL wants a self-signed root and fails. STRICT (on from 3.13) judges
    # the CA's extensions, which is not what this check is for.
    context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return context


def self_test_dashboard_listener(
    url: str, timeout_seconds: float = 8.0, cert_path: Path | None = None
) -> tuple[bool, str]:
    health_url = f"{url.rstrip('/')}/api/health"
    deadline = time.monotonic() + max(0.5, timeout_seconds)
    if cert_path and cert_path.exists():
        tls_context = listener_trust_context(cert_path)
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
        # OSError covers URLError, TLS and timeouts; ValueError a non-JSON body;
        # AttributeError a JSON body that is not an object.
        except (OSError, ValueError, AttributeError, HTTPException) as exc:
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
    except (webbrowser.Error, OSError) as exc:
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
