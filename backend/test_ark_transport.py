import io
import ssl
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request
from unittest.mock import Mock, patch

import urllib3

from app import ark_transport as transport


class ArkTransportTests(unittest.TestCase):
    def test_handshake_retries_happen_before_sending_and_restore_timeout(self):
        connection = transport.ArkHTTPSConnection("ark.invalid", timeout=30)
        with (
            patch.object(transport.HTTPSConnection, "connect", side_effect=[
                TimeoutError(), urllib3.exceptions.NewConnectionError(connection, "connect failed"), None,
            ]) as connect,
            patch.object(transport.time, "sleep"),
            patch.object(connection, "request") as send,
        ):
            connection.connect()
        self.assertEqual(connect.call_count, 3)
        self.assertEqual(connection.timeout, 30)
        send.assert_not_called()

    def test_connect_budget_and_attempts_are_bounded(self):
        for budget in (5, 30):
            clock = [0.0]
            connection = transport.ArkHTTPSConnection("ark.invalid", timeout=budget)

            def connect():
                clock[0] += connection.timeout
                raise TimeoutError()

            with (
                self.subTest(budget=budget),
                patch.object(transport.HTTPSConnection, "connect", side_effect=connect) as attempt,
                patch.object(transport.time, "monotonic", side_effect=lambda: clock[0]),
                patch.object(transport.time, "sleep", side_effect=lambda value: clock.__setitem__(0, clock[0] + value)),
                self.assertRaises(TimeoutError),
            ):
                connection.connect()
            self.assertLessEqual(attempt.call_count, 4)
            self.assertLessEqual(clock[0], budget)
            self.assertEqual(connection.timeout, budget)

    def test_certificate_errors_fail_immediately(self):
        connection = transport.ArkHTTPSConnection("ark.invalid", timeout=30)
        with (
            patch.object(transport.HTTPSConnection, "connect", side_effect=ssl.SSLCertVerificationError()) as connect,
            patch.object(transport.time, "sleep") as sleep,
            self.assertRaises(ssl.SSLCertVerificationError),
        ):
            connection.connect()
        connect.assert_called_once()
        sleep.assert_not_called()

    def test_request_errors_are_not_replayed_and_do_not_expose_urls(self):
        pool = Mock()
        pool.urlopen.side_effect = urllib3.exceptions.ReadTimeoutError(None, "/responses?secret=test", "timeout")
        with patch.object(transport, "_pool", pool), patch.object(transport, "getproxies", return_value={}):
            with self.assertRaises(URLError) as caught:
                with transport.open_response(Request("https://ark.invalid/responses", data=b"payload"), timeout=45):
                    self.fail("request should fail")
        self.assertNotIn("secret", str(caught.exception))
        pool.urlopen.assert_called_once()
        self.assertFalse(pool.urlopen.call_args.kwargs["retries"])
        self.assertEqual(pool.urlopen.call_args.kwargs["timeout"].read_timeout, 45)

    def test_proxy_route_uses_existing_handler(self):
        with (
            patch.object(transport, "getproxies", return_value={"https": "http://proxy.invalid"}),
            patch.object(transport, "proxy_bypass", return_value=False),
            patch.object(transport, "urlopen", return_value=io.BytesIO(b"{}")) as request,
            patch.object(transport, "_pool") as pool,
        ):
            with transport.open_response(Request("https://ark.invalid/responses"), timeout=45) as response:
                self.assertEqual(response.read(), b"{}")
        request.assert_called_once()
        pool.urlopen.assert_not_called()

    def test_drained_connection_timeout_is_reset_before_pool_reuse(self):
        pool = transport.ArkHTTPSConnectionPool("ark.invalid", maxsize=1, block=True)
        pool._get_conn()
        connection = Mock(timeout=45)
        pool._put_conn(connection)
        reused = pool._get_conn()
        self.assertIs(reused, connection)
        connection.sock.settimeout.assert_called_once_with(45)
        connection.close()
        pool.close()

    def test_sse_yields_partial_buffers_without_waiting_for_eof(self):
        response = Mock()
        response.read1.side_effect = [b'data: {"res', b'ult":1}\n\ndata: {}\n', b'last', b'']
        self.assertEqual(list(transport.ArkResponse(response)), [b'data: {"result":1}\n', b'\n', b'data: {}\n', b'last'])

    def test_real_http_reuses_connection_and_preserves_body_and_errors(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                requests.append((self.client_address, body, self.headers.get("Authorization")))
                self.send_response(429 if self.path == "/error" else 200)
                self.send_header("Content-Type", "text/event-stream")
                content = b'data: {"status":"completed"}\n\ndata: [DONE]\n\n'
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                self.wfile.flush()

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        pool = urllib3.PoolManager(maxsize=1, block=True)
        try:
            with patch.object(transport, "_pool", pool), patch.object(transport, "getproxies", return_value={}):
                for _ in range(3):
                    request = Request(f"http://127.0.0.1:{server.server_port}/responses", data=b"original",
                                      headers={"Authorization": "Bearer test-key"})
                    with transport.open_response(request, timeout=2) as response:
                        self.assertIn(b"completed", next(iter(response)))
                with self.assertRaises(HTTPError) as caught:
                    with transport.open_response(Request(f"http://127.0.0.1:{server.server_port}/error", data=b"error"), timeout=2):
                        self.fail("HTTP error expected")
            self.assertEqual(caught.exception.code, 429)
            self.assertIn(b"completed", caught.exception.read())
            self.assertEqual(len({item[0] for item in requests}), 1)
            self.assertEqual([item[1] for item in requests], [b"original"] * 3 + [b"error"])
            self.assertEqual([item[2] for item in requests[:3]], ["Bearer test-key"] * 3)
        finally:
            pool.clear()
            server.shutdown()
            server.server_close()
            worker.join(2)


if __name__ == "__main__":
    unittest.main()
