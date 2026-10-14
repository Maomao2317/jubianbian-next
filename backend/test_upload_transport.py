"""Exercise urllib's actual fixed-length multipart wire format."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest
from unittest.mock import patch

from app import providers
from app.errors import ArkError
from app.media import multipart_body


class UploadTransportTests(unittest.TestCase):
    def test_streamed_multipart_arrives_unchanged_without_chunked_encoding(self):
        received = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received["length"] = self.headers.get("Content-Length")
                received["encoding"] = self.headers.get("Transfer-Encoding")
                received["body"] = self.rfile.read(int(received["length"]))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"id":"file-test"}')

            def log_message(self, *args):
                pass

        body, boundary = multipart_body({"purpose": "user_data"}, "file", "source.mp4",
                                        bytes(range(256)) * 1024, "video/mp4")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(providers, "ARK_BASE_URL", f"http://127.0.0.1:{server.server_port}"):
                result = providers.ark_http("POST", "/files", body, f"multipart/form-data; boundary={boundary}",
                                            api_key="test-key", upload=True)
            self.assertEqual(result, {"id": "file-test"})
            self.assertEqual(received["body"], body)
            self.assertEqual(int(received["length"]), len(body))
            self.assertIsNone(received["encoding"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_upload_deadline_stops_a_progressing_transfer(self):
        chunks = providers._ark_upload_chunks(b"x" * (128 * 1024), 10)
        with patch.object(providers.time, "monotonic", side_effect=[9, 10]):
            self.assertEqual(len(next(chunks)), 64 * 1024)
            with self.assertRaises(ArkError) as caught:
                next(chunks)
        self.assertEqual(caught.exception.provider_code, "video_upload_retry_exhausted")


if __name__ == "__main__":
    unittest.main()
