"""Real HTTP transport: explicit admission retries, bounded time, intact bodies."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import socket
import threading
import time
import unittest
from unittest.mock import patch
from app import gpu_transport as transport


class GpuTransportTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                fixture.calls.append(self.path)
                try:
                    if self.path == "/early" and len(fixture.calls) == 1:
                        self.send_response(503)
                        self.send_header("X-GPU-Busy", "1")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        self.wfile.flush()
                        time.sleep(.1)  # Allow either OS to observe the early response before FIN/RST.
                        return
                    body = self.rfile.read(int(self.headers["Content-Length"]))
                    if self.path == "/disconnect":
                        self.connection.shutdown(socket.SHUT_RDWR)
                        return
                    if self.path == "/busy" and len(fixture.calls) == 1:
                        self.send_response(503)
                        self.send_header("X-GPU-Busy", "1")
                        self.end_headers()
                        return
                    if self.path == "/failure":
                        self.send_error(503)
                        return
                    raw = json.dumps({"bytes": len(body), "first": body[:3].hex()}).encode()
                    if self.path == "/headers":
                        for part in (b"HTTP/1.0 200 OK\r\n", b"Content-Type: application/json\r\n", b"\r\n", raw):
                            self.wfile.write(part)
                            self.wfile.flush()
                            time.sleep(.08)
                        return
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(raw) + (10 if self.path == "/truncated" else 0)))
                    self.end_headers()
                    if self.path == "/drip":
                        for byte in raw:
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                            time.sleep(.04)
                    elif self.path == "/slow":
                        time.sleep(.25)
                        self.wfile.write(raw)
                    else:
                        self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def send(self, endpoint, payload=b"abc", timeout=2):
        return transport.post(self.url, "fixture-token", endpoint, io.BytesIO(payload), len(payload), {}, timeout)

    def test_file_payload_arrives_unchanged_and_busy_is_retried(self):
        with patch.object(transport.time, "sleep"):
            self.assertEqual(self.send("/busy"), {"bytes": 3, "first": b"abc".hex()})
        self.assertEqual(self.calls, ["/busy", "/busy"])

    def test_large_upload_rejected_before_body_can_retry_only_explicit_busy(self):
        with patch.object(transport.time, "sleep"):
            result = self.send("/early", b"abc" * 4_000_000)
        self.assertEqual(result["bytes"], 12_000_000)
        self.assertEqual(len(self.calls), 2)

    def test_disconnect_or_real_worker_failure_is_never_resubmitted(self):
        for endpoint in ("/disconnect", "/failure", "/truncated"):
            self.calls.clear()
            with self.subTest(endpoint=endpoint), self.assertRaises(Exception):
                self.send(endpoint)
            self.assertEqual(self.calls, [endpoint])

    def test_absolute_deadline_bounds_slow_headers_and_dripping_body(self):
        for endpoint in ("/headers", "/drip", "/slow"):
            self.calls.clear()
            started = time.monotonic()
            with self.subTest(endpoint=endpoint), self.assertRaises(TimeoutError):
                self.send(endpoint, timeout=.12)
            self.assertLess(time.monotonic() - started, .6)
            self.assertEqual(self.calls, [endpoint])

    def test_busy_wait_budget_is_bounded(self):
        with patch.object(transport.config, "MEDIA_TRANSFER_QUEUE_TIMEOUT_SECONDS", 0):
            with self.assertRaises(transport.RemoteBusy):
                self.send("/busy")
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
