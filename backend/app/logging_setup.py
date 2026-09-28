"""Application logging helpers and privacy-safe diagnostic utilities."""

from __future__ import annotations

import hashlib
import logging
from logging.handlers import RotatingFileHandler

from .config import (
    ARK_API_KEY,
    LOG_DIR,
    JBB_LOG_BACKUP_COUNT,
    JBB_LOG_LEVEL,
    JBB_LOG_MAX_BYTES,
    OPENAI_API_KEY,
    TENCENTCLOUD_SECRET_ID,
    TENCENTCLOUD_SECRET_KEY,
)

def _configure_logging() -> logging.Logger:
    logger = logging.getLogger("jubianbian")
    logger.setLevel(getattr(logging, JBB_LOG_LEVEL, logging.INFO))
    logger.propagate = False
    if logger.handlers:
        return logger
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    file_handler = RotatingFileHandler(
        LOG_DIR / "app.log",
        maxBytes=JBB_LOG_MAX_BYTES,
        backupCount=JBB_LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(stream)
    logger.addHandler(file_handler)
    return logger


logger = _configure_logging()


def safe_error_text(error: BaseException, limit: int = 1000) -> str:
    message = str(error) or error.__class__.__name__
    for secret in (
        ARK_API_KEY,
        OPENAI_API_KEY,
        TENCENTCLOUD_SECRET_ID,
        TENCENTCLOUD_SECRET_KEY,
    ):
        if secret:
            message = message.replace(secret, "[REDACTED]")
    return message[:limit]


def _email_log_id(email: str) -> str:
    """Return a stable, non-reversible identifier for auth logs."""
    normalized = email.strip().lower()
    local, _, domain = normalized.partition("@")
    local_hint = (local[:1] + "***") if local else "***"
    domain_hint = domain[:40] if domain else "invalid"
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:10]
    return f"{local_hint}@{domain_hint}#{digest}"
