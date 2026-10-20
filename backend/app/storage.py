"""Local and S3-compatible media storage behind one small interface."""

from __future__ import annotations

import shutil
import tempfile
import hashlib
import time
import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

from .config import (
    DATA_DIR,
    DOWNLOAD_CONCURRENCY,
    MEDIA_TRANSFER_QUEUE_TIMEOUT_SECONDS,
    MEDIA_DOWNLOAD_TIMEOUT_SECONDS,
    MEDIA_PROXY_UPLOAD_TIMEOUT_SECONDS,
    ARK_UPLOAD_MAX_IN_FLIGHT,
    OBJECT_STORAGE_ACCESS_KEY,
    OBJECT_STORAGE_ADDRESSING_STYLE,
    OBJECT_STORAGE_BUCKET,
    OBJECT_STORAGE_LEGACY_BUCKET,
    OBJECT_STORAGE_ENABLED,
    OBJECT_STORAGE_ENDPOINT,
    OBJECT_STORAGE_PREFIX,
    OBJECT_STORAGE_PRESIGN_SECONDS,
    OBJECT_STORAGE_REGION,
    OBJECT_STORAGE_SECRET_KEY,
    UPLOAD_DIR,
)
from .resource_limits import capacity
from .logging_setup import logger


def object_storage_enabled() -> bool:
    return OBJECT_STORAGE_ENABLED


def is_object_uri(value: str | Path) -> bool:
    return str(value or "").startswith("s3://")


def object_uri(task_id: str) -> str:
    key = "/".join(part for part in (OBJECT_STORAGE_PREFIX, f"{task_id}.mp4") if part)
    return f"s3://{OBJECT_STORAGE_BUCKET}/{quote(key, safe='/')}"


def local_upload_path(task_id: str) -> Path:
    return UPLOAD_DIR / f"{task_id}.mp4"


def _object_parts(uri: str | Path) -> tuple[str, str]:
    parsed = urlparse(str(uri))
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.lstrip("/"):
        raise ValueError("无效的对象存储地址")
    bucket = OBJECT_STORAGE_BUCKET if parsed.netloc == OBJECT_STORAGE_LEGACY_BUCKET and OBJECT_STORAGE_LEGACY_BUCKET else parsed.netloc
    return bucket, unquote(parsed.path.lstrip("/"))


def _client(*, delivery: bool = False) -> Any:
    if not OBJECT_STORAGE_ENABLED:
        raise RuntimeError("对象存储尚未配置")
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:  # pragma: no cover - deployment dependency
        raise RuntimeError("对象存储依赖未安装") from exc
    return boto3.client(
        "s3",
        endpoint_url=OBJECT_STORAGE_ENDPOINT,
        region_name=OBJECT_STORAGE_REGION,
        aws_access_key_id=OBJECT_STORAGE_ACCESS_KEY,
        aws_secret_access_key=OBJECT_STORAGE_SECRET_KEY,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": OBJECT_STORAGE_ADDRESSING_STYLE},
            connect_timeout=10 if delivery else 60,
            read_timeout=30 if delivery else 60,
            retries={"total_max_attempts": 2, "mode": "standard"} if delivery else {"max_attempts": 4, "mode": "adaptive"},
        ),
    )


def presigned_put(uri: str, content_type: str = "video/mp4") -> dict[str, Any]:
    bucket, key = _object_parts(uri)
    url = _client().generate_presigned_url(
        "put_object",
        Params={"Bucket": bucket, "Key": key, "ContentType": content_type},
        ExpiresIn=OBJECT_STORAGE_PRESIGN_SECONDS,
        HttpMethod="PUT",
    )
    return {"url": url, "method": "PUT", "headers": {"Content-Type": content_type}, "mode": "object"}


def presigned_get(uri: str | Path) -> str:
    """Grant temporary read access to one video without making it public."""
    bucket, key = _object_parts(uri)
    return str(_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=max(3600, OBJECT_STORAGE_PRESIGN_SECONDS),
        HttpMethod="GET",
    ))


def media_info(uri: str | Path) -> dict[str, Any] | None:
    if is_object_uri(uri):
        try:
            bucket, key = _object_parts(uri)
            payload = _client(delivery=True).head_object(Bucket=bucket, Key=key)
            return {"size": int(payload.get("ContentLength") or 0), "etag": str(payload.get("ETag") or "").strip('"')}
        except Exception:
            return None
    path = Path(uri)
    try:
        stat = path.stat()
        return {"size": stat.st_size, "etag": "", "mtime_ns": stat.st_mtime_ns} if path.is_file() else None
    except OSError:
        return None


def media_exists(uri: str | Path) -> bool:
    return media_info(uri) is not None


def upload_recognition_proxy(path: Path, identity: str) -> str:
    """Store a private, immutable delivery copy under a separate object prefix."""
    from .config import ARK_VIDEO_SAFE_BYTES
    size = path.stat().st_size
    if not 0 < size <= ARK_VIDEO_SAFE_BYTES:
        raise ValueError("识别副本超过安全大小上限")
    key = "/".join(part for part in (OBJECT_STORAGE_PREFIX, "recognition", hashlib.sha256(identity.encode()).hexdigest() + ".mp4") if part)
    # Kill and reap the transport before allowing Files fallback. A timed-out
    # thread would otherwise keep uploading while the fallback runs alongside it.
    with capacity("media-upload", ARK_UPLOAD_MAX_IN_FLIGHT,
                  timeout=MEDIA_TRANSFER_QUEUE_TIMEOUT_SECONDS):
        _bounded_transfer("upload", OBJECT_STORAGE_BUCKET, key, path,
                          MEDIA_PROXY_UPLOAD_TIMEOUT_SECONDS)
    return f"s3://{OBJECT_STORAGE_BUCKET}/{quote(key, safe='/')}"


def _bounded_transfer(operation: str, bucket: str, key: str, path: Path, timeout: float) -> None:
    payload = json.dumps(dict(operation=operation, bucket=bucket, key=key, path=str(path.resolve())))
    try:
        result = subprocess.run([sys.executable, "-m", "app.storage"], input=payload,
                                text=True, encoding="utf-8", capture_output=True, timeout=timeout,
                                cwd=Path(__file__).resolve().parents[1])
    except subprocess.TimeoutExpired:
        # subprocess.run kills and waits for its child, including socket stalls
        # during continuously progressing transfers (not covered by read_timeout).
        raise TimeoutError("视频传输超时，系统将自动尝试恢复") from None
    if result.returncode:
        # Never expose SDK output: it may contain signed URLs or credentials.
        raise OSError("视频传输暂时失败，系统将自动尝试恢复")


def _transfer_main() -> None:
    """Isolated transport; credentials remain in inherited environment only."""
    value = json.load(sys.stdin)
    client = _client(delivery=True)
    if value["operation"] == "upload":
        path = Path(value["path"])
        with path.open("rb") as body:
            client.put_object(Bucket=value["bucket"], Key=value["key"], Body=body,
                              ContentLength=path.stat().st_size, ContentType="video/mp4")
    elif value["operation"] == "download":
        from boto3.s3.transfer import TransferConfig
        client.download_file(value["bucket"], value["key"], value["path"],
                             Config=TransferConfig(max_concurrency=1, use_threads=False,
                                                   num_download_attempts=2))
    else:
        raise ValueError("Unsupported media operation")


def delete_media(uri: str | Path) -> None:
    if is_object_uri(uri):
        bucket, key = _object_parts(uri)
        _client().delete_object(Bucket=bucket, Key=key)
        return
    Path(uri).unlink(missing_ok=True)


def materialize_media(uri: str | Path, task_id: str) -> tuple[Path, Path | None]:
    """Return a local media path and an optional temporary directory to clean."""
    if not is_object_uri(uri):
        return Path(uri), None
    bucket, key = _object_parts(uri)
    temp_dir = Path(tempfile.mkdtemp(prefix=f"jbb-media-{task_id[:18]}-", dir=DATA_DIR))
    target = temp_dir / "source.mp4"
    try:
        with capacity("media-download", DOWNLOAD_CONCURRENCY,
                      timeout=MEDIA_TRANSFER_QUEUE_TIMEOUT_SECONDS) as queue_ms:
            started = time.perf_counter()
            completed = False
            try:
                _bounded_transfer("download", bucket, key, target, MEDIA_DOWNLOAD_TIMEOUT_SECONDS)
                if not target.is_file() or target.stat().st_size <= 0:
                    raise OSError("对象存储下载为空")
                completed = True
            finally:
                logger.info("media_step task_id=%s operation=download queue_ms=%.1f duration_ms=%.1f completed=%s", task_id, queue_ms, (time.perf_counter() - started) * 1000, completed)
        return target, temp_dir
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def cleanup_materialized(temp_dir: Path | None) -> None:
    if temp_dir is not None:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    try:
        _transfer_main()
    except Exception:
        sys.exit(1)
