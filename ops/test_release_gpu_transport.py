"""Release safety: a direct S3 reservation is durable; active work blocks."""
import importlib.util
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch


class ReleaseSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Deployment runs on Linux; this test only exercises SQLite admission.
        sys.path.insert(0, str(Path(__file__).parent))
        with patch.dict(sys.modules, {"fcntl": unittest.mock.Mock()}):
            spec = importlib.util.spec_from_file_location("gpu_release_under_test", Path(__file__).with_name("release_gpu_transport.py"))
            cls.release = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.release)

    def test_switch_preserves_direct_upload_and_holds_writer_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "tasks.sqlite3"
            with closing(sqlite3.connect(database)) as db, db:
                db.execute("CREATE TABLE tasks (status TEXT, stage TEXT, stored_path TEXT)")
                db.execute("INSERT INTO tasks VALUES ('queued', 'uploading', 's3://bucket/object')")
            def stop(*args, **kwargs):
                with closing(sqlite3.connect(database, timeout=0)) as competing:
                    with self.assertRaises(sqlite3.OperationalError):
                        competing.execute("UPDATE tasks SET stage='queued'")
            with patch.object(self.release.common, "database", return_value=database), patch.object(self.release.common, "run", side_effect=stop) as run:
                self.assertEqual(self.release.stop_when_compute_idle("example"), {"queued": 1})
                run.assert_called_once()
            with closing(sqlite3.connect(database)) as db, db:
                self.assertEqual(db.execute("SELECT * FROM tasks").fetchall(), [("queued", "uploading", "s3://bucket/object")])
                db.execute("UPDATE tasks SET stage='queued'")

    def test_active_jobs_and_local_or_malformed_uploads_block_stop(self):
        for row in [("running", "vision", "s3://bucket/object"),
                    ("queued", "queued", "s3://bucket/object"),
                    ("queued", "uploading", "/app/data/upload.mp4"),
                    ("uploading", "uploading", "s3://bucket/object"),
                    ("queued", None, "s3://bucket/object"),
                    ("queued", "uploading", None),
                    ("queued", "uploading", "s3://")]:
            with self.subTest(row=row), tempfile.TemporaryDirectory() as directory:
                database = Path(directory) / "tasks.sqlite3"
                with closing(sqlite3.connect(database)) as db, db:
                    db.execute("CREATE TABLE tasks (status TEXT, stage TEXT, stored_path TEXT)")
                    db.execute("INSERT INTO tasks VALUES (?, ?, ?)", row)
                with patch.object(self.release.common, "database", return_value=database), patch.object(self.release.common, "run") as run:
                    with self.assertRaisesRegex(RuntimeError, "Release deferred"):
                        self.release.stop_when_compute_idle("example")
                    run.assert_not_called()
                with closing(sqlite3.connect(database, timeout=0)) as db, db:
                    db.execute("UPDATE tasks SET status='done'")


if __name__ == "__main__":
    unittest.main()
