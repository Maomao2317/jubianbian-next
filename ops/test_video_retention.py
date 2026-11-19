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
        plans = [{"objects": ("s3://bucket/one.mp4", "s3://bucket/two.mp4")}]
        def delete(uri):
            if uri.endswith("two.mp4"):
                raise OSError("temporary failure")

        with patch.object(retention.storage, "delete_media", side_effect=delete):
            self.assertEqual(retention.bulk_delete(plans), {"s3://bucket/one.mp4"})

    def test_shared_proxy_is_not_sent_for_deletion(self):
        plans = [{"objects": ("s3://bucket/original.mp4", "s3://bucket/shared.mp4")}]
        with patch.object(retention.storage, "delete_media") as delete:
            self.assertEqual(retention.bulk_delete(plans, {"s3://bucket/shared.mp4"}),
                             {"s3://bucket/original.mp4"})
        delete.assert_called_once_with("s3://bucket/original.mp4")

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
