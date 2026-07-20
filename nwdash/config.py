"""Constants, paths, patterns, logging, and mutable runtime flags.

Split from networker_dashboard.py (v2.5.0); behavior unchanged.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

APP_NAME = "NetWorker Backup & Recovery Dashboard"
APP_VERSION = "2.11.0"
APP_DEBUG = False
DEFAULT_PORT = 8443
DEFAULT_API_PORT = 9090
DEFAULT_TIMEOUT_SECONDS = 30  # outbound NetWorker REST/API call timeout
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30  # inbound per-request socket timeout (slowloris guard)
DEFAULT_MAX_CONNECTIONS = 200
REQUEST_TIMEOUT_SECONDS = DEFAULT_REQUEST_TIMEOUT_SECONDS
MAX_CONNECTIONS = DEFAULT_MAX_CONNECTIONS
SERVER_HEALTH_REFRESH_SECONDS = 60
MAX_POST_BYTES = 128 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
# NetWorker has no server-side time filter for /global/jobs, so the whole jobs
# database (bounded only by NetWorker's completed-job retention) is returned and
# trimmed to the report window client-side. On busy servers that easily exceeds
# the default response guard, so the jobs-history fetch gets a higher ceiling.
MAX_JOBS_RESPONSE_BYTES = 64 * 1024 * 1024
TABLE_LIMIT = 80
HOST_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]+$")
TIME_HHMM_PATTERN = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
API_VERSION_PATTERN = re.compile(r"^v[0-9]+$")
API_VERSION_CANDIDATES = ("v3", "v2", "v1")
REPORT_RANGES = {
    "24h": ("Last 24 Hours", 1),
    "7d": ("Last Week", 7),
    "30d": ("Last Month", 30),
}
THEME_PALETTES: dict[str, dict[str, str]] = {
    "default": {
        "bg": "#eef3f6",
        "surface": "#ffffff",
        "surface2": "#f7fafb",
        "ink": "#172026",
        "muted": "#5f6d76",
        "line": "#d7e1e7",
        "brand": "#126e82",
        "brandInk": "#ffffff",
        "green": "#18764a",
        "red": "#bd2b3a",
        "amber": "#a96800",
        "blue": "#2457a6",
    },
    "midnight": {
        "bg": "#101719",
        "surface": "#172124",
        "surface2": "#1e2b2f",
        "ink": "#edf6f8",
        "muted": "#9db1b8",
        "line": "#314249",
        "brand": "#2aa6b8",
        "brandInk": "#071113",
        "green": "#6fcf97",
        "red": "#ff6b78",
        "amber": "#f2c14e",
        "blue": "#7db7ff",
    },
    "graphite": {"bg": "#f1f2f3", "surface": "#ffffff", "surface2": "#eff1f2", "ink": "#1f2326", "muted": "#666f75", "line": "#d1d6da", "brand": "#3d5a5f", "brandInk": "#ffffff", "green": "#1d7c55", "red": "#b93246", "amber": "#ad7300", "blue": "#3d64a8"},
    "contrast": {"bg": "#ffffff", "surface": "#ffffff", "surface2": "#f2f2f2", "ink": "#0b0b0b", "muted": "#3d3d3d", "line": "#202020", "brand": "#005fcc", "brandInk": "#ffffff", "green": "#006b3c", "red": "#b00020", "amber": "#8a5a00", "blue": "#004fb8"},
    "ocean": {"bg": "#e8f4f6", "surface": "#ffffff", "surface2": "#edf8fa", "ink": "#102a31", "muted": "#527179", "line": "#bfd8de", "brand": "#087f8c", "brandInk": "#ffffff", "green": "#11845b", "red": "#c03546", "amber": "#b27900", "blue": "#1c6eb8"},
    "forest": {"bg": "#eef5ef", "surface": "#ffffff", "surface2": "#f2f8f1", "ink": "#17251b", "muted": "#5f7565", "line": "#cfddcf", "brand": "#2f6f45", "brandInk": "#ffffff", "green": "#1f7a45", "red": "#b83b4b", "amber": "#a06c00", "blue": "#3867a8"},
    "ruby": {"bg": "#f8eef1", "surface": "#ffffff", "surface2": "#fff4f6", "ink": "#2d1720", "muted": "#7a5d66", "line": "#e6cbd3", "brand": "#9f2d55", "brandInk": "#ffffff", "green": "#17794e", "red": "#b92345", "amber": "#aa7200", "blue": "#445ca8"},
    "steel": {"bg": "#edf1f5", "surface": "#ffffff", "surface2": "#f3f6f9", "ink": "#17202b", "muted": "#5d6a78", "line": "#ccd6e1", "brand": "#425c78", "brandInk": "#ffffff", "green": "#26724a", "red": "#aa3d45", "amber": "#9a6b12", "blue": "#376da9"},
    "arctic": {"bg": "#edf7f8", "surface": "#ffffff", "surface2": "#f4fbfb", "ink": "#10272d", "muted": "#5a737a", "line": "#c8dee3", "brand": "#0d7891", "brandInk": "#ffffff", "green": "#168059", "red": "#b83245", "amber": "#9e7207", "blue": "#2d68a7"},
    "citrus": {"bg": "#f5f7ec", "surface": "#ffffff", "surface2": "#fbfcf3", "ink": "#202817", "muted": "#68705b", "line": "#dde5ca", "brand": "#617d18", "brandInk": "#ffffff", "green": "#23733f", "red": "#b43a47", "amber": "#a16d00", "blue": "#3f6fa5"},
    "harbor": {"bg": "#eef3f4", "surface": "#ffffff", "surface2": "#f5f8f9", "ink": "#17242a", "muted": "#5e7077", "line": "#d0dce0", "brand": "#235f73", "brandInk": "#ffffff", "green": "#24764f", "red": "#b63548", "amber": "#9d6e08", "blue": "#335fa3"},
    "ember": {"bg": "#f6f1ee", "surface": "#ffffff", "surface2": "#fbf7f4", "ink": "#2a1f1a", "muted": "#75665f", "line": "#e2d4cd", "brand": "#8d4a36", "brandInk": "#ffffff", "green": "#26734a", "red": "#b23545", "amber": "#9b6a10", "blue": "#3c67a2"},
    "violet": {"bg": "#f2effa", "surface": "#ffffff", "surface2": "#f7f4fc", "ink": "#221a33", "muted": "#6b6280", "line": "#ddd3ec", "brand": "#6d3fbf", "brandInk": "#ffffff", "green": "#1f7a4d", "red": "#b93049", "amber": "#9c6b00", "blue": "#4a5fb0"},
    "sandstone": {"bg": "#f5efe4", "surface": "#fffdf8", "surface2": "#faf5ea", "ink": "#2e2417", "muted": "#7a6c58", "line": "#e3d8c4", "brand": "#8a6d3b", "brandInk": "#ffffff", "green": "#2b7442", "red": "#b03a3f", "amber": "#996a00", "blue": "#4a6ba0"},
    "carbon": {"bg": "#000000", "surface": "#0d0d0d", "surface2": "#161616", "ink": "#f2f2f2", "muted": "#9a9a9a", "line": "#2a2a2a", "brand": "#4dd0e1", "brandInk": "#001114", "green": "#4ade80", "red": "#ff6b6b", "amber": "#fbbf24", "blue": "#60a5fa"},
}
# The dashboard brand/header card uses a fixed navy->teal gradient over the
# brand color (see the in-page .brand-card CSS). Email reports and the PNG
# snapshot reuse these so the brand card looks identical to the live dashboard.
# Outlook ignores CSS gradients, so the email also carries a solid fallback.
BRAND_CARD_GRADIENT = "linear-gradient(135deg, rgba(11, 32, 42, 0.98), rgba(18, 110, 130, 0.96)), #126e82"
BRAND_CARD_SOLID = "#103a47"
BRAND_CARD_INK = "#ffffff"
CUSTOM_REPORT_RANGE = "custom"
DEFAULT_REPORT_RANGE = "24h"
SESSION_TTL_SECONDS = 365 * 24 * 60 * 60  # 1 year — sessions persist until server restart or explicit clear
SHARED_REFRESH_SECONDS = 60
ALERT_AUTOMATION_MIN_INTERVAL_MINUTES = 1
ALERT_AUTOMATION_MAX_INTERVAL_MINUTES = 1440
APP_BASE_DIR = Path(sys.executable).parent.resolve() if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
DATA_DIR = APP_BASE_DIR / "data"
DASHBOARD_SNAPSHOT_FILE = DATA_DIR / "networker_snapshots.json"
AUTO_SNAPSHOT_FILE = DATA_DIR / "auto_snapshot_config.json"
UI_PREFS_FILE = DATA_DIR / "ui_prefs.json"
SESSION_KEY_FILE = DATA_DIR / ".session_key"
SESSION_PERSISTENCE_FILE = DATA_DIR / "sessions.json"
AUTOMATIONS_FILE = DATA_DIR / "automations.json"
LAST_GOOD_DASHBOARD_FILE = DATA_DIR / "last_good_dashboard.json"
PROFILES_FILE = DATA_DIR / "profiles.json"
EMAIL_CONFIG_FILE = DATA_DIR / "email_config.json"
REPORT_CACHE_DIR = DATA_DIR / "report_cache"
DISPLAY_TOKEN_FILE = DATA_DIR / "display_token.json"
DISPLAY_CONNECTION_FILE = DATA_DIR / "display_connection.json"
REPORT_GROUPS_FILE = DATA_DIR / "report_groups.json"
EMAIL_PROFILES_FILE = DATA_DIR / "email_profiles.json"
AUTH_KEY_FILE = DATA_DIR / ".auth_key"
AUTH_CONFIG_FILE = DATA_DIR / "auth.json"
COOKIE_NAME = "nwdash_auth"
AUTH_TTL_SECONDS = 43200  # 12 hours
PBKDF2_ITERATIONS = 200_000
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 300
AUTH_ENABLED = False  # set in run() once a password is configured

# ── Logging ────────────────────────────────────────────────────────────────
LOG_DIR = APP_BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "networker_dashboard.log"
PROCESS_START_TIME = time.time()
LOG = logging.getLogger("networker_dashboard")
_LOG_EXTRA_KEYS = ("request_id", "client", "status", "path", "event")


class _JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        obj = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in _LOG_EXTRA_KEYS:
            val = getattr(record, key, None)
            if val is not None:
                obj[key] = val
        if record.exc_info:
            obj["exc"] = self.formatException(record.exc_info)
        return json.dumps(obj, ensure_ascii=True, default=str)


def configure_logging(debug: bool) -> None:
    LOG.setLevel(logging.DEBUG if debug else logging.INFO)
    for handler in list(LOG.handlers):
        LOG.removeHandler(handler)
    formatter = _JsonLogFormatter()
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        LOG.addHandler(file_handler)
    except OSError:
        pass
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    LOG.addHandler(stream_handler)
    LOG.propagate = False


_PROFILE_PW_SENTINEL = "__profile_password__"
_PROFILE_PW_SAVED    = "(saved)"


def safe_log_text(value: Any, max_len: int = 600) -> str:
    text = str(value if value is not None else "")
    text = re.sub(r"[\r\n\t]+", " ", text)
    if len(text) > max_len:
        return text[: max_len - 3] + "..."
    return text


def debug_log(message: str) -> None:
    LOG.debug(safe_log_text(message, 520))
# ── Mutable runtime flags ────────────────────────────────────────────────────
# Relocated here from their original single-file locations so every module can
# read the live value via `from . import config as _cfg` (run() mutates these).
DEFAULT_MAX_SSE_CLIENTS = 50
MAX_SSE_CLIENTS = DEFAULT_MAX_SSE_CLIENTS
ALLOWLIST_ENABLED = False
