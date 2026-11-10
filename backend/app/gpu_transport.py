"""Private GPU HTTP transport with independent admission/upload/response budgets."""
from __future__ import annotations

import http.client
import io
import json
import logging
import select
import socket
import threading
import time
from contextlib import contextmanager
from urllib.parse import urlsplit
from . import config

logger = logging.getLogger("jubianbian")


class RemoteBusy(RuntimeError):
    """Healthy worker rejected admission before reading or processing the body."""


@contextmanager
def _deadline(sock, seconds):
    """Bound even slow-drip headers/bodies, not just individual socket reads."""
    expired = threading.Event()

    def expire():
        expired.set()
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    timer = threading.Timer(seconds, expire)
    timer.daemon = True
    timer.start()
    try:
        yield
        if expired.is_set():
            raise TimeoutError("GPU transport deadline exceeded")
    except Exception as exc:
        if expired.is_set():
            raise TimeoutError("GPU transport deadline exceeded") from exc
        raise
    finally:
        timer.cancel()


def _busy(response):
    return response.status in {429, 503} and response.getheader("X-GPU-Busy") == "1"


def post(url, token, endpoint, payload, size, headers, response_timeout):
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("Invalid private GPU endpoint")
    factory = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    started = time.monotonic()
    busy_deadline = started + config.MEDIA_TRANSFER_QUEUE_TIMEOUT_SECONDS
    stream = io.BytesIO(payload) if isinstance(payload, bytes) else payload
    attempt = 0
    while True:
        attempt += 1
        stream.seek(0)
        connection = factory(parts.hostname, parts.port, timeout=10)
        response = None
        try:
            connection.connect()
            upload_started = time.monotonic()
            with _deadline(connection.sock, 90):
                connection.sock.settimeout(15)
                connection.putrequest("POST", parts.path.rstrip("/") + endpoint)
                for key, value in {"Authorization": "Bearer " + token, "Content-Type": "application/octet-stream",
                                   "Content-Length": str(size), **headers}.items():
                    connection.putheader(key, value)
                connection.endheaders()
                sent = 0
                try:
                    while chunk := stream.read(256 * 1024):
                        if select.select([connection.sock], [], [], 0)[0]:
                            response = connection.getresponse()
                            if not _busy(response):
                                raise RuntimeError("GPU replied before accepting complete upload")
                            break
                        connection.send(chunk)
                        sent += len(chunk)
                except OSError:
                    # A worker can reject admission before reading a large body.
                    # Recover only an explicit pre-execution busy response;
                    # never retry an ambiguous disconnect or inference timeout.
                    try:
                        connection.sock.settimeout(2)
                        response = connection.getresponse()
                    except Exception:
                        raise
                    if not _busy(response):
                        raise
                if response is None and sent != size:
                    raise ValueError("GPU upload length mismatch")
            uploaded = time.monotonic()
            # Waiting for GPU execution starts after the body is fully sent.
            # Neither host admission nor upload consumes this response budget.
            if response is None:
                connection.sock.settimeout(response_timeout)
                # Retain the socket: HTTP/1.0 can detach it into the response.
                with _deadline(connection.sock, response_timeout):
                    response = connection.getresponse()
                    if not _busy(response):
                        if response.status != 200:
                            raise RuntimeError(f"GPU returned HTTP {response.status}")
                        raw = response.read(4_000_001)
                        if len(raw) > 4_000_000:
                            raise ValueError("Oversized GPU response")
                        if response.length not in (None, 0):
                            raise ValueError("Incomplete GPU response")
            if _busy(response):
                # Only this explicit pre-execution rejection is safe to retry.
                # Never submit another inference after an ambiguous timeout.
                connection.close()
                if time.monotonic() >= busy_deadline:
                    raise RemoteBusy("GPU admission remained busy")
                logger.info("gpu_remote_busy endpoint=%s attempt=%s", endpoint, attempt)
                time.sleep(min(2, max(0, busy_deadline - time.monotonic())))
                continue
            result = json.loads(raw)
            logger.info("gpu_remote_transport endpoint=%s bytes=%s upload_ms=%.1f response_ms=%.1f total_ms=%.1f",
                        endpoint, size, (uploaded-upload_started)*1000,
                        (time.monotonic()-uploaded)*1000, (time.monotonic()-started)*1000)
            return result
        finally:
            if response is not None:
                response.close()
            connection.close()
