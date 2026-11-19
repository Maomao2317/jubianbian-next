"""Retention removes video media without deleting screenplay data."""
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import video_retention as retention


class VideoRetentionTests(unittest.TestCase):
    def test_old_terminal_media_only_and_idempotent(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            data = Path(directory)
            uploads = data / "uploads"
            uploads.mkdir()
            old = uploads / "old.mp4"
            active = uploads / "active.mp4"
            recent = uploads / "recent.mp4"
            for path in (old, active, recent):
                path.write_bytes(b"video")
            db = sqlite3.connect(":memory:")
            db.row_factory = sqlite3.Row
            db.execute("CREATE TABLE tasks(id TEXT, stored_path TEXT, status TEXT, created_at TEXT, result_json TEXT)")
            db.executemany("INSERT INTO tasks VALUES (?,?,?,?,?)", [
                ("old", str(old), "done", "2026-11-01T00:00:00+00:00", '{"script":"台词"}'),
                ("active", str(active), "running", "2026-11-01T00:00:00+00:00", None),
                ("recent", str(recent), "done", "2026-11-10T00:00:00+00:00", '{"script":"新剧本"}'),
            ])
            cutoff = datetime(2026, 11, 5, tzinfo=timezone.utc)
            with patch.object(retention.config, "DATA_DIR", data), patch.object(
                retention.config, "UPLOAD_DIR", uploads
            ):
                self.assertEqual(retention.audit(db, cutoff)["eligible"], 1)
                result = retention.run(db, cutoff)
                self.assertEqual(result["deleted"], 1)
                self.assertEqual(result["failed"], 0)
                self.assertFalse(old.exists())
                self.assertTrue(active.exists())
                self.assertTrue(recent.exists())
                self.assertEqual(db.execute("SELECT count(*) FROM tasks").fetchone()[0], 3)
                self.assertEqual(db.execute("SELECT result_json FROM tasks WHERE id='old'").fetchone()[0],
                                 '{"script":"台词"}')
                self.assertEqual(retention.run(db, cutoff)["eligible"], 0)
            db.close()

    def test_object_bulk_delete_requires_confirmed_keys(self):
        class Client:
            def delete_objects(self, *, Bucket, Delete):
                self.assert_bucket = Bucket
                return {"Deleted": [{"Key": Delete["Object"][0]["Key"]}],
                        "Error": [{"Key": Delete["Object"][1]["Key"]}]}

        plans = [{"objects": ("s3://bucket/one.mp4", "s3://bucket/two.mp4")}]
        with patch.object(retention.storage, "_client", return_value=Client()):
            self.assertEqual(retention.bulk_delete(plans), {"s3://bucket/one.mp4"})

    def test_shared_proxy_is_not_sent_for_deletion(self):
        class Client:
            def delete_objects(self, *, Bucket, Delete):
                self.keys = [entry["Key"] for entry in Delete["Object"]]
                return {"Deleted": Delete["Object"]}

        client = Client()
        plans = [{"objects": ("s3://bucket/original.mp4", "s3://bucket/shared.mp4")}]
        with patch.object(retention.storage, "_client", return_value=client):
            self.assertEqual(retention.bulk_delete(plans, {"s3://bucket/shared.mp4"}),
                             {"s3://bucket/original.mp4"})
        self.assertEqual(client.keys, ["original.mp4"])

    def test_out_of_scope_local_file_is_rejected(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            uploads = root / "uploads"
            uploads.mkdir()
            row = {"id": "old", "stored_path": str(root / "elsewhere" / "old.mp4"),
                   "created_at": "2026-11-01T00:00:00+00:00"}
            with patch.object(retention.config, "UPLOAD_DIR", uploads):
                with self.assertRaises(ValueError):
                    retention.plan(row)


if __name__ == "__main__":
    unittest.main()
