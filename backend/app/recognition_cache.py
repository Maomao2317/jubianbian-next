"""Task-scoped checkpoints; never persist provider keys or signed URLs."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from copy import deepcopy
from typing import Any

from . import config
from .evidence import EVIDENCE_VERSION
from .logging_setup import logger
from .storage import media_info

_prune_lock = threading.Lock()
_next_prune = 0.0


def cache_directory(task_id: str) -> Path:
    return config.DATA_DIR / "recognition-cache" / hashlib.sha256(task_id.encode()).hexdigest()


def restore_cached_scene_groups(task_id: str, script: dict[str, Any]) -> dict[str, Any]:
    """Recover legacy repair parent identities from retained model audits.

    Only exact text/speaker/time matches within an identical setting qualify.
    Ambiguous matches and unavailable audits leave the scene unchanged. This
    read adapter never changes saved business data or any screenplay block.
    """
    def identity(block):
        return tuple(block.get(key) for key in ("type", "text", "speaker", "startSec", "endSec"))

    setting = ("location", "timeOfDay", "interiorExterior", "segmentType")
    parents = []
    for path in sorted(cache_directory(task_id).glob("model_*.json")):
        if path.name != "model_initial.json" and not path.name.startswith("model_segment_"):
            continue
        try:
            audit = json.loads(path.read_text(encoding="utf-8"))["data"]["normalizedScript"]
            for index, scene in enumerate(audit.get("scenes") or []):
                blocks = scene.get("blocks") or []
                if len(blocks) > 1:
                    group = hashlib.sha256(f"{task_id}:{path.name}:{index}".encode()).hexdigest()
                    parents.append((tuple(scene.get(key) for key in setting),
                                    {identity(block) for block in blocks}, group))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    if not parents:
        return script
    result = deepcopy(script)
    for scene in result.get("scenes") or []:
        if scene.get("continuityGroup") or not scene.get("blocks"):
            continue
        keys = {identity(block) for block in scene["blocks"]}
        groups = {group for fields, blocks, group in parents
                  if fields == tuple(scene.get(key) for key in setting) and keys <= blocks}
        if len(groups) == 1:
            scene["continuityGroup"] = groups.pop()
    return result


class RecognitionCache:
    def __init__(self, task_id: str, source_uri: str, info: dict[str, Any]):
        self.directory = cache_directory(task_id)
        settings = {name: getattr(config, name) for name in (
            "LOCAL_ASR_MODEL", "LOCAL_ASR_DEVICE", "LOCAL_ASR_COMPUTE_TYPE", "LOCAL_ASR_LANGUAGE",
            "LOCAL_ASR_BEAM_SIZE", "LOCAL_ASR_VAD_MIN_SPEECH_MS", "LOCAL_ASR_VAD_MIN_SILENCE_MS",
            "LOCAL_ASR_VAD_SPEECH_PAD_MS", "OCR_LANG", "OCR_MAX_FRAMES", "OCR_SAMPLE_INTERVAL_SECONDS",
            "ARK_UPLOAD_PROXY_TRIGGER_MB", "ARK_UPLOAD_PROXY_MAX_MB", "ARK_UPLOAD_PROXY_MAX_HEIGHT",
            "ARK_UPLOAD_PROXY_VIDEO_BITRATE", "ARK_UPLOAD_PROXY_AUDIO_BITRATE", "ARK_UPLOAD_PROXY_PRESET",
        )}
        fingerprint = ["recognition-cache-1", EVIDENCE_VERSION, task_id, source_uri, info, settings]
        self.identity = hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()

    def scoped(self, scope: str) -> RecognitionCache:
        """Isolate a clip's media checkpoints from the full video and other clips."""
        child = object.__new__(RecognitionCache)
        child.identity = hashlib.sha256(f"{self.identity}:{scope}".encode()).hexdigest()
        child.directory = self.directory / "segments" / child.identity
        return child

    def load(self, kind: str) -> dict[str, Any] | None:
        try:
            value = json.loads((self.directory / f"{kind}.json").read_text(encoding="utf-8"))
            if value.get("identity") == self.identity and isinstance(value.get("data"), dict):
                return value["data"]
        except (OSError, ValueError, AttributeError):
            pass
        return None

    def save(self, kind: str, data: dict[str, Any]) -> None:
        temporary = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.directory, delete=False) as handle:
                temporary = Path(handle.name)
                json.dump({"identity": self.identity, "data": data}, handle, ensure_ascii=False, separators=(",", ":"))
            os.replace(temporary, self.directory / f"{kind}.json")
        except OSError:
            # Losing a checkpoint must not turn successful recognition into failure.
            logger.warning("recognition_cache_write_failed kind=%s", kind)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def proxy(self) -> Path | None:
        metadata = self.load("proxy")
        path = self.directory / "proxy.mp4"
        try:
            if metadata and 0 < path.stat().st_size == metadata.get("size") <= config.ARK_VIDEO_SAFE_BYTES:
                if hashlib.sha256(path.read_bytes()).hexdigest() == metadata.get("sha256"):
                    return path
        except OSError:
            pass
        return None

    def save_proxy(self, source: Path) -> Path:
        target = self.directory / "proxy.mp4"
        temporary = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self.directory, delete=False) as handle:
                temporary = Path(handle.name)
            shutil.copyfile(source, temporary)
            os.replace(temporary, target)
            self.save("proxy", {"size": target.stat().st_size, "sha256": hashlib.sha256(target.read_bytes()).hexdigest()})
            return target
        except OSError:
            logger.warning("recognition_cache_write_failed kind=proxy")
            return source
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def adopt_proxy(self, source: Path) -> Path:
        """Move a freshly encoded proxy into the cache when possible.

        Compression and the cache live under ``DATA_DIR`` in normal
        deployments, so an atomic rename avoids copying and rereading a
        40 MiB delivery file before the upload starts.  Fall back to the
        original copy path for unusual cross-device or mocked filesystems.
        """
        target = self.directory / "proxy.mp4"
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
        except OSError:
            return self.save_proxy(source)
        try:
            self.save("proxy", {
                "size": target.stat().st_size,
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            })
        except OSError:
            logger.warning("recognition_cache_write_failed kind=proxy_metadata")
        return target


def _media_directories(directory: Path) -> list[Path]:
    return [directory, *sorted((directory / "segments").glob("*/"))]


def discard_local_proxy(task_id: str) -> None:
    """Release bulk disk space after success; evidence/remote checkpoints remain."""
    for directory in _media_directories(cache_directory(task_id)):
        for name in ("proxy.mp4", "proxy.json"):
            try:
                (directory / name).unlink(missing_ok=True)
            except OSError:
                logger.warning("recognition_cache_cleanup_failed kind=proxy")


def _prune_local_proxies() -> None:
    """Failed-task proxies expire after a day; active tasks are always protected."""
    global _next_prune
    with _prune_lock:
        if time.monotonic() < _next_prune or not config.DB_PATH.exists():
            return
        _next_prune = time.monotonic() + 3600
    try:
        with closing(sqlite3.connect(config.DB_PATH)) as connection:
            active = {cache_directory(row[0]).name for row in connection.execute(
                "SELECT id FROM tasks WHERE status IN ('queued','running')")}
        root = config.DATA_DIR / "recognition-cache"
        for directory in root.iterdir() if root.exists() else []:
            if directory.name not in active:
                for media_directory in _media_directories(directory):
                    proxy = media_directory / "proxy.mp4"
                    if proxy.is_file() and proxy.stat().st_mtime < time.time() - 86400:
                        proxy.unlink(missing_ok=True)
                        (media_directory / "proxy.json").unlink(missing_ok=True)
    except (OSError, sqlite3.Error):
        logger.warning("recognition_cache_prune_failed")


def cleanup_task_cache(task_id: str) -> None:
    """Used by normal task retention, with a strict guard against original keys."""
    from .storage import delete_media, _object_parts
    directory = cache_directory(task_id)
    for media_directory in _media_directories(directory):
        try:
            value = json.loads((media_directory / "delivery.json").read_text(encoding="utf-8"))
            uri = value["data"]["uri"]
            bucket, key = _object_parts(uri)
            prefix = "/".join(part for part in (config.OBJECT_STORAGE_PREFIX, "recognition", "") if part) + "/"
            filename = key.removeprefix(prefix)
            if bucket == config.OBJECT_STORAGE_BUCKET and key.startswith(prefix) and len(filename) == 68 and filename.endswith(".mp4"):
                int(filename[:-4], 16)
                delete_media(uri)
        except (OSError, ValueError, KeyError, TypeError):
            pass
        except Exception:
            logger.warning("recognition_cache_cleanup_failed kind=object_proxy")
    shutil.rmtree(directory, ignore_errors=True)


def recognition_cache(task_id: str, source_uri: str) -> RecognitionCache | None:
    _prune_local_proxies()
    info = media_info(source_uri)
    # Without a source identity, never risk reusing another version's evidence.
    if not info or not info.get("size") or not (info.get("etag") or info.get("mtime_ns")):
        return None
    return RecognitionCache(task_id, source_uri, info)
