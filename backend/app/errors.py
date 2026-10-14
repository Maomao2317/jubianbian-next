"""Shared domain exceptions used by recognition and persistence layers."""

from __future__ import annotations

import re


class ArkError(RuntimeError):
    """A provider error with a user-safe message (never includes the API key)."""

    def __init__(self, message: str, *, status_code: int | None = None, provider_code: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.provider_code = provider_code


def is_account_balance_error(error: BaseException) -> bool:
    """Identify explicit billing failures without treating every 403 as one."""
    code = re.sub(r"[^a-z0-9]", "", str(getattr(error, "provider_code", "")).casefold())
    if code == "accountbalanceexhausted":
        return True
    if getattr(error, "status_code", None) not in {402, 403}:
        return False
    if code in {"accountoverdue", "arrearage", "insufficientbalance", "accountinsufficientbalance"}:
        return True
    message = str(error).casefold()
    return any(marker in message for marker in (
        "overdue balance", "insufficient balance", "account overdue", "arrearage", "欠费", "余额不足",
    ))
