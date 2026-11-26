"""Pooled Ark connections with bounded retries before sending HTTP requests."""
from contextlib import contextmanager
import io
import random
import socket
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import getproxies, proxy_bypass, urlopen

import urllib3
from urllib3.connection import HTTPSConnection
from urllib3.connectionpool import HTTPSConnectionPool

from .logging_setup import logger


class ArkHTTPSConnection(HTTPSConnection):
    def connect(self):
        # urllib3 calls connect before sending the request headers or body.
        # Retrying here cannot create a second model generation.
        original_timeout = self.timeout
        deadline = time.monotonic() + float(original_timeout)
        for attempt in range(1, 5):
            self.timeout = min(8.0, max(0.001, deadline - time.monotonic()))
            try:
                super().connect()
                return
            except (OSError, urllib3.exceptions.ConnectTimeoutError) as exc:
                self.close()
                if isinstance(exc, ssl.SSLCertVerificationError):
                    raise
                delay = min(2.0, 0.5 * 2 ** (attempt - 1)) + random.uniform(0, 0.25)
                remaining = deadline - time.monotonic()
                if attempt == 4 or remaining <= delay:
                    raise
                logger.warning(
                    "provider_step provider=ark operation=tls_connect_retry attempt=%s error_type=%s",
                    attempt, type(exc).__name__,
                )
                time.sleep(delay)
            finally:
                self.timeout = original_timeout


class ArkHTTPSConnectionPool(HTTPSConnectionPool):
    ConnectionCls = ArkHTTPSConnection

    def _put_conn(self, connection):
        # read1 can return a drained connection to the pool automatically.
        # Reset its short drain timeout before another thread can acquire it.
        if connection is not None and connection.sock is not None:
            connection.sock.settimeout(connection.timeout)
        super()._put_conn(connection)


_pool = urllib3.PoolManager(
    num_pools=4, maxsize=32, block=True, ssl_context=ssl.create_default_context(),
    socket_options=[*HTTPSConnection.default_socket_options, (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)],
)
# Keep urllib3's global scheme registry intact for object storage and other APIs.
_pool.pool_classes_by_scheme = {**_pool.pool_classes_by_scheme, "https": ArkHTTPSConnectionPool}


class ArkResponse:
    def __init__(self, response):
        self.response = response

    def read(self):
        return self.response.read()

    def __iter__(self):
        # read1 yields available SSE bytes promptly instead of waiting for an
        # entire 64 KiB buffer while the model is still generating.
        pending = b""
        while chunk := self.response.read1(64 * 1024):
            lines = (pending + chunk).split(b"\n")
            pending = lines.pop()
            for line in lines:
                yield line + b"\n"
        if pending:
            yield pending


def _release(response, failed):
    try:
        # A completed SSE event may precede the final HTTP chunk. Give that
        # small trailer a bounded drain so the TLS connection can be reused.
        connection = response.connection
        if not failed and connection is not None and connection.sock is not None:
            deadline = time.monotonic() + 0.5
            connection.sock.settimeout(0.25)
            for _ in range(4):
                if response.isclosed() or time.monotonic() >= deadline:
                    break
                if not response.read1(64 * 1024):
                    break
    except (OSError, urllib3.exceptions.HTTPError):
        pass
    finally:
        if not response.isclosed():
            response.close()
        response.release_conn()


@contextmanager
def open_response(request, timeout):
    parsed = urlsplit(request.full_url)
    # Preserve configured proxy routing through the existing urllib handler.
    if getproxies().get(parsed.scheme) and not proxy_bypass(parsed.hostname):
        with urlopen(request, timeout=timeout) as response:
            yield response
        return
    response = None
    failed = True
    try:
        response = _pool.urlopen(
            request.get_method(), request.full_url, body=request.data,
            headers=dict(request.header_items()),
            timeout=urllib3.Timeout(connect=min(30.0, timeout), read=timeout),
            pool_timeout=min(30.0, timeout), preload_content=False,
            retries=False, redirect=False,
        )
        if response.status >= 300:
            raise HTTPError(request.full_url, response.status, response.reason,
                            response.headers, io.BytesIO(response.read()))
        yield ArkResponse(response)
        failed = False
    except urllib3.exceptions.HTTPError as exc:
        # Pool errors can include request URLs. Return only the error type.
        raise URLError(type(exc).__name__) from exc
    finally:
        if response is not None:
            _release(response, failed)
