"""Shared domain exceptions used by recognition and persistence layers."""

from __future__ import annotations


class ArkError(RuntimeError):
    """A provider error with a user-safe message (never includes the API key)."""

    def __init__(self, message: str, *, status_code: int | None = None, provider_code: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.provider_code = provider_code
