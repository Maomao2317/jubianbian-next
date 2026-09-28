"""HTTP middleware: request correlation logging and lightweight rate limits."""

from __future__ import annotations

from collections import deque
from threading import Lock
import time
import uuid
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

from .config import (
    MAX_UPLOAD_BYTES,
    RATE_LIMIT_API_PER_MINUTE,
    RATE_LIMIT_CREATE_PER_WINDOW,
    RATE_LIMIT_CREATE_WINDOW_SECONDS,
    REQUEST_ID,
    TRUST_PROXY_HEADERS,
)
from .logging_setup import logger, safe_error_text

class RequestLogMiddleware(BaseHTTPMiddleware):
    """Emit one request record with a correlation id and elapsed time."""

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        token = REQUEST_ID.set(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception as exc:  # pragma: no cover - defensive middleware boundary
            elapsed_ms = (time.perf_counter() - started) * 1000
            logger.exception(
                "request_failed request_id=%s method=%s path=%s duration_ms=%.1f error=%s",
                request_id,
                request.method,
                request.url.path,
                elapsed_ms,
                safe_error_text(exc),
            )
            raise
        finally:
            REQUEST_ID.reset(token)
        elapsed_ms = (time.perf_counter() - started) * 1000
        response.headers["X-Request-ID"] = request_id
        message = "request_complete request_id=%s method=%s path=%s status=%s duration_ms=%.1f"
        if request.url.path == "/api/health":
            logger.debug(message, request_id, request.method, request.url.path, response.status_code, elapsed_ms)
        else:
            logger.info(message, request_id, request.method, request.url.path, response.status_code, elapsed_ms)
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Small single-process limiter for the public test deployment.

    It is intentionally conservative and dependency-free. A reverse proxy or
    shared Redis limiter should replace this when the service becomes multi-
    process or multi-instance.
    """

    def __init__(self, application: Any) -> None:
        super().__init__(application)
        self._lock = Lock()
        self._buckets: dict[tuple[str, str], deque[float]] = {}

    @staticmethod
    def _client_ip(request: Request) -> str:
        if TRUST_PROXY_HEADERS:
            forwarded = request.headers.get("x-forwarded-for", "")
            if forwarded:
                return forwarded.split(",", 1)[0].strip() or "unknown"
        return request.client.host if request.client else "unknown"

    def _check(self, bucket_name: str, client_ip: str, limit: int, window: int) -> tuple[bool, int]:
        now = time.monotonic()
        key = (bucket_name, client_ip)
        with self._lock:
            bucket = self._buckets.setdefault(key, deque())
            cutoff = now - window
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                retry_after = max(1, int(bucket[0] + window - now))
                return False, retry_after
            bucket.append(now)
            # Keep the in-memory map bounded after long-running idle periods.
            if len(self._buckets) > 5000:
                self._buckets = {
                    item_key: item_bucket
                    for item_key, item_bucket in self._buckets.items()
                    if item_bucket and item_bucket[-1] > now - window
                }
        return True, 0

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        path = request.url.path
        if not path.startswith("/api/") or path == "/api/health":
            return await call_next(request)

        client_ip = self._client_ip(request)
        if request.method == "POST" and path == "/api/tasks":
            content_length = request.headers.get("content-length")
            if content_length and int(content_length) > MAX_UPLOAD_BYTES + 2 * 1024 * 1024:
                return JSONResponse(
                    {"detail": f"文件超过 {MAX_UPLOAD_BYTES // (1024 * 1024)} MB"},
                    status_code=413,
                )
            allowed, retry_after = self._check(
                "create", client_ip, RATE_LIMIT_CREATE_PER_WINDOW, RATE_LIMIT_CREATE_WINDOW_SECONDS
            )
            limit = RATE_LIMIT_CREATE_PER_WINDOW
            window = RATE_LIMIT_CREATE_WINDOW_SECONDS
        else:
            allowed, retry_after = self._check("api", client_ip, RATE_LIMIT_API_PER_MINUTE, 60)
            limit = RATE_LIMIT_API_PER_MINUTE
            window = 60

        if not allowed:
            return JSONResponse(
                {"detail": "请求过于频繁，请稍后再试"},
                status_code=429,
                headers={"Retry-After": str(retry_after), "X-RateLimit-Limit": str(limit)},
            )

        response = await call_next(request)
        response.headers.setdefault("X-RateLimit-Limit", str(limit))
        response.headers.setdefault("X-RateLimit-Window", str(window))
        return response
