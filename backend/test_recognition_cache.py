"""Retries must keep original evidence and avoid repeating costly preparation."""

import asyncio
import os
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from unittest.mock import Mock, patch

from app import config, evidence, processing, providers, recognition_cache as checkpoints, storage
from app.errors import ArkError
import test_provider_delivery as delivery_tests


class RecognitionCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(config, "DATA_DIR", self.root))
        self.stack.enter_context(patch.object(config, "DB_PATH", self.root / "test.sqlite3"))
        self.original = self.root / "original.mp4"
        self.original.write_bytes(b"original-video")
        self.cache = checkpoints.recognition_cache("task", str(self.original))

    def test_source_replacement_and_changed_sampling_invalidate_checkpoint(self):
        self.cache.save("evidence", {"sources": []})
        self.assertEqual(self.cache.load("evidence"), {"sources": []})
        self.original.write_bytes(b"replacement-video")
        replaced = checkpoints.recognition_cache("task", str(self.original))
        self.assertIsNone(replaced.load("evidence"))
        with patch.object(config, "OCR_SAMPLE_INTERVAL_SECONDS", 0.123):
            changed = checkpoints.recognition_cache("task", str(self.original))
        self.assertNotEqual(replaced.identity, changed.identity)
        self.assertNotEqual(self.cache.identity, checkpoints.recognition_cache("another-task", str(self.original)).identity)

    def test_proxy_checksum_rejects_corruption_without_touching_original(self):
        proxy = self.root / "delivery.mp4"
        proxy.write_bytes(b"compressed")
        saved = self.cache.save_proxy(proxy)
        self.assertEqual(self.cache.proxy(), saved)
        saved.write_bytes(b"corrupted!")  # Same byte length: size alone is insufficient.
        self.assertIsNone(self.cache.proxy())
        self.assertEqual(self.original.read_bytes(), b"original-video")

    def test_adopt_proxy_moves_fresh_file_without_copying_original(self):
        proxy_dir = self.root / "encoded"
        proxy_dir.mkdir()
        proxy = proxy_dir / "delivery.mp4"
        proxy.write_bytes(b"compressed")
        with patch.object(checkpoints.shutil, "copyfile", side_effect=AssertionError("proxy was copied")):
            saved = self.cache.adopt_proxy(proxy)
        self.assertEqual(saved.read_bytes(), b"compressed")
        self.assertFalse(proxy.exists())
        self.assertEqual(self.cache.proxy(), saved)

    def test_adopt_proxy_falls_back_to_copy_when_move_fails(self):
        proxy = self.root / "encoded.mp4"
        proxy.write_bytes(b"compressed")
        original_replace = checkpoints.os.replace

        def reject_source_move(source, target):
            if Path(source) == proxy:
                raise OSError("cross-device move")
            return original_replace(source, target)

        with patch.object(checkpoints.os, "replace", side_effect=reject_source_move):
            saved = self.cache.adopt_proxy(proxy)
        self.assertEqual(saved.read_bytes(), b"compressed")
        self.assertTrue(proxy.exists())
        self.assertEqual(self.cache.proxy(), saved)

    def test_disk_checkpoint_failure_is_optional(self):
        with patch.object(checkpoints.os, "replace", side_effect=OSError("disk full")):
            self.cache.save("evidence", {"sources": []})
            self.assertEqual(self.cache.save_proxy(self.original), self.original)
        self.assertEqual(self.original.read_bytes(), b"original-video")

    def test_clip_delivery_is_isolated_reused_and_cleaned_without_touching_source(self):
        self.cache.save_proxy(self.original)
        clip = self.cache.scoped("segment_0_22000:clip-v1")
        another = self.cache.scoped("segment_22000_44000:clip-v1")
        clip_source = self.root / "clip.mp4"
        clip_source.write_bytes(b"different-clip")
        clip.save_proxy(clip_source)
        clip.save("delivery", {"uri": "s3://bucket/recognition/" + "a" * 64 + ".mp4"})
        self.assertEqual(self.cache.proxy().read_bytes(), b"original-video")
        self.assertEqual(self.cache.scoped("segment_0_22000:clip-v1").proxy().read_bytes(), b"different-clip")
        self.assertIsNone(self.cache.load("delivery"))
        self.assertIsNone(another.load("delivery"))
        self.assertNotEqual(clip.identity, another.identity)
        checkpoints.discard_local_proxy("task")
        self.assertIsNone(clip.proxy())
        self.assertIsNotNone(clip.load("delivery"))
        with (patch.object(config, "OBJECT_STORAGE_BUCKET", "bucket"),
              patch.object(config, "OBJECT_STORAGE_PREFIX", ""),
              patch.object(storage, "delete_media") as delete):
            checkpoints.cleanup_task_cache("task")
        delete.assert_called_once_with("s3://bucket/recognition/" + "a" * 64 + ".mp4")
        self.assertEqual(self.original.read_bytes(), b"original-video")

    def test_clip_repair_uses_private_cos_and_reuses_encoded_clip_after_model_failure(self):
        clip = self.cache.scoped("segment_0_22000:clip-v1")
        clip.save_proxy(self.original)
        uri = "s3://bucket/recognition/" + "a" * 64 + ".mp4"
        with (patch.object(providers, "DATA_DIR", self.root),
              patch.object(providers, "ARK_VIDEO_SOURCE_MODE", "auto"),
              patch.object(providers, "object_storage_enabled", return_value=True),
              patch.object(providers, "ark_account_key", return_value="key"),
              patch.object(providers, "upload_recognition_proxy", return_value=uri) as upload,
              patch.object(providers, "presigned_get", return_value="https://private/clip"),
              patch.object(providers, "media_info", return_value={"size": self.original.stat().st_size}),
              patch.object(providers, "ark_upload_video") as files,
              patch.object(providers.subprocess, "run") as encode,
              patch.object(providers, "ark_recognize", side_effect=[ArkError("temporary model failure"),
                  ({"scenes": []}, {"total_tokens": 5})]) as recognize):
            args = (self.original, "test", 120, {}, "turbo", {"startSec": 0, "endSec": 20})
            with self.assertRaises(ArkError):
                providers.ark_recognize_range(*args, cache=self.cache)
            providers.ark_recognize_range(*args, cache=self.cache)
        encode.assert_not_called()
        files.assert_not_called()
        upload.assert_called_once_with(clip.proxy(), clip.identity)
        self.assertEqual(recognize.call_args.kwargs["prepared_video"]["_video_uri"], uri)
        self.assertIsNone(self.cache.load("delivery"))

    def test_clip_preparation_retries_transient_error_but_not_exhausted_upload_budget(self):
        clip = self.cache.scoped("segment_0_22000:clip-v1")
        clip.save_proxy(self.original)
        transient = ArkError("TLS disconnected", provider_code="transient_network")
        exhausted = ArkError("budget exhausted", status_code=503, provider_code="video_upload_retry_exhausted")
        with (patch.object(providers, "DATA_DIR", self.root),
              patch.object(providers.time, "sleep"),
              patch.object(providers, "ark_prepare_video", side_effect=[transient, {"api_key": "key", "file_id": "file"}]) as prepare,
              patch.object(providers, "ark_recognize", return_value=({"scenes": []}, {})),
              patch.object(providers.subprocess, "run") as encode):
            args = (self.original, "test", 120, {}, "turbo", {"startSec": 0, "endSec": 20})
            providers.ark_recognize_range(*args, cache=self.cache)
            self.assertEqual(prepare.call_count, 2)
            self.assertEqual(prepare.call_args_list[0].args[0], prepare.call_args_list[1].args[0])
            prepare.reset_mock(side_effect=True)
            prepare.side_effect = exhausted
            self.cache.save("segment_0_22000", {"protocol": 0})
            with self.assertRaises(ArkError):
                providers.ark_recognize_range(*args, cache=self.cache)
            prepare.assert_called_once()
        encode.assert_not_called()

    def test_large_video_uses_private_proxy_then_retry_uses_fresh_url(self):
        with self.original.open("r+b") as handle:
            handle.truncate(50 * 1024 * 1024)
        self.cache = checkpoints.recognition_cache("task", str(self.original))
        proxy_dir = self.root / "encoded"
        proxy_dir.mkdir()
        proxy = proxy_dir / "delivery.mp4"
        proxy.write_bytes(b"compressed")
        uri = "s3://bucket/recognition/" + "a" * 64 + ".mp4"
        with (
            patch.object(providers, "ARK_VIDEO_SOURCE_MODE", "auto"),
            patch.object(providers, "object_storage_enabled", return_value=True),
            patch.object(providers, "ark_account_key", return_value="private-key"),
            patch.object(providers, "_ark_upload_proxy", return_value=(proxy, proxy_dir)) as encode,
            patch.object(providers, "upload_recognition_proxy", return_value=uri) as upload,
            patch.object(providers, "presigned_get", side_effect=["https://signed/first", "https://signed/retry"]) as sign,
            patch.object(providers, "media_info", return_value={"size": 10}),
            patch.object(providers, "ark_upload_video") as files,
            patch.object(providers, "ark_wait_for_file") as wait,
        ):
            first = providers.ark_prepare_video(self.original, 180, source_uri="s3://bucket/original", cache=self.cache)
            second = providers.ark_prepare_video(self.original, 180, source_uri="s3://bucket/original", cache=self.cache)
        self.assertEqual(first["_video_uri"], uri)
        self.assertEqual(second["video_url"], "https://signed/retry")
        self.assertEqual(encode.call_count, 1)
        self.assertEqual(upload.call_count, 1)
        self.assertEqual(sign.call_count, 2)
        files.assert_not_called()
        wait.assert_not_called()
        self.assertEqual(self.original.stat().st_size, 50 * 1024 * 1024)
        for path in self.cache.directory.glob("*.json"):
            content = path.read_text(encoding="utf-8")
            self.assertNotIn("private-key", content)
            self.assertNotIn("https://signed/", content)

    def test_object_upload_failure_reuses_proxy_even_if_checkpoint_fails(self):
        cache = Mock(identity="task-fingerprint")
        cache.load.return_value = None
        cache.proxy.return_value = None
        cache.save_proxy.side_effect = lambda path: path
        with (
            patch.object(providers, "ARK_VIDEO_SOURCE_MODE", "auto"),
            patch.object(providers, "object_storage_enabled", return_value=True),
            patch.object(providers, "ark_account_key", return_value="key"),
            patch.object(providers, "_ark_upload_proxy", return_value=(self.original, self.root / "temporary")) as encode,
            patch.object(providers, "upload_recognition_proxy", side_effect=TimeoutError("视频传输超时")),
            patch.object(providers, "_ark_upload_prepared_video", return_value=("file", "key")) as files,
            patch.object(providers, "ark_wait_for_file"),
        ):
            result = providers.ark_prepare_video(self.original, 180, cache=cache)
        self.assertEqual(result["file_id"], "file")
        self.assertEqual(files.call_args.args[0], self.original)
        self.assertEqual(encode.call_count, 1)

    def test_success_releases_local_proxy_and_retention_only_deletes_proxy_key(self):
        self.cache.save_proxy(self.original)
        self.cache.save("evidence", {"sources": []})
        checkpoints.discard_local_proxy("task")
        self.assertIsNone(self.cache.proxy())
        self.assertIsNotNone(self.cache.load("evidence"))
        prefix = "videos/recognition/"
        with patch.object(config, "OBJECT_STORAGE_BUCKET", "bucket"), patch.object(config, "OBJECT_STORAGE_PREFIX", "videos"):
            for uri, allowed in (("s3://bucket/videos/task.mp4", False),
                                 ("s3://other/" + prefix + "a" * 64 + ".mp4", False),
                                 ("s3://bucket/" + prefix + "a" * 64 + ".mp4", True)):
                self.cache.save("delivery", {"uri": uri})
                with patch.object(storage, "delete_media") as delete:
                    checkpoints.cleanup_task_cache("task")
                self.assertEqual(delete.call_count, int(allowed))
        self.assertTrue(self.original.exists())

    def test_prune_preserves_active_tasks_and_removes_only_expired_proxy(self):
        self.cache.save_proxy(self.original)
        self.cache.save("evidence", {"sources": []})
        proxy = self.cache.directory / "proxy.mp4"
        os.utime(proxy, (1, 1))
        with closing(sqlite3.connect(config.DB_PATH)) as db, db:
            db.execute("CREATE TABLE tasks(id TEXT, status TEXT)")
            db.execute("INSERT INTO tasks VALUES('task','running')")
        with patch.object(checkpoints, "_next_prune", 0):
            checkpoints._prune_local_proxies()
        self.assertTrue(proxy.exists())
        with closing(sqlite3.connect(config.DB_PATH)) as db, db:
            db.execute("UPDATE tasks SET status='failed'")
        with patch.object(checkpoints, "_next_prune", 0):
            checkpoints._prune_local_proxies()
        self.assertFalse(proxy.exists())
        self.assertIsNotNone(self.cache.load("evidence"))
        self.assertTrue(self.original.exists())

    def test_streaming_storage_put_never_overwrites_original_key(self):
        with (patch.object(storage, "OBJECT_STORAGE_BUCKET", "bucket"),
              patch.object(storage, "OBJECT_STORAGE_PREFIX", "videos"),
              patch.object(storage, "_bounded_transfer") as transfer):
            uri = storage.upload_recognition_proxy(self.original, "fingerprint")
        operation, bucket, key, path, deadline = transfer.call_args.args
        self.assertEqual((operation, bucket, path), ("upload", "bucket", self.original))
        self.assertTrue(key.startswith("videos/recognition/"))
        self.assertNotEqual(key, "videos/task.mp4")
        self.assertIn(key, uri)
        self.assertEqual(deadline, storage.MEDIA_PROXY_UPLOAD_TIMEOUT_SECONDS)


class EvidenceReuseTests(unittest.TestCase):
    def test_failed_source_is_recollected_and_one_character_words_are_preserved(self):
        audio = {"kind": "audio", "status": "available", "items": [{"text": "啊", "startSec": .1, "endSec": .3}]}
        frames = {"kind": "keyframes", "status": "available", "items": []}
        old = {"sources": [audio, frames, {"kind": "ocr", "status": "unavailable", "items": []}]}
        ocr = {"kind": "ocr", "status": "available", "items": [{"text": "呵", "startSec": .4, "endSec": .5}]}
        with (patch.object(evidence, "_audio_evidence") as asr,
              patch.object(evidence, "_keyframe_evidence") as keyframes,
              patch.object(evidence, "_ocr_evidence", return_value=ocr) as read):
            fresh = evidence.collect_evidence(Path("original.mp4"), "test", 1, cached_evidence=old)
            cached = evidence.collect_evidence(Path("original.mp4"), "test", 1, cached_evidence=fresh)
        asr.assert_not_called()
        keyframes.assert_not_called()
        read.assert_called_once()
        self.assertEqual(cached["sources"], [audio, ocr, frames])
        self.assertIn("啊", cached["summary"])
        self.assertIn("呵", cached["summary"])
        self.assertEqual(cached["cacheHitSources"], ["audio", "keyframes", "ocr"])

    def test_preparation_failure_saves_evidence_without_marking_vision(self):
        cache = Mock()
        cache.load.return_value = None
        collected = {"sources": []}
        failure = ArkError("upload stopped", provider_code="video_upload_retry_exhausted")
        with (patch.object(processing, "recognition_cache", return_value=cache),
              patch.object(processing, "materialize_media", return_value=(Path("original.mp4"), None)),
              patch.object(processing, "ARK_API_KEYS", ("key",)),
              patch.object(processing, "OPENAI_API_KEY", ""),
              patch.object(processing, "collect_evidence", return_value=collected),
              patch.object(processing, "ark_prepare_video", side_effect=failure),
              patch.object(processing, "batch_character_context", return_value=None),
              patch.object(processing, "mark_task_stage") as stage,
              patch.object(processing, "ark_recognize") as recognize):
            with self.assertRaises(ArkError):
                asyncio.run(processing.run_recognizer({"id": "task", "stored_path": "original.mp4", "title": "test", "duration_sec": 1}))
        cache.save.assert_called_once_with("evidence", collected)
        self.assertNotIn("vision", [call.kwargs["stage"] for call in stage.call_args_list])
        recognize.assert_not_called()
        self.assertTrue(processing._is_non_retryable_provider_error(failure))


class UploadBudgetTests(unittest.TestCase):
    def test_total_budget_overrides_old_three_attempt_240_second_configuration(self):
        clock = [0.0]
        timeouts = []

        def timeout_request(*args, **kwargs):
            timeouts.append(kwargs["timeout"])
            clock[0] += kwargs["timeout"]
            raise ArkError("write timed out", provider_code="transient_network")

        with (tempfile.TemporaryDirectory() as root,
              patch.object(providers, "ARK_UPLOAD_ATTEMPTS", 3),
              patch.object(providers, "ARK_UPLOAD_TIMEOUT_SECONDS", 240),
              patch.object(providers, "ARK_UPLOAD_TOTAL_TIMEOUT_SECONDS", 240),
              patch.object(providers.time, "monotonic", side_effect=lambda: clock[0]),
              patch.object(providers.time, "sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)),
              patch.object(providers, "ark_http", side_effect=timeout_request)):
            path = Path(root) / "proxy.mp4"
            path.write_bytes(b"video")
            with self.assertRaises(ArkError) as caught:
                providers._ark_upload_prepared_video(path, "key", 1)
        self.assertEqual(timeouts, [240])
        self.assertEqual(clock[0], 240)
        self.assertTrue(processing._is_non_retryable_provider_error(caught.exception))

    def test_waiting_for_upload_capacity_consumes_budget_without_sending(self):
        slot = Mock()
        slot.acquire.return_value = False
        with (patch.object(providers, "_ark_upload_semaphore", slot),
              patch.object(providers.time, "monotonic", return_value=5),
              patch.object(providers, "urlopen") as http):
            with self.assertRaises(ArkError):
                providers.ark_http("POST", "/files", b"video", api_key="key", upload=True, upload_deadline=10)
        slot.acquire.assert_called_once_with(timeout=5)
        slot.release.assert_not_called()
        http.assert_not_called()

    def test_review_refreshes_signed_link_and_refresh_failure_keeps_valid_link(self):
        prepared = {"api_key": "key", "video_url": "https://signed/old", "_video_uri": "s3://bucket/proxy"}
        with (patch.object(providers, "presigned_get", side_effect=["https://signed/fresh", OSError("offline")]),
              patch.object(providers, "ark_stream_response", return_value=delivery_tests.ProviderDeliveryTests().completed()) as model,
              patch.object(providers, "ark_prepare_video") as prepare):
            providers.ark_recognize(Path("source.mp4"), "test", 1, prepared_video=prepared)
            providers.ark_recognize(Path("source.mp4"), "test", 1, prepared_video=prepared)
        prepare.assert_not_called()
        self.assertEqual(model.call_count, 2)
        self.assertEqual(prepared["video_url"], "https://signed/fresh")


if __name__ == "__main__":
    unittest.main()
