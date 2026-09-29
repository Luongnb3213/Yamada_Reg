from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path


if getattr(sys, "frozen", False):
    _exe_dir = Path(sys.executable).parent
    BUNDLE_DIR = Path(sys._MEIPASS)
    if _exe_dir.name == "MacOS" and _exe_dir.parent.name == "Contents":
        ROOT_DIR = _exe_dir.parent.parent.parent
    else:
        ROOT_DIR = _exe_dir
else:
    ROOT_DIR = Path(__file__).resolve().parent.parent
    BUNDLE_DIR = ROOT_DIR


def _extract_if_missing(rel_path: str) -> None:
    dest = ROOT_DIR / rel_path
    src = BUNDLE_DIR / rel_path
    if not dest.exists() and src.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(dest))


if getattr(sys, "frozen", False):
    _extract_if_missing("config.json")


config_name = os.environ.get("CONFIG_FILE", "config.json")
CONFIG_FILE = ROOT_DIR / config_name
if CONFIG_FILE.exists():
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        _cfg = json.load(f)
else:
    _cfg = {}


def _get(key: str, default=None):
    return _cfg.get(key, default)


def _as_int(key: str, default: int, minimum: int | None = None) -> int:
    try:
        value = int(_get(key, default))
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


def _as_float(key: str, default: float, minimum: float | None = None) -> float:
    try:
        value = float(_get(key, default))
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    return value


APP_NAME = str(_get("app_name", "Yamada_Reg") or "Yamada_Reg")

DATA_DIR = ROOT_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
LOG_DIR = ROOT_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

# Runtime
BROWSER_PATH = str(_get("browser_path", "") or "")
HEADLESS = bool(_get("headless", False))
KEEP_BROWSER_OPEN = bool(_get("keep_browser_open", False))
XLSX_PATH = str(_get("xlsx_path", "") or "")
ACTIVE_SHEET = str(_get("active_sheet", "Iclouds") or "Iclouds")
WORKER_COUNT = _as_int("worker_count", 1, minimum=1)
RUN_LIMIT = _as_int("run_limit", 0, minimum=0)
ACCOUNT_COOLDOWN_SECONDS = _as_int("account_cooldown_seconds", 0, minimum=0)

# Generic account defaults
DEFAULT_PASSWORD = str(_get("default_password", "") or "")
DEFAULT_PREFECTURE = str(_get("default_prefecture", "東京都") or "東京都")
DOB_YEAR_MIN = _as_int("dob_year_min", 1995, minimum=1900)
DOB_YEAR_MAX = _as_int("dob_year_max", 2007, minimum=1900)

# Email
EMAIL_OTP_TIMEOUT = _as_int("email_otp_timeout", 120, minimum=1)
EMAIL_CLOCK_SKEW_MARGIN = _as_int("email_clock_skew_margin", 180, minimum=0)
EMAIL_MODE = str(_get("email_mode", "alias") or "alias")
IMAP_HOST = str(_get("imap_host", "") or "").strip()
CATCHALL_INBOX = str(_get("catchall_inbox", "") or "").strip()
CATCHALL_PASSWORD = str(_get("catchall_password", "") or "").strip()
CATCHALL_DOMAIN = str(_get("catchall_domain", "") or "").strip()
CATCHALL_EMAIL_PREFIX = str(_get("catchall_email_prefix", "acc") or "acc")

# Proxy
USE_PROXY = bool(_get("use_proxy", True))
MAX_ACCOUNTS_PER_PROXY = _as_int("max_accounts_per_proxy", 1, minimum=1)
PROXY_HEALTH_URL = str(_get("proxy_health_url", "https://example.com/") or "https://example.com/")
PROXY_HEALTH_TIMEOUT = _as_int("proxy_health_timeout", 10, minimum=5)
PROXY_FAILURE_THRESHOLD = _as_int("proxy_failure_threshold", 3, minimum=1)
PROXY_CIRCUIT_OPEN = False
PROXY_CIRCUIT_REASON = ""

# SMS / phone inventory
SMS_ENABLED = bool(_get("sms_enabled", False))
SMS_BASE_URL = str(_get("sms_base_url", "") or "").rstrip("/")
SMS_API_KEY = str(_get("sms_api_key", "") or "")
SMS_USERNAME = str(_get("sms_username", "") or "")
SMS_PASSWORD = str(_get("sms_password", "") or "")
SMS_SERVICE_ID = str(_get("sms_service_id", "") or "")
SMS_COUNTRY = str(_get("sms_country", "jpn") or "jpn")
SMS_SERVER = str(_get("sms_server", "2") or "2")
SMS_OTP_TIMEOUT = _as_int("sms_otp_timeout", 300, minimum=10)

USE_PRE_FETCHED_NUMBERS = bool(_get("use_pre_fetched_numbers", False))
USE_MANUAL_PHONE_LIST = bool(_get("use_manual_phone_list", False))
PHONE_SOURCE = str(
    _get(
        "phone_source",
        "manual" if USE_MANUAL_PHONE_LIST else "prefetched",
    )
).strip().lower()
if PHONE_SOURCE not in ("prefetched", "manual"):
    PHONE_SOURCE = "manual" if USE_MANUAL_PHONE_LIST else "prefetched"

OTP_WEB_ENABLED = bool(_get("otp_web_enabled", False))
OTP_WEB_URL = str(_get("otp_web_url", "https://www.otpbase.space") or "").rstrip("/")
OTP_WEB_API_KEY = str(_get("otp_web_api_key", "") or "").strip()
OTP_WEB_POLL_INTERVAL = _as_float("otp_web_poll_interval", 2.0, minimum=0.5)
OTP_WEB_TIMEOUT = _as_int("otp_web_timeout", SMS_OTP_TIMEOUT, minimum=10)
OTP_WEB_MANUAL_FALLBACK = bool(_get("otp_web_manual_fallback", True))
OTP_SOURCE_KEYWORDS = tuple(
    part.strip()
    for part in str(_get("otp_source_keywords", "") or "").split(",")
    if part.strip()
)

# Shared stop/state flags
STOP_FLAG = False
ACTIVE_BROWSERS = []
SESSION_STATS = {
    "PENDING": 0,
    "PROCESSING": 0,
    "SUCCESS": 0,
    "FAILED": 0,
}
