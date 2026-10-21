"""Real child transports against a local S3 fixture; no cloud credentials or APIs."""
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from app import storage


@unittest.skipUnless(importlib.util.find_spec("boto3"), "boto3 is a deployment dependency")
class StorageTransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.body = bytes(range(256)) * 2048
        self.uploads = []
        self.started = threading.Event()
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def headers_for(self, size):
                self.send_response(200)
                self.send_header("Content-Length", str(size))
                self.send_header("ETag", '"' + hashlib.md5(fixture.body).hexdigest() + '"')
                self.end_headers()

            def do_HEAD(self):
                self.headers_for(len(fixture.body))

            def do_GET(self):
                self.headers_for(len(fixture.body))
                if "stall" not in self.path:
                    self.wfile.write(fixture.body)
                    return
                fixture.started.set()
                try:
                    for offset in range(0, len(fixture.body), 1024):
                        self.wfile.write(fixture.body[offset:offset + 1024])
                        self.wfile.flush()
                        time.sleep(.05)  # Progress never triggers a socket read timeout.
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

            def do_PUT(self):
                fixture.uploads.append(self.rfile.read(int(self.headers["Content-Length"])))
                self.headers_for(0)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(self.server.server_close)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.shutdown)
        endpoint = f"http://127.0.0.1:{self.server.server_port}"
        self.env = patch.dict(os.environ, {
            "JBB_OBJECT_STORAGE_ENDPOINT": endpoint, "JBB_OBJECT_STORAGE_BUCKET": "fixture",
            "JBB_OBJECT_STORAGE_ACCESS_KEY": "fixture-access", "JBB_OBJECT_STORAGE_SECRET_KEY": "fixture-secret",
            "JBB_OBJECT_STORAGE_REGION": "us-east-1", "JBB_OBJECT_STORAGE_ADDRESSING_STYLE": "path",
            "JBB_DATA_DIR": str(self.root), "NO_PROXY": "127.0.0.1,localhost",
            "AWS_REQUEST_CHECKSUM_CALCULATION": "when_supported", "AWS_RESPONSE_CHECKSUM_VALIDATION": "when_required",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_real_upload_and_download_preserve_all_bytes(self):
        source, target = self.root / "upload.mp4", self.root / "download.mp4"
        source.write_bytes(self.body)
        storage._bounded_transfer("upload", "fixture", "ok.mp4", source, 15)
        storage._bounded_transfer("download", "fixture", "ok.mp4", target, 15)
        self.assertEqual(self.uploads, [self.body])
        self.assertEqual(target.read_bytes(), self.body)

    def test_https_upload_retains_fixed_length_under_sdk_checksum_default(self):
        class RequestCaptured(Exception):
            pass

        def capture(request, **_kwargs):
            headers = {key.lower(): value for key, value in request.headers.items()}
            self.assertEqual(int(headers["content-length"]), len(self.body))
            self.assertNotIn("transfer-encoding", headers)
            self.assertNotIn("content-encoding", headers)
            self.assertNotIn("x-amz-trailer", headers)
            self.assertEqual(request.body.read(), self.body)
            # Stop before the socket opens; no credentials or media leave the test.
            raise RequestCaptured()

        with patch.multiple(storage, OBJECT_STORAGE_ENABLED=True,
                            OBJECT_STORAGE_ENDPOINT="https://fixture.invalid",
                            OBJECT_STORAGE_BUCKET="fixture",
                            OBJECT_STORAGE_ACCESS_KEY="fixture-access",
                            OBJECT_STORAGE_SECRET_KEY="fixture-secret",
                            OBJECT_STORAGE_REGION="us-east-1",
                            OBJECT_STORAGE_ADDRESSING_STYLE="path"):
            client = storage._client(delivery=True)
        self.addCleanup(client.close)
        client.meta.events.register("before-send.s3.PutObject", capture)
        with self.assertRaises(RequestCaptured):
            client.put_object(Bucket="fixture", Key="fixture.mp4", Body=io.BytesIO(self.body),
                              ContentLength=len(self.body), ContentType="video/mp4")

    def test_continuously_progressing_download_is_killed_reaped_and_cleaned(self):
        children = []
        real_popen = subprocess.Popen

        def spawn(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            children.append(child)
            return child

        started = time.monotonic()
        with (patch.object(storage, "DATA_DIR", self.root),
              patch.object(storage, "MEDIA_DOWNLOAD_TIMEOUT_SECONDS", 5),
              patch.object(storage.subprocess, "Popen", side_effect=spawn)):
            with self.assertRaisesRegex(TimeoutError, "视频传输超时"):
                storage.materialize_media("s3://fixture/stall.mp4", "fixture")
        self.assertTrue(self.started.is_set(), "The child must reach the streaming response")
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].returncode, "Timed-out transport must be reaped")
        self.assertEqual(list(self.root.glob("jbb-media-*")), [])
        # A timed-out download releases its capacity slot for the next task.
        with patch.object(storage, "DATA_DIR", self.root):
            target, directory = storage.materialize_media("s3://fixture/ok.mp4", "next")
        self.assertEqual(target.read_bytes(), self.body)
        storage.cleanup_materialized(directory)

    def test_child_failure_never_exposes_transport_output(self):
        error = subprocess.CompletedProcess([], 1, "signed-url=private", "secret-key=private")
        with patch.object(storage.subprocess, "run", return_value=error):
            with self.assertRaises(OSError) as raised:
                storage._bounded_transfer("download", "fixture", "key", self.root / "file", 2)
        self.assertNotIn("private", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
