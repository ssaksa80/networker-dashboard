"""Argument parsing and application entry point.

Split from networker_dashboard.py (v2.5.0); behavior unchanged.
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import signal
import ssl
import sys
import threading
from pathlib import Path
from typing import Any

from . import config as _cfg
from .config import (
    APP_NAME,
    APP_VERSION,
    DEFAULT_MAX_CONNECTIONS,
    DEFAULT_MAX_SSE_CLIENTS,
    DEFAULT_PORT,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    LOG,
    configure_logging,
)
from .auth import _is_loopback_bind, auth_password_configured, set_auth_password
from .certs import ensure_certificate
from .models import (
    SHARED_DASHBOARD_LOCK,
    SHARED_DASHBOARD_STATE,
    SHARED_REFRESH_STOP,
    _automation_keys_snapshot,
    _session_ids_snapshot,
    shared_dashboard_refresh_loop,
)
from .profiles import ALLOWED_HOST_NAMES, ALLOWED_NETWORKS, configure_allowed_hosts
from .sessions import restore_sessions_from_disk
from .snapshots import auto_snapshot_worker
from .emailer import cancel_alert_automation
from .report_jobs import restore_jobs_from_disk, scheduler_loop as report_scheduler_loop
from .report_api import handle_report_jobs, _smtp_config as _report_smtp_config
from .server import (
    DashboardHandler,
    auto_launch_dashboard,
    bind_dashboard_server,
    port_self_test_script_block,
    preferred_launch_url,
    self_test_dashboard_listener,
    service_access_urls,
)

def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument(
        "--bind",
        default="0.0.0.0",
        help="Interface to bind. Defaults to 0.0.0.0 (all interfaces, reachable on the server IP for publishing). Use 127.0.0.1 to restrict to local only.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help="HTTPS port to listen on. If busy, an available random port is selected.",
    )
    parser.add_argument(
        "--cert",
        default=".certs/networker_dashboard.crt",
        help="TLS certificate path in PEM format.",
    )
    parser.add_argument(
        "--key",
        default=".certs/networker_dashboard.key",
        help="TLS private key path in PEM format.",
    )
    parser.add_argument(
        "--no-auto-cert",
        action="store_true",
        help="Require an existing certificate/key instead of writing the embedded development certificate.",
    )
    parser.add_argument(
        "--no-launch",
        action="store_true",
        help="Do not automatically open the dashboard HTML page in the default browser after startup self-test.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable verbose REST API diagnostics without logging passwords or Authorization headers.",
    )
    parser.add_argument(
        "--request-timeout",
        type=int,
        default=DEFAULT_REQUEST_TIMEOUT_SECONDS,
        help="Per-request socket timeout in seconds. Drops idle/slowloris connections.",
    )
    parser.add_argument(
        "--max-connections",
        type=int,
        default=DEFAULT_MAX_CONNECTIONS,
        help="Maximum concurrent connections before new ones are refused.",
    )
    parser.add_argument(
        "--max-sse",
        type=int,
        default=DEFAULT_MAX_SSE_CLIENTS,
        help="Maximum concurrent live-stream (SSE) viewers.",
    )
    parser.add_argument(
        "--auth-password",
        default="",
        help="Dashboard access password. May also be supplied via the DASHBOARD_AUTH_PASSWORD environment variable. Stored only as a salted hash.",
    )
    parser.add_argument(
        "--allowed-hosts",
        default="",
        help="Comma-separated allow-list of NetWorker hosts/IPs/CIDRs the server may connect to (e.g. nw1.corp.local,10.0.0.0/24). May also be set via DASHBOARD_ALLOWED_HOSTS. If unset, any host is permitted.",
    )
    return parser.parse_args(argv)


def report_cfg_provider() -> dict:
    """Fresh SMTP + ops-address config each fire (so admin edits take effect)."""
    smtp, smtp_password = _report_smtp_config()
    ops = smtp.get("opsAlertAddress", "") if isinstance(smtp, dict) else ""
    return {"smtp": smtp, "smtp_password": smtp_password, "ops_address": ops}


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    _cfg.APP_DEBUG = bool(args.debug)
    configure_logging(_cfg.APP_DEBUG)
    LOG.info(f"{APP_NAME} {APP_VERSION} starting", extra={"event": "startup"})
    _cfg.REQUEST_TIMEOUT_SECONDS = max(5, int(args.request_timeout))
    _cfg.MAX_CONNECTIONS = max(1, int(args.max_connections))
    _cfg.MAX_SSE_CLIENTS = max(1, int(args.max_sse))
    DashboardHandler.timeout = _cfg.REQUEST_TIMEOUT_SECONDS
    auth_password = args.auth_password or os.environ.get("DASHBOARD_AUTH_PASSWORD") or ""
    if auth_password:
        set_auth_password(auth_password)
    _cfg.AUTH_ENABLED = auth_password_configured()
    configure_allowed_hosts(args.allowed_hosts or os.environ.get("DASHBOARD_ALLOWED_HOSTS") or "")
    cert_path = Path(args.cert).expanduser().resolve()
    key_path = Path(args.key).expanduser().resolve()

    used_embedded_cert = False
    if args.no_auto_cert:
        if not cert_path.exists() or not key_path.exists():
            raise SystemExit("Certificate/key missing and --no-auto-cert was provided.")
    else:
        used_embedded_cert = ensure_certificate(cert_path, key_path)

    if not 0 <= int(args.port) <= 65535:
        raise SystemExit("HTTPS port must be between 0 and 65535.")

    print(port_self_test_script_block(args.bind, int(args.port)))
    server, selected_port, used_random_port = bind_dashboard_server(
        args.bind, int(args.port), max_connections=_cfg.MAX_CONNECTIONS
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    access_urls = service_access_urls(args.bind, selected_port)
    launch_url = preferred_launch_url(access_urls)

    print(f"{APP_NAME} {APP_VERSION}")
    if int(args.port) == 0:
        print(f"Requested automatic random HTTPS port; selected {selected_port}.")
    elif used_random_port:
        print(f"Requested HTTPS port {args.port} is not available; selected {selected_port} instead.")
    else:
        print(f"Requested HTTPS port {selected_port} is available and bound.")
    print("Serving HTTPS only at:")
    for label, url in access_urls:
        print(f"  {label}: {url}")
    if used_embedded_cert:
        print(
            "Using the embedded self-signed development certificate. "
            "Use --cert and --key with --no-auto-cert for production."
        )
    if _cfg.APP_DEBUG:
        print("Debug logging is enabled. Passwords and Authorization headers are not logged.")
    print("Credentials are encrypted at rest in the data directory and are never stored in plaintext or placed in URLs.")
    if _cfg.ALLOWLIST_ENABLED:
        print(f"NetWorker host allow-list ENABLED ({len(ALLOWED_HOST_NAMES) + len(ALLOWED_NETWORKS)} entr(y/ies)).")
    else:
        print("NetWorker host allow-list is disabled (the server may connect to any host you enter). Set --allowed-hosts to restrict.")
    if _cfg.AUTH_ENABLED:
        print("Dashboard authentication is ENABLED (gateway password required).")
        LOG.info("authentication enabled", extra={"event": "auth"})
    elif not _is_loopback_bind(args.bind):
        print("=" * 72)
        print("WARNING: Bound to a non-loopback address with NO authentication.")
        print("Anyone who can reach this port can view all backup data.")
        print("Set DASHBOARD_AUTH_PASSWORD (or --auth-password), or bind to 127.0.0.1.")
        print("=" * 72)
        LOG.warning("exposed to non-loopback with no authentication", extra={"event": "auth"})
    else:
        print("Dashboard authentication is disabled (local loopback bind).")
        LOG.info("authentication disabled (loopback bind)", extra={"event": "auth"})

    def _handle_term(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    for _sig_name in ("SIGTERM", "SIGINT"):
        _sig = getattr(signal, _sig_name, None)
        if _sig is not None:
            try:
                signal.signal(_sig, _handle_term)
            except (ValueError, OSError, RuntimeError):
                pass

    server_thread = threading.Thread(
        target=server.serve_forever,
        name="networker-dashboard-https",
        daemon=True,
    )
    server_thread.start()
    SHARED_REFRESH_STOP.clear()
    shared_refresh_thread = threading.Thread(
        target=shared_dashboard_refresh_loop,
        name="networker-dashboard-shared-refresh",
        daemon=True,
    )
    shared_refresh_thread.start()

    # Restore persisted state from the previous run in background — non-blocking
    def _restore_sessions_bg() -> None:
        report_count = restore_jobs_from_disk()
        LOG.info(f"restored {report_count} scheduled report job(s)", extra={"event": "startup"})
        count = restore_sessions_from_disk()
        if count:
            print(f"Restored {count} dashboard session(s) from previous run.")
            # Re-prime shared dashboard state with first restored session
            with SHARED_DASHBOARD_LOCK:
                ids = _session_ids_snapshot()
                if not SHARED_DASHBOARD_STATE.get("sessionId") and ids:
                    SHARED_DASHBOARD_STATE["sessionId"] = ids[0]
        else:
            print("No previous sessions to restore (connect via browser to begin monitoring).")

    threading.Thread(target=_restore_sessions_bg, name="session-restore", daemon=True).start()
    threading.Thread(target=auto_snapshot_worker, name="auto-snapshot", daemon=True).start()
    threading.Thread(target=report_scheduler_loop, args=(report_cfg_provider,),
                     name="report-scheduler", daemon=True).start()

    try:
        self_test_ok, self_test_message = self_test_dashboard_listener(launch_url, cert_path=cert_path)
        print(f"Startup self-test: {self_test_message}")
        if not self_test_ok:
            raise SystemExit("Dashboard HTTPS listener did not pass startup self-test.")

        launch_ok, launch_message = auto_launch_dashboard(launch_url, enabled=not args.no_launch)
        print(launch_message)

        while server_thread.is_alive():
            server_thread.join(1.0)
    except KeyboardInterrupt:
        print("\nStopping dashboard.")
    finally:
        try:
            for automation_id in _automation_keys_snapshot():
                cancel_alert_automation(automation_id)
            SHARED_REFRESH_STOP.set()
            if server_thread.is_alive():
                server.shutdown()
                server_thread.join(3.0)
            shared_refresh_thread.join(3.0)
            server.server_close()
        except Exception as exc:
            LOG.debug(f"shutdown cleanup failed: {exc}")
        # Daemon worker/background threads may still be mid-write to stderr (via
        # logging) when the interpreter finalizes — a normal shutdown can then
        # deadlock on the stderr buffer lock (Fatal Python error:
        # _enter_buffered_busy). Flush, then exit hard to skip finalization and
        # avoid that race.
        logging.shutdown()
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception as exc:
            LOG.debug(f"final stdout/stderr flush failed: {exc}")
        os._exit(0)
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(argv)
