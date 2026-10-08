"""Centralized runtime configuration and filesystem paths."""

from __future__ import annotations

import os
import re
import time
from contextvars import ContextVar
from pathlib import Path

# Environment-backed settings are kept in one module so feature modules can
# describe their own responsibilities without duplicating deployment logic.
ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.getenv("JBB_DATA_DIR", ROOT / "data"))
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "jubianbian.sqlite3"
FRONTEND_DIR = ROOT / "web-staging"
MAX_UPLOAD_BYTES = int(os.getenv("JBB_MAX_UPLOAD_MB", "500")) * 1024 * 1024
MAX_DURATION_MINUTES = int(os.getenv("JBB_MAX_DURATION_MINUTES", "6"))
MAX_DURATION_SECONDS = MAX_DURATION_MINUTES * 60
RATE_LIMIT_API_PER_MINUTE = max(1, int(os.getenv("JBB_RATE_LIMIT_API_PER_MINUTE", "120")))
RATE_LIMIT_CREATE_PER_WINDOW = max(1, int(os.getenv("JBB_RATE_LIMIT_CREATE_PER_WINDOW", "10")))
RATE_LIMIT_CREATE_WINDOW_SECONDS = max(60, int(os.getenv("JBB_RATE_LIMIT_CREATE_WINDOW_SECONDS", "600")))
WORKER_CONCURRENCY = max(1, min(10, int(os.getenv("JBB_WORKER_CONCURRENCY", "8"))))
QUEUE_MAX_WAIT_MINUTES = max(1, int(os.getenv("JBB_QUEUE_MAX_WAIT_MINUTES", "600")))
TRUST_PROXY_HEADERS = os.getenv("JBB_TRUST_PROXY_HEADERS", "0").strip().lower() in {"1", "true", "yes"}
JBB_ENVIRONMENT = os.getenv("JBB_ENVIRONMENT", "production").strip() or "production"
BUILD_VERSION = os.getenv("JBB_BUILD_VERSION", "8b3c16f")
JBB_LOG_LEVEL = os.getenv("JBB_LOG_LEVEL", "INFO").strip().upper() or "INFO"
_DEFAULT_CORS_ORIGINS = {
    "staging": "https://test.jubianbian.com",
    "production": "https://jubianbian.com,https://www.jubianbian.com",
}.get(JBB_ENVIRONMENT, "http://127.0.0.1:8000,http://localhost:8000")
CORS_ORIGINS = tuple(
    origin.strip().rstrip("/")
    for origin in os.getenv("JBB_CORS_ORIGINS", _DEFAULT_CORS_ORIGINS).split(",")
    if origin.strip()
)
ADMIN_EMAILS = {value.strip().lower() for value in os.getenv("JBB_ADMIN_EMAILS", "").split(",") if value.strip()}
JBB_LOG_MAX_BYTES = max(64 * 1024, int(os.getenv("JBB_LOG_MAX_BYTES", str(5 * 1024 * 1024))))
JBB_LOG_BACKUP_COUNT = max(1, int(os.getenv("JBB_LOG_BACKUP_COUNT", "5")))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
OPENAI_TRANSCRIPTION_MODEL = os.getenv("OPENAI_TRANSCRIPTION_MODEL", "whisper-1")
OPENAI_TEXT_MODEL = os.getenv("OPENAI_TEXT_MODEL", "gpt-4o-mini")
ARK_API_KEY = os.getenv("ARK_API_KEY", "").strip()
ARK_API_KEY_2 = os.getenv("ARK_API_KEY_2", "").strip()
ARK_API_KEYS = tuple(dict.fromkeys(key for key in (ARK_API_KEY, ARK_API_KEY_2) if key))
ARK_BASE_URL = os.getenv("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3").rstrip("/")
ARK_MODEL = os.getenv("ARK_MODEL", "doubao-seed-2-1-turbo-260628").strip()
ARK_LITE_MODEL = os.getenv("ARK_LITE_MODEL", "doubao-seed-2-1-lite-260915").strip()
ARK_TURBO_MODEL = os.getenv("ARK_TURBO_MODEL", ARK_MODEL).strip()
ARK_ROUTING_MODE = os.getenv("ARK_ROUTING_MODE", "complexity").strip().lower() or "complexity"
ARK_VIDEO_FPS = max(0.2, min(5.0, float(os.getenv("ARK_VIDEO_FPS", "0.5"))))
ARK_FILE_POLL_SECONDS = max(0.5, float(os.getenv("ARK_FILE_POLL_SECONDS", "1")))
ARK_FILE_POLL_TIMEOUT_SECONDS = max(30.0, float(os.getenv("ARK_FILE_POLL_TIMEOUT_SECONDS", "300")))
ARK_FALLBACK_ON_ERROR = os.getenv("ARK_FALLBACK_ON_ERROR", "1").strip().lower() in {"1", "true", "yes", "on"}
# Optional Ark billing fallback.  The Ark response is preferred when it
# contains a billed amount; these rates let staging calculate a cost from the
# recorded token usage when the response only exposes token counts.  Values
# are RMB per one million tokens and should match the configured model.
ARK_INPUT_TOKEN_PRICE_RMB_PER_MILLION = float(os.getenv("ARK_INPUT_TOKEN_PRICE_RMB_PER_MILLION", "0") or "0")
ARK_OUTPUT_TOKEN_PRICE_RMB_PER_MILLION = float(os.getenv("ARK_OUTPUT_TOKEN_PRICE_RMB_PER_MILLION", "0") or "0")
VOLCENGINE_ACCESS_KEY = os.getenv("VOLCENGINE_ACCESS_KEY", "").strip()
VOLCENGINE_SECRET_KEY = os.getenv("VOLCENGINE_SECRET_KEY", "").strip()
TENCENTCLOUD_SECRET_ID = os.getenv("TENCENTCLOUD_SECRET_ID", "").strip()
TENCENTCLOUD_SECRET_KEY = os.getenv("TENCENTCLOUD_SECRET_KEY", "").strip()
TENCENTCLOUD_REGION = os.getenv("TENCENTCLOUD_REGION", "ap-guangzhou").strip() or "ap-guangzhou"
TENCENTCLOUD_SES_ENDPOINT = os.getenv("TENCENTCLOUD_SES_ENDPOINT", "ses.tencentcloudapi.com").strip() or "ses.tencentcloudapi.com"
TENCENTCLOUD_SES_FROM_EMAIL = os.getenv("TENCENTCLOUD_SES_FROM_EMAIL", "").strip()
TENCENTCLOUD_SES_FROM_NAME = os.getenv("TENCENTCLOUD_SES_FROM_NAME", "剧编编").strip() or "剧编编"
TENCENTCLOUD_SES_TEMPLATE_ID = int(os.getenv("TENCENTCLOUD_SES_TEMPLATE_ID", "0") or "0")
AUTH_ALLOW_DEV_CODE = os.getenv(
    "JBB_AUTH_ALLOW_DEV_CODE",
    # Only an explicitly configured development environment may expose a
    # fallback verification code.  Staging is reachable from the internet and
    # must exercise the same real-email path as production by default.
    "1" if JBB_ENVIRONMENT in {"development", "dev", "local"} else "0",
).strip().lower() in {"1", "true", "yes", "on"}
SESSION_COOKIE = "jbb_session"
SESSION_TTL_SECONDS = 60 * 60 * 24 * 30
AUTH_CODE_TTL_SECONDS = 10 * 60
AUTH_CODE_RESEND_SECONDS = max(30, int(os.getenv("JBB_AUTH_CODE_RESEND_SECONDS", "60")))
AUTH_CODE_MAX_ATTEMPTS = max(3, int(os.getenv("JBB_AUTH_CODE_MAX_ATTEMPTS", "5")))
COOKIE_SECURE = os.getenv(
    "JBB_COOKIE_SECURE",
    # Compose explicitly enables Secure for the HTTPS deployments. Keep the
    # application default HTTP-friendly for the local uvicorn launch config.
    "0",
).strip().lower() in {"1", "true", "yes", "on"}
PASSWORD_MIN_LENGTH = 8
PASSWORD_LETTER_RE = re.compile(r"[A-Za-z]")
PASSWORD_DIGIT_RE = re.compile(r"[0-9]")

DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR = DATA_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
APP_STARTED_MONOTONIC = time.monotonic()
REQUEST_ID = ContextVar("request_id", default="-")
POINTS_PER_MINUTE = 5
POINTS_PER_YUAN = 13.8
REGISTER_POINTS = 25
