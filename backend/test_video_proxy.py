"""Large-video delivery must never compromise the original evidence source."""

import hashlib
import io
import json
import sqlite3
import shutil
import subprocess
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import auth_store, processing, providers, routes, task_store
from app.errors import ArkError


MIB = 1024 * 1024


class CompressionProgressTests(unittest.TestCase):
    def test_ffmpeg_time_parser_is_real_bounded_and_tolerant(self):
        for line, expected in (("out_time_us=12000000", 12), ("out_time_ms=12000000", 12),
                               ("out_time=00:00:12.000000", 12), ("out_time_us=100000000", 99),
                               ("out_time_us=-100", 0), ("out_time_us=N/A", None),
                               ("out_time_us=nan", None), ("progress=end", None), ("frame=12", None)):
            with self.subTest(line=line):
                self.assertEqual(providers._ffmpeg_progress_percent(line, 100), expected)

    def test_encoder_drains_progress_and_only_reports_monotonic_values_below_100(self):
        process = Mock(stdout=io.StringIO("out_time_us=12000000\nout_time_us=10000000\nout_time_us=100000000\n"))
        process.wait.return_value = 0
        process.poll.return_value = 0
        callback = Mock()
        with patch.object(providers.subprocess, "Popen", return_value=process):
            providers._run_ffmpeg_proxy(["ffmpeg"], 100, callback)
        self.assertEqual([call.args for call in callback.call_args_list], [("compressing", 12), ("compressing", 99)])
        self.assertTrue(process.stdout.closed)
        process.kill.assert_not_called()

    def test_encoder_timeout_kills_and_reaps_process(self):
        process = Mock(stdout=io.StringIO(""))
        process.wait.side_effect = [subprocess.TimeoutExpired(["ffmpeg"], 1), -9]
        process.poll.return_value = None
        with patch.object(providers.subprocess, "Popen", return_value=process), self.assertRaises(subprocess.TimeoutExpired):
            providers._run_ffmpeg_proxy(["ffmpeg"], 100, Mock())
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)
        self.assertTrue(process.stdout.closed)

    def test_progress_store_failure_does_not_break_encoder(self):
        callback = Mock(side_effect=RuntimeError("temporarily unavailable"))
        with patch.object(providers.logger, "exception") as log:
            providers._report_media_progress(callback, "compressing", 12)
        log.assert_called_once()

    def test_task_progress_is_throttled_and_stage_changes_are_immediate(self):
        callback = processing._media_progress_callback("task")
        with (patch.object(processing, "update_task") as update,
              patch.object(processing, "record_task_event") as event,
              patch.object(processing.time, "monotonic", side_effect=[0, .1, .2, .8, 1.2, 1.3, 1.4, 1.5])):
            callback("compression_queued", 0)
            callback("compressing", 0)
            callback("compressing", 12)
            callback("compressing", 14)
            callback("compressing", 24)
            callback("compressing", 23)
            callback("compressing", 100)
            callback("video_upload", None)
        self.assertEqual(update.call_count, 5)
        self.assertEqual(event.call_count, 3)
        self.assertEqual(update.call_args_list[2].kwargs["stage_progress_percent"], 24)
        self.assertEqual(update.call_args_list[2].kwargs["progress_percent"], 19)
        self.assertIsNone(update.call_args.kwargs["stage_progress_percent"])

    def test_migration_api_round_trip_and_stale_progress_reset(self):
        database = "file:jbb-compression-progress?mode=memory&cache=shared"

        def open_db():
            connection = sqlite3.connect(database, uri=True)
            connection.row_factory = sqlite3.Row
            return connection

        keeper = open_db()
        try:
            with (patch.object(auth_store, "db", open_db), patch.object(task_store, "db", open_db),
                  patch.object(routes, "db", open_db), patch.object(routes, "current_user", return_value={"id": "user"})):
                auth_store.init_db()
                keeper.execute("ALTER TABLE tasks DROP COLUMN stage_progress_percent")
                keeper.commit()
                auth_store.init_db()
                auth_store.init_db()  # Upgrade is idempotent.
                keeper.execute("""INSERT INTO tasks (id, user_id, title, file_name, stored_path, status, stage,
                                  progress_percent, stage_progress_percent, created_at, updated_at)
                                  VALUES ('task', 'user', 'test', '1.mp4', 'source', 'running', 'compressing',
                                          17, 12, '2026-10-13', '2026-10-13')""")
                keeper.commit()
                listed = routes.list_tasks(None)[0]
                self.assertEqual(listed["stageProgressPercent"], 12)
                self.assertEqual(listed["progressPercent"], 17)
                task_store.update_task("task", stage="vision", progress_percent=62)
                self.assertIsNone(routes.list_tasks(None)[0]["stageProgressPercent"])
                self.assertIsNone(keeper.execute("SELECT stage_progress_percent FROM tasks").fetchone()[0])
        finally:
            keeper.close()


class VideoProxyTests(unittest.TestCase):
    def source(self, size=320 * MIB):
        return SimpleNamespace(name="original.mp4", stem="original",
                               stat=Mock(return_value=SimpleNamespace(st_size=size)))

    def proxy_environment(self, stack, *, size=30 * MIB, error=None):
        source = self.source()
        result = SimpleNamespace(st_size=size)
        stack.enter_context(patch.object(providers, "ARK_UPLOAD_PROXY_TRIGGER_MB", 45))
        stack.enter_context(patch.object(providers, "ARK_UPLOAD_PROXY_MAX_MB", 40))
        stack.enter_context(patch.object(providers, "ARK_UPLOAD_PROXY_MAX_HEIGHT", 1280))
        stack.enter_context(patch.object(providers, "ARK_UPLOAD_PROXY_VIDEO_BITRATE", "2500k"))
        stack.enter_context(patch.object(providers, "ARK_UPLOAD_PROXY_AUDIO_BITRATE", "96k"))
        stack.enter_context(patch.object(providers.shutil, "which", return_value="ffmpeg"))
        create = stack.enter_context(patch.object(providers.tempfile, "mkdtemp", return_value="mock-proxy"))
        run = stack.enter_context(patch.object(providers, "_run_ffmpeg_proxy", side_effect=error))
        original_stat = Path.stat

        def proxy_stat(path, *args, **kwargs):
            if path == Path("mock-proxy/original.ark.mp4"):
                return result
            return original_stat(path, *args, **kwargs)

        stack.enter_context(patch.object(Path, "stat", proxy_stat))
        cleanup = stack.enter_context(patch.object(providers.shutil, "rmtree"))
        return source, create, run, cleanup

    def test_small_source_stays_untouched_even_with_old_small_target(self):
        for size in (1, 20 * MIB, 45 * MIB):
            with (
                self.subTest(size=size),
                patch.object(providers, "ARK_UPLOAD_PROXY_TRIGGER_MB", 45),
                patch.object(providers, "ARK_UPLOAD_PROXY_MAX_MB", 4),
                patch.object(providers.shutil, "which") as which,
                patch.object(providers.tempfile, "mkdtemp") as create,
            ):
                source = self.source(size)
                self.assertEqual(providers._ark_upload_proxy(source, 120), (source, None))
                which.assert_not_called()
                create.assert_not_called()

    def test_unreadable_and_empty_sources_are_actionable_and_not_retryable(self):
        for source in (self.source(0), SimpleNamespace(stat=Mock(side_effect=OSError("missing")))):
            with self.subTest(source=source), self.assertRaises(ArkError) as caught:
                providers._ark_upload_proxy(source, 120)
            self.assertEqual(caught.exception.provider_code, "video_source_unreadable")
            self.assertTrue(processing._is_non_retryable_provider_error(caught.exception))
            self.assertIn("重新上传", processing._task_error_message(caught.exception))

    def test_disabled_proxy_never_submits_large_original(self):
        with patch.object(providers, "ARK_UPLOAD_PROXY_MAX_MB", 0), self.assertRaises(ArkError) as caught:
            providers._ark_upload_proxy(self.source(), 120)
        self.assertEqual(caught.exception.provider_code, "video_proxy_disabled")

    def test_missing_ffmpeg_never_submits_large_original(self):
        with patch.object(providers.shutil, "which", return_value=None), self.assertRaises(ArkError) as caught:
            providers._ark_upload_proxy(self.source(), 120)
        self.assertEqual(caught.exception.provider_code, "video_proxy_unavailable")

    def test_large_source_produces_validated_proxy_without_changing_original(self):
        with ExitStack() as stack:
            source, create, run, cleanup = self.proxy_environment(stack)
            proxy, directory = providers._ark_upload_proxy(source, 120)
            self.assertEqual(proxy, Path("mock-proxy/original.ark.mp4"))
            self.assertEqual(directory, Path("mock-proxy"))
            create.assert_called_once()
            cleanup.assert_not_called()
            source.stat.assert_called_once()
            command = run.call_args.args[0]
            self.assertEqual(command[command.index("-i") + 1], str(source))
            self.assertEqual(command[-1], str(proxy))
            self.assertNotIn("-r", command)
            self.assertNotIn("-ss", command)
            self.assertNotIn("-t", command)
            self.assertIn("0:a:0?", command)
            self.assertIn("-nostdin", command)
            self.assertIn("force_original_aspect_ratio=decrease", command[command.index("-vf") + 1])
            self.assertIn("force_divisible_by=2", command[command.index("-vf") + 1])
            self.assertEqual(command.count("-threads"), 2)

    def test_proxy_progress_100_only_after_successful_validation(self):
        for size, succeeds in ((30 * MIB, True), (41 * MIB, False), (0, False)):
            with self.subTest(size=size), ExitStack() as stack:
                source, _, _, _ = self.proxy_environment(stack, size=size)
                callback = Mock()
                if succeeds:
                    providers._ark_upload_proxy(source, 120, progress_callback=callback)
                    self.assertEqual(callback.call_args.args, ("compressing", 100))
                else:
                    with self.assertRaises(ArkError):
                        providers._ark_upload_proxy(source, 120, progress_callback=callback)
                    self.assertNotIn(("compressing", 100), [call.args for call in callback.call_args_list])
                self.assertEqual(callback.call_args_list[0].args, ("compression_queued", 0))

    def test_small_file_skips_compression_progress(self):
        callback = Mock()
        providers._ark_upload_proxy(self.source(10 * MIB), 120, progress_callback=callback)
        callback.assert_not_called()

    def test_size_checks_reject_empty_and_oversized_proxies_and_clean_up(self):
        for size, code in ((0, "video_proxy_failed"), (41 * MIB, "video_proxy_oversize"),
                           (51 * MIB, "video_proxy_oversize")):
            with self.subTest(size=size), ExitStack() as stack:
                source, _, _, cleanup = self.proxy_environment(stack, size=size)
                with self.assertRaises(ArkError) as caught:
                    providers._ark_upload_proxy(source, 120)
                self.assertEqual(caught.exception.provider_code, code)
                self.assertTrue(processing._is_non_retryable_provider_error(caught.exception))
                self.assertIn("原片", processing._task_error_message(caught.exception))
                self.assertNotIn("Lite", processing._task_error_message(caught.exception))
                cleanup.assert_called_once_with(Path("mock-proxy"), ignore_errors=True)

    def test_transcoder_errors_never_return_large_original_or_call_provider(self):
        for error in (subprocess.CalledProcessError(1, ["ffmpeg"]),
                      subprocess.TimeoutExpired(["ffmpeg"], 1), OSError("cannot encode")):
            with self.subTest(error=error), ExitStack() as stack:
                source, _, run, cleanup = self.proxy_environment(stack, error=error)
                http = stack.enter_context(patch.object(providers, "ark_http"))
                with self.assertRaises(ArkError) as caught:
                    providers.ark_upload_video(source, "key", 120)
                self.assertEqual(caught.exception.provider_code, "video_proxy_failed")
                run.assert_called_once()
                http.assert_not_called()
                cleanup.assert_called_once()

    def test_unknown_duration_is_probed_before_encoding(self):
        with ExitStack() as stack:
            source, _, run, _ = self.proxy_environment(stack)
            probe = stack.enter_context(patch.object(providers, "probe_duration", return_value=120))
            providers._ark_upload_proxy(source)
            probe.assert_called_once_with(source)
            run.assert_called_once()

    def test_invalid_duration_never_encodes_or_submits_unbounded_proxy(self):
        for duration in (0, -1, float("inf"), float("nan")):
            with self.subTest(duration=duration), ExitStack() as stack:
                source, create, run, cleanup = self.proxy_environment(stack)
                stack.enter_context(patch.object(providers, "probe_duration", return_value=duration))
                with self.assertRaises(ArkError) as caught:
                    providers._ark_upload_proxy(source)
                self.assertEqual(caught.exception.provider_code, "video_proxy_duration_unknown")
                create.assert_not_called()
                run.assert_not_called()
                cleanup.assert_not_called()

    def test_temp_directory_creation_failure_is_actionable(self):
        with ExitStack() as stack:
            source, create, run, cleanup = self.proxy_environment(stack)
            create.side_effect = OSError("disk full")
            with self.assertRaises(ArkError) as caught:
                providers._ark_upload_proxy(source, 120)
            self.assertEqual(caught.exception.provider_code, "video_proxy_failed")
            run.assert_not_called()
            cleanup.assert_not_called()

    def test_one_compression_lane_is_held_during_encoding_and_released_on_failure(self):
        with ExitStack() as stack:
            source, _, run, _ = self.proxy_environment(stack)
            lane = threading.BoundedSemaphore(1)
            stack.enter_context(patch.object(providers, "_ark_proxy_semaphore", lane))

            def fail_while_locked(*args, **kwargs):
                acquired = lane.acquire(blocking=False)
                if acquired:
                    lane.release()
                self.assertFalse(acquired)
                raise subprocess.TimeoutExpired(["ffmpeg"], 1)

            run.side_effect = fail_while_locked
            with self.assertRaises(ArkError):
                providers._ark_upload_proxy(source, 120)
            self.assertTrue(lane.acquire(blocking=False))
            lane.release()

    def test_quality_profile_uses_budget_for_six_minute_clip(self):
        with (
            patch.object(providers, "ARK_UPLOAD_PROXY_MAX_MB", 40),
            patch.object(providers, "ARK_UPLOAD_PROXY_VIDEO_BITRATE", "2500k"),
            patch.object(providers, "ARK_UPLOAD_PROXY_AUDIO_BITRATE", "96k"),
            patch.object(providers, "ARK_UPLOAD_PROXY_MAX_HEIGHT", 1280),
        ):
            bitrate, edge = providers._ark_proxy_profile(360)
        self.assertGreater(providers._bitrate_bps(bitrate), 700_000)
        self.assertLess((providers._bitrate_bps(bitrate) + 96_000) * 360 / 8, 40 * MIB)
        self.assertEqual(edge, 1280)

    def test_upload_guard_checks_size_before_reading_or_making_request(self):
        source = self.source(46 * MIB)
        source.read_bytes = Mock()
        with (
            patch.object(providers, "_ark_upload_proxy", return_value=(source, None)),
            patch.object(providers, "ark_http") as http,
            self.assertRaises(ArkError) as caught,
        ):
            providers.ark_upload_video(source, "key", 120)
        self.assertEqual(caught.exception.provider_code, "video_upload_too_large")
        source.read_bytes.assert_not_called()
        http.assert_not_called()

    def test_upload_revalidates_content_after_read(self):
        source = self.source(10)
        # Emulate a file changed after stat without allocating a large buffer.
        class ChangedVideo:
            def __len__(self):
                return 46 * MIB
        source.read_bytes = Mock(return_value=ChangedVideo())
        with (
            patch.object(providers, "_ark_upload_proxy", return_value=(source, None)),
            patch.object(providers, "ark_http") as http,
            self.assertRaises(ArkError) as caught,
        ):
            providers.ark_upload_video(source, "key", 120)
        self.assertEqual(caught.exception.provider_code, "video_upload_too_large")
        http.assert_not_called()

    def test_upload_memory_lane_is_held_before_read_and_released_on_failure(self):
        source = self.source(10)
        lane = threading.BoundedSemaphore(1)

        def fail_while_locked():
            acquired = lane.acquire(blocking=False)
            if acquired:
                lane.release()
            self.assertFalse(acquired)
            raise OSError("file unavailable")

        source.read_bytes = Mock(side_effect=fail_while_locked)
        with (
            patch.object(providers, "_ark_upload_proxy", return_value=(source, None)),
            patch.object(providers, "_ark_upload_body_semaphore", lane),
            self.assertRaises(ArkError) as caught,
        ):
            providers.ark_upload_video(source, "key", 120)
        self.assertEqual(caught.exception.provider_code, "video_source_unreadable")
        self.assertTrue(lane.acquire(blocking=False))
        lane.release()

    def test_upload_failure_cleans_proxy_and_retry_reuses_same_bytes(self):
        proxy = self.source(100)
        proxy.read_bytes = Mock(return_value=b"encoded proxy")
        directory = Path("mock-proxy")
        with (
            patch.object(providers, "_ark_upload_proxy", return_value=(proxy, directory)) as encode,
            patch.object(providers, "ARK_UPLOAD_ATTEMPTS", 2),
            patch.object(providers.time, "sleep"),
            patch.object(providers.shutil, "rmtree") as cleanup,
            patch.object(providers, "ark_http", side_effect=[
                ArkError("temporary", provider_code="transient_network"), {"id": "file"},
            ]) as http,
        ):
            self.assertEqual(providers.ark_upload_video(self.source(), "key", 120), ("file", "key"))
            encode.assert_called_once()
            proxy.read_bytes.assert_called_once()
            self.assertEqual(http.call_count, 2)
            self.assertIs(http.call_args_list[0].args[2], http.call_args_list[1].args[2])
            cleanup.assert_called_once_with(directory, ignore_errors=True)

        with (
            patch.object(providers, "_ark_upload_proxy", return_value=(proxy, directory)),
            patch.object(providers.shutil, "rmtree") as cleanup,
            patch.object(providers, "ark_http", side_effect=ArkError("forbidden", status_code=403)),
            self.assertRaises(ArkError),
        ):
            providers.ark_upload_video(self.source(), "key", 120)
        cleanup.assert_called_once_with(directory, ignore_errors=True)

    def test_url_boundary_uses_files_without_claiming_same_provider_limit(self):
        with (
            patch.object(providers, "ARK_VIDEO_SOURCE_MODE", "auto"),
            patch.object(providers, "ark_account_key", return_value="key"),
            patch.object(providers, "presigned_get") as sign,
            patch.object(providers, "ark_upload_video", return_value=("file", "key")),
            patch.object(providers, "ark_wait_for_file"),
        ):
            result = providers.ark_prepare_video(self.source(45 * MIB), 120, source_uri="s3://bucket/video")
        sign.assert_not_called()
        self.assertEqual(result, {"api_key": "key", "file_id": "file"})


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe required for real encoding")
class VideoProxyEncodingTests(unittest.TestCase):
    def check_encoding(self, width, height, *, audio):
        with tempfile.TemporaryDirectory(prefix="jbb-proxy-test-") as root:
            directory = Path(root)
            original = directory / "original.mp4"
            command = [shutil.which("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error",
                       "-f", "lavfi", "-i", f"testsrc2=size={width}x{height}:rate=25:duration=2"]
            if audio:
                command += ["-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:a", "aac"]
            subprocess.run(command + ["-c:v", "mpeg4", "-q:v", "2", "-y", str(original)],
                           capture_output=True, check=True, timeout=30)
            digest = hashlib.sha256(original.read_bytes()).digest()
            progress = []
            with (
                patch.object(providers, "DATA_DIR", directory),
                patch.object(providers, "ARK_UPLOAD_PROXY_TRIGGER_MB", 0),
                patch.object(providers, "ARK_UPLOAD_PROXY_MAX_MB", 40),
                patch.object(providers, "ARK_UPLOAD_PROXY_MAX_HEIGHT", 160),
                patch.object(providers, "ARK_UPLOAD_PROXY_VIDEO_BITRATE", "100k"),
            ):
                proxy, temporary = providers._ark_upload_proxy(original, 2, progress_callback=lambda stage, percent: progress.append((stage, percent)))
            self.assertNotEqual(proxy, original)
            self.assertIsNotNone(temporary)
            self.assertEqual(hashlib.sha256(original.read_bytes()).digest(), digest)
            self.assertEqual(progress[0], ("compression_queued", 0))
            self.assertEqual(progress[-1], ("compressing", 100))
            encoding_values = [percent for stage, percent in progress if stage == "compressing"]
            self.assertEqual(encoding_values, sorted(encoding_values))
            self.assertTrue(any(0 < percent < 100 for percent in encoding_values))
            output = subprocess.run([shutil.which("ffprobe"), "-v", "error", "-show_streams", "-show_format",
                                     "-of", "json", str(proxy)], capture_output=True, check=True, timeout=10)
            info = json.loads(output.stdout)
            video = next(stream for stream in info["streams"] if stream["codec_type"] == "video")
            self.assertEqual((video["width"], video["height"]), (160, 90) if width > height else (90, 160))
            self.assertEqual(video["r_frame_rate"], "25/1")
            self.assertAlmostEqual(float(info["format"]["duration"]), 2, delta=0.1)
            self.assertEqual(any(stream["codec_type"] == "audio" for stream in info["streams"]), audio)

    def test_portrait_video_retains_aspect_ratio_audio_and_timeline(self):
        self.check_encoding(180, 320, audio=True)

    def test_landscape_video_without_audio_retains_aspect_ratio_and_timeline(self):
        self.check_encoding(320, 180, audio=False)


if __name__ == "__main__":
    unittest.main()
