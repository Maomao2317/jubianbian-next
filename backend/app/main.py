from __future__ import annotations

import asyncio
from collections import deque
from contextvars import ContextVar
import json
import logging
from logging.handlers import RotatingFileHandler
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from threading import Lock
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.error import HTTPError, URLError
from urllib.request import Request as UrlRequest, urlopen

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.staticfiles import StaticFiles


ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.getenv("JBB_DATA_DIR", ROOT / "data"))
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "jubianbian.sqlite3"
FRONTEND_DIR = ROOT / "web-staging"
MAX_UPLOAD_BYTES = int(os.getenv("JBB_MAX_UPLOAD_MB", "500")) * 1024 * 1024
MAX_DURATION_MINUTES = int(os.getenv("JBB_MAX_DURATION_MINUTES", "6"))
MAX_DURATION_SECONDS = MAX_DURATION_MINUTES * 60
RATE_LIMIT_API_PER_MINUTE = max(1, int(os.getenv("JBB_RATE_LIMIT_API_PER_MINUTE", "120")))
RATE_LIMIT_CREATE_PER_WINDOW = max(1, int(os.getenv("JBB_RATE_LIMIT_CREATE_PER_WINDOW", "10")))
RATE_LIMIT_CREATE_WINDOW_SECONDS = max(60, int(os.getenv("JBB_RATE_LIMIT_CREATE_WINDOW_SECONDS", "600")))
TRUST_PROXY_HEADERS = os.getenv("JBB_TRUST_PROXY_HEADERS", "0").strip().lower() in {"1", "true", "yes"}
JBB_ENVIRONMENT = os.getenv("JBB_ENVIRONMENT", "production").strip() or "production"
JBB_LOG_LEVEL = os.getenv("JBB_LOG_LEVEL", "INFO").strip().upper() or "INFO"
JBB_LOG_MAX_BYTES = max(64 * 1024, int(os.getenv("JBB_LOG_MAX_BYTES", str(5 * 1024 * 1024))))
JBB_LOG_BACKUP_COUNT = max(1, int(os.getenv("JBB_LOG_BACKUP_COUNT", "5")))
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
OPENAI_TRANSCRIPTION_MODEL = os.getenv("OPENAI_TRANSCRIPTION_MODEL", "whisper-1")
OPENAI_TEXT_MODEL = os.getenv("OPENAI_TEXT_MODEL", "gpt-4o-mini")
ARK_API_KEY = os.getenv("ARK_API_KEY", "").strip()
ARK_BASE_URL = os.getenv("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3").rstrip("/")
ARK_MODEL = os.getenv("ARK_MODEL", "doubao-seed-2-0-lite-260428").strip()
ARK_VIDEO_FPS = max(0.2, min(5.0, float(os.getenv("ARK_VIDEO_FPS", "0.5"))))
ARK_FILE_POLL_SECONDS = max(1.0, float(os.getenv("ARK_FILE_POLL_SECONDS", "2")))
ARK_FILE_POLL_TIMEOUT_SECONDS = max(30.0, float(os.getenv("ARK_FILE_POLL_TIMEOUT_SECONDS", "300")))
ARK_FALLBACK_ON_ERROR = os.getenv("ARK_FALLBACK_ON_ERROR", "1").strip().lower() in {"1", "true", "yes", "on"}

DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR = DATA_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
APP_STARTED_MONOTONIC = time.monotonic()
REQUEST_ID = ContextVar("request_id", default="-")


def _configure_logging() -> logging.Logger:
    logger = logging.getLogger("jubianbian")
    logger.setLevel(getattr(logging, JBB_LOG_LEVEL, logging.INFO))
    logger.propagate = False
    if logger.handlers:
        return logger
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    file_handler = RotatingFileHandler(
        LOG_DIR / "app.log",
        maxBytes=JBB_LOG_MAX_BYTES,
        backupCount=JBB_LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(stream)
    logger.addHandler(file_handler)
    return logger


logger = _configure_logging()


def safe_error_text(error: BaseException, limit: int = 1000) -> str:
    message = str(error) or error.__class__.__name__
    for secret in (ARK_API_KEY, OPENAI_API_KEY):
        if secret:
            message = message.replace(secret, "[REDACTED]")
    return message[:limit]

app = FastAPI(title="剧编编 API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class RequestLogMiddleware(BaseHTTPMiddleware):
    """Emit one request record with a correlation id and elapsed time."""

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        token = REQUEST_ID.set(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception as exc:  # pragma: no cover - defensive middleware boundary
            elapsed_ms = (time.perf_counter() - started) * 1000
            logger.exception(
                "request_failed request_id=%s method=%s path=%s duration_ms=%.1f error=%s",
                request_id,
                request.method,
                request.url.path,
                elapsed_ms,
                safe_error_text(exc),
            )
            raise
        finally:
            REQUEST_ID.reset(token)
        elapsed_ms = (time.perf_counter() - started) * 1000
        response.headers["X-Request-ID"] = request_id
        message = "request_complete request_id=%s method=%s path=%s status=%s duration_ms=%.1f"
        if request.url.path == "/api/health":
            logger.debug(message, request_id, request.method, request.url.path, response.status_code, elapsed_ms)
        else:
            logger.info(message, request_id, request.method, request.url.path, response.status_code, elapsed_ms)
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Small single-process limiter for the public test deployment.

    It is intentionally conservative and dependency-free. A reverse proxy or
    shared Redis limiter should replace this when the service becomes multi-
    process or multi-instance.
    """

    def __init__(self, application: Any) -> None:
        super().__init__(application)
        self._lock = Lock()
        self._buckets: dict[tuple[str, str], deque[float]] = {}

    @staticmethod
    def _client_ip(request: Request) -> str:
        if TRUST_PROXY_HEADERS:
            forwarded = request.headers.get("x-forwarded-for", "")
            if forwarded:
                return forwarded.split(",", 1)[0].strip() or "unknown"
        return request.client.host if request.client else "unknown"

    def _check(self, bucket_name: str, client_ip: str, limit: int, window: int) -> tuple[bool, int]:
        now = time.monotonic()
        key = (bucket_name, client_ip)
        with self._lock:
            bucket = self._buckets.setdefault(key, deque())
            cutoff = now - window
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                retry_after = max(1, int(bucket[0] + window - now))
                return False, retry_after
            bucket.append(now)
            # Keep the in-memory map bounded after long-running idle periods.
            if len(self._buckets) > 5000:
                self._buckets = {
                    item_key: item_bucket
                    for item_key, item_bucket in self._buckets.items()
                    if item_bucket and item_bucket[-1] > now - window
                }
        return True, 0

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        path = request.url.path
        if not path.startswith("/api/") or path == "/api/health":
            return await call_next(request)

        client_ip = self._client_ip(request)
        if request.method == "POST" and path == "/api/tasks":
            content_length = request.headers.get("content-length")
            if content_length and int(content_length) > MAX_UPLOAD_BYTES + 2 * 1024 * 1024:
                return JSONResponse(
                    {"detail": f"文件超过 {MAX_UPLOAD_BYTES // (1024 * 1024)} MB"},
                    status_code=413,
                )
            allowed, retry_after = self._check(
                "create", client_ip, RATE_LIMIT_CREATE_PER_WINDOW, RATE_LIMIT_CREATE_WINDOW_SECONDS
            )
            limit = RATE_LIMIT_CREATE_PER_WINDOW
            window = RATE_LIMIT_CREATE_WINDOW_SECONDS
        else:
            allowed, retry_after = self._check("api", client_ip, RATE_LIMIT_API_PER_MINUTE, 60)
            limit = RATE_LIMIT_API_PER_MINUTE
            window = 60

        if not allowed:
            return JSONResponse(
                {"detail": "请求过于频繁，请稍后再试"},
                status_code=429,
                headers={"Retry-After": str(retry_after), "X-RateLimit-Limit": str(limit)},
            )

        response = await call_next(request)
        response.headers.setdefault("X-RateLimit-Limit", str(limit))
        response.headers.setdefault("X-RateLimit-Window", str(window))
        return response


app.add_middleware(RateLimitMiddleware)
app.add_middleware(RequestLogMiddleware)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def db() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    with db() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                file_name TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                file_size INTEGER NOT NULL DEFAULT 0,
                mime_type TEXT NOT NULL DEFAULT 'video/mp4',
                duration_sec REAL NOT NULL DEFAULT 0,
                estimated_minutes INTEGER NOT NULL DEFAULT 0,
                credits_used INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                stage TEXT NOT NULL,
                progress_percent INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                result_json TEXT,
                quality_json TEXT,
                provider TEXT,
                model TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                total_tokens INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            )
            """
        )
        # Keep existing local databases compatible with the provider metadata
        # added for real Ark calls. SQLite has no IF NOT EXISTS for columns.
        columns = {item[1] for item in connection.execute("PRAGMA table_info(tasks)").fetchall()}
        for name, definition in (
            ("provider", "TEXT"),
            ("model", "TEXT"),
            ("input_tokens", "INTEGER"),
            ("output_tokens", "INTEGER"),
            ("total_tokens", "INTEGER"),
        ):
            if name not in columns:
                connection.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS task_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                status TEXT,
                stage TEXT,
                progress_percent INTEGER,
                message TEXT NOT NULL DEFAULT '',
                duration_ms REAL,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_task_events_task_id ON task_events(task_id, id)")


init_db()


def row_to_task(row: sqlite3.Row) -> dict[str, Any]:
    task = dict(row)
    raw_result = json.loads(task.pop("result_json")) if task.get("result_json") else None
    # Normalize on read as well as on generation so older tasks immediately
    # benefit from the same punctuation, VO, scene-granularity and micro-detail
    # rules without rewriting their stored source response.
    if raw_result:
        try:
            task["result"] = normalize_script(raw_result, task.get("title") or "未命名视频")
        except (ArkError, TypeError, ValueError, KeyError):
            task["result"] = raw_result
    else:
        task["result"] = None
    task["quality"] = json.loads(task.pop("quality_json")) if task.get("quality_json") else None
    task["fileName"] = task.pop("file_name")
    task["fileSize"] = task.pop("file_size")
    task["mimeType"] = task.pop("mime_type")
    task["durationSec"] = task.pop("duration_sec")
    task["estimatedMinutes"] = task.pop("estimated_minutes")
    task["creditsUsed"] = task.pop("credits_used")
    task["progressPercent"] = task.pop("progress_percent")
    task["createdAt"] = task.pop("created_at")
    task["updatedAt"] = task.pop("updated_at")
    task["completedAt"] = task.pop("completed_at")
    task["provider"] = task.pop("provider", None)
    task["model"] = task.pop("model", None)
    input_tokens = task.pop("input_tokens", None)
    output_tokens = task.pop("output_tokens", None)
    total_tokens = task.pop("total_tokens", None)
    task["usage"] = (
        {
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "totalTokens": total_tokens,
        }
        if any(value is not None for value in (input_tokens, output_tokens, total_tokens))
        else None
    )
    task.pop("stored_path", None)
    return task


def get_task(task_id: str) -> dict[str, Any] | None:
    with db() as connection:
        row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return row_to_task(row) if row else None


def update_task(task_id: str, **values: Any) -> None:
    if not values:
        return
    values["updated_at"] = now_iso()
    assignments = ", ".join(f"{key} = ?" for key in values)
    with db() as connection:
        connection.execute(
            f"UPDATE tasks SET {assignments} WHERE id = ?",
            (*values.values(), task_id),
        )


def record_task_event(
    task_id: str,
    event_type: str,
    message: str = "",
    *,
    status: str | None = None,
    stage: str | None = None,
    progress_percent: int | None = None,
    duration_ms: float | None = None,
) -> None:
    """Persist a small task timeline while keeping log messages searchable."""
    try:
        with db() as connection:
            connection.execute(
                """
                INSERT INTO task_events
                  (task_id, event_type, status, stage, progress_percent, message, duration_ms, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    event_type,
                    status,
                    stage,
                    progress_percent,
                    message[:1000],
                    duration_ms,
                    now_iso(),
                ),
            )
    except sqlite3.Error:
        logger.exception("task_event_persist_failed task_id=%s event_type=%s", task_id, event_type)


def mark_task_stage(
    task_id: str,
    *,
    status: str,
    stage: str,
    progress_percent: int,
    message: str,
    event_type: str = "stage",
) -> None:
    update_task(task_id, status=status, stage=stage, progress_percent=progress_percent, error=None)
    record_task_event(
        task_id,
        event_type,
        message,
        status=status,
        stage=stage,
        progress_percent=progress_percent,
    )
    logger.info(
        "task_stage task_id=%s status=%s stage=%s progress=%s message=%s request_id=%s",
        task_id,
        status,
        stage,
        progress_percent,
        message,
        REQUEST_ID.get(),
    )


def task_events(task_id: str, limit: int = 100) -> list[dict[str, Any]]:
    with db() as connection:
        rows = connection.execute(
            """
            SELECT event_type, status, stage, progress_percent, message, duration_ms, created_at
            FROM task_events WHERE task_id = ? ORDER BY id DESC LIMIT ?
            """,
            (task_id, max(1, min(limit, 200))),
        ).fetchall()
    return [
        {
            "type": row["event_type"],
            "status": row["status"],
            "stage": row["stage"],
            "progressPercent": row["progress_percent"],
            "message": row["message"],
            "durationMs": row["duration_ms"],
            "createdAt": row["created_at"],
        }
        for row in reversed(rows)
    ]


def task_counts() -> dict[str, int]:
    with db() as connection:
        rows = connection.execute("SELECT status, COUNT(*) AS count FROM tasks GROUP BY status").fetchall()
    counts = {str(row["status"]): int(row["count"]) for row in rows}
    return {
        "queued": counts.get("queued", 0),
        "running": counts.get("running", 0),
        "done": counts.get("done", 0),
        "failed": counts.get("failed", 0),
        "total": sum(counts.values()),
    }


def task_row(task_id: str) -> sqlite3.Row | None:
    with db() as connection:
        return connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()


def safe_filename(name: str) -> str:
    name = Path(name or "video.mp4").name
    name = re.sub(r"[^\w\-.\u4e00-\u9fff ]+", "_", name).strip(" .")
    return name or "video.mp4"


def title_from_filename(name: str) -> str:
    return re.sub(r"\.[^.]+$", "", safe_filename(name)).strip() or "未命名视频"


def probe_duration(path: Path) -> float | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        value = float(result.stdout.strip())
        return value if value > 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def sample_script(title: str, duration_sec: float, transcript: str = "") -> dict[str, Any]:
    """Return a stable script shape even when no AI provider is configured.

    This fallback keeps the upload -> processing -> export loop usable locally.
    The provider hook below can replace the scene content without changing the API.
    """
    action = "视频已完成音视频素材整理，建议根据成片快速核对人物名与专有名词。"
    if transcript:
        action = transcript[:500]
    return {
        "version": "1.0",
        "title": title,
        "characters": [],
        "scenes": [
            {
                "id": "scene_001",
                "heading": "1-1 不明 不明 未标注地点",
                "location": "待补充",
                "characters": [],
                # A duration note is metadata, not an environment description.
                # Keeping it out of the script avoids the unhelpful generic first
                # sentence that previously appeared before every scene.
                "environment": "",
                "summary": "开场信息不足，无法从当前素材确认人物关系和下一步行动。",
                "blocks": [{"type": "action", "text": action}],
            }
        ],
    }


def multipart_body(fields: dict[str, str], file_field: str, file_name: str, file_data: bytes, content_type: str) -> tuple[bytes, str]:
    boundary = f"----JBB{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for key, value in fields.items():
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode(),
            value.encode(),
            b"\r\n",
        ])
    chunks.extend([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="{file_field}"; filename="{file_name}"\r\n'.encode(),
        f"Content-Type: {content_type}\r\n\r\n".encode(),
        file_data,
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    return b"".join(chunks), boundary


class ArkError(RuntimeError):
    """A provider error with a user-safe message (never includes the API key)."""

    def __init__(self, message: str, *, status_code: int | None = None, provider_code: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.provider_code = provider_code


def ark_fallback_message(error: ArkError) -> str:
    """Explain a provider downgrade without exposing provider credentials/details."""
    if error.status_code == 429 or error.provider_code.lower() in {"setlimitexceeded", "ratelimitexceeded"}:
        return "方舟模型当前达到推理限额（HTTP 429），本次已切换到备用识别链路。"
    return "方舟识别服务暂时不可用，本次已切换到备用识别链路。"


def ark_http(method: str, path: str, data: bytes | None = None, content_type: str = "application/json") -> Any:
    if not ARK_API_KEY:
        raise ArkError("未配置方舟 API Key，请在 fangzhou.env 中填写 ARK_API_KEY")
    request = UrlRequest(
        f"{ARK_BASE_URL}{path}",
        data=data,
        headers={
            "Authorization": f"Bearer {ARK_API_KEY}",
            "Content-Type": content_type,
        },
        method=method,
    )
    try:
        with urlopen(request, timeout=300) as response:
            raw = response.read()
    except HTTPError as exc:
        detail = ""
        provider_code = ""
        try:
            payload = json.loads(exc.read().decode("utf-8", errors="replace"))
            detail = str(payload.get("message") or payload.get("error") or payload.get("detail") or "")
            provider_code = str(payload.get("code") or "") if isinstance(payload, dict) else ""
        except (OSError, ValueError):
            pass
        if exc.code == 429:
            # Provider 429 bodies may contain account identifiers and internal
            # quota text. Keep the actionable cause without echoing that data.
            detail = "模型当前达到推理限额或已暂停，请在方舟模型激活页调整限额或关闭 Safe Experience Mode"
        suffix = f"：{detail[:300]}" if detail else ""
        raise ArkError(
            f"方舟接口请求失败（HTTP {exc.code}）{suffix}",
            status_code=exc.code,
            provider_code=provider_code,
        ) from exc
    except URLError as exc:
        raise ArkError(f"无法连接方舟接口：{exc.reason}") from exc
    except OSError as exc:
        raise ArkError(f"方舟接口网络错误：{exc}") from exc
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ArkError("方舟接口返回了无法解析的响应") from exc


def ark_upload_video(path: Path) -> str:
    # The Files API accepts a local video and lets the model reuse it by file_id.
    # The upload is intentionally done in the worker thread so FastAPI stays responsive.
    fields = {
        "purpose": "user_data",
        "preprocess_configs": json.dumps({"video": {"fps": ARK_VIDEO_FPS}}, separators=(",", ":")),
    }
    body, boundary = multipart_body(
        fields,
        "file",
        path.name,
        path.read_bytes(),
        "video/mp4",
    )
    payload = ark_http("POST", "/files", body, f"multipart/form-data; boundary={boundary}")
    file_id = None
    if isinstance(payload, dict):
        file_id = payload.get("id") or payload.get("file_id")
    if not file_id:
        raise ArkError("方舟上传成功但没有返回 file_id")
    return str(file_id)


def ark_wait_for_file(file_id: str) -> None:
    deadline = time.monotonic() + ARK_FILE_POLL_TIMEOUT_SECONDS
    encoded_id = quote(file_id, safe="")
    while True:
        payload = ark_http("GET", f"/files/{encoded_id}")
        status = str(payload.get("status") or payload.get("state") or "").lower() if isinstance(payload, dict) else ""
        if not status or status in {"active", "ready", "uploaded", "succeeded", "completed"}:
            return
        if status in {"failed", "error", "expired", "cancelled", "canceled"}:
            message = payload.get("message") if isinstance(payload, dict) else ""
            raise ArkError(f"方舟视频预处理失败：{message or status}")
        if time.monotonic() >= deadline:
            raise ArkError("方舟视频预处理超时，请稍后重试")
        time.sleep(ARK_FILE_POLL_SECONDS)


def ark_prompt(title: str, duration_sec: float) -> str:
    return (
        "你是专业的中文短剧剧本整理助手。请先完整核对视频中的声音、对白和连续动作，再把它还原成按原时间顺序排列、可直接继续编剧的‘场景剧本’。"
        "这不是分镜表，也不是剪辑单：不要按一个镜头一个 block 输出，不要写正反打、特写、近景、推拉摇移或镜头切换。"
        "只返回一个合法 JSON 对象，不要 Markdown、不要代码围栏、不要解释。"
        "JSON 结构必须是："
        "{version:string,title:string,characters:string[],characterProfiles:[{id:string,name:string,aliases:string[],appearance:string,clothing:string}],"
        "scenes:[{id:string,heading:string,location:string,timeOfDay:string,interiorExterior:string,segmentType:'main'|'recap'|'trailer'|'title_card'|'credits',summary:string,characters:string[],environment:string,"
        "blocks:[{type:'action',text:string,emotion:string,startSec:number,endSec:number}|"
        "{type:'dialogue',speaker:string,emotion:string,emotionImportant:boolean,text:string,confidence:'high'|'medium'|'low',uncertain:boolean,startSec:number,endSec:number}|"
        "{type:'vo',speaker:string,emotion:string,text:string,confidence:'high'|'medium'|'low',uncertain:boolean,inferred:boolean,startSec:number,endSec:number}|"
        "{type:'sound',category:'effect'|'ambience',source:'heard',importance:'plot'|'atmosphere',text:string,startSec:number,endSec:number}|"
        "{type:'emotion',text:string,startSec:number,endSec:number}]}]}。"
        "必须遵守以下规则："
        "1. heading 必须严格使用‘编号 时段 内外 地点’，例如‘1-1 夜 外 酒店门口’、‘1-2 日 内 酒店房间’；时段只用日、夜、清晨、黄昏或不明，内外只用内、外或不明。"
        "2. 每个 main 场景必须填写 summary：用一到两句说明本场景的起因、冲突/行动、结果，以及结果如何推动下一场；这是剧情衔接，不要写镜头调度。"
        "3. 只有地点、时间、内外景或叙事目的真正变化才新建场景。同一地点的连续对白和动作必须放在同一场景，按‘剧情段落/事件’组织，不要按剪辑镜头切碎。"
        "4. 连续动作要完整覆盖因果和数量：每一次明确的拳击、推搡、开门、拿取、进出、转身离开都不能漏写；如果是第一拳、第二拳，要按实际顺序明确写出，不能只写‘打了几拳’或只写最后结果，也不能凭空增加动作。每个 action 句都必须写规范人物名、动作和对象，不要让句子以‘狠狠拽住’、‘随后离开’、‘故意看向’这类无主语短语开头；看不清时写‘未知人物’，不要猜。"
        "5. 指尖发白、眼里有血丝、眼神一闪、呼吸变化等只作为动作段中的补充，不得单独成为一个镜头/block；只有它直接改变人物决定或剧情结果时才保留。"
        "6. 严禁写‘特写、近景、正反打、镜头切到、推近、拉远、俯拍、仰拍、画面给到’等拍摄或剪辑指令。后期剪辑处理不进入剧本正文。"
        "7. environment 只写理解剧情必需的地点、人物空间关系和关键道具，最多一到两句；不写‘画面一开始/视频时长/镜头中可以看到’等泛泛开场句，不堆砌灯光、颜色、树叶等无关布景。"
        "8. 人物第一次出现时写能帮助识别和表演的外观或服装；与剧情无关的颜色、饰品和装饰不要堆砌。"
        "9. dialogue 必须一人一句、完整保留原话，不能改写成剧情概括，不能省略、合并或补写听不清的内容。先根据语气和语法恢复准确中文标点：逗号分隔分句，句末使用‘。’、‘？’、‘！’或‘……’，不要输出无标点的长句，也不要连续重复标点。听不清的人名、称谓和代词保留‘[听不清]’，confidence 写 low、uncertain 写 true。"
        "10. 对白默认不填写 emotion，也不要每句对白后重复括号情绪；只有情绪本身是剧情信息且不靠台词已经显而易见时，才把 emotionImportant 写 true。关键转折可单独使用 emotion block。"
        "11. 人物或旁白的画外音、内心声、回忆声、电话另一端声音必须使用 type=vo，并在 speaker 写对应人物名或‘旁白’，不能混成普通 dialogue，也不能漏标。若声音来源无法确认，speaker 写‘未知说话人’并标 uncertain。没有声音依据时不能臆造 VO。"
        "12. 正式扒剧本不输出背景音乐；sound 只保留原片确实听到且对剧情有作用的动作音效或环境声，例如电话铃、关门声、撞击声、车辆鸣笛，不要罗列无关的电流声、风声和布景声。"
        "13. characterProfiles 中为每个人建立唯一 id、规范 name 和 aliases；正文所有 speaker 和 characters 必须使用同一个规范 name，不能混用‘男主’、‘江川’等称呼。"
        "14. segmentType 为 recap、trailer、title_card 或 credits 的内容默认不要写入正文；片头片尾包装、封面、标题卡、上集回顾和高光预告不能当成新剧情。"
        "15. startSec 和 endSec 填写画面或声音的大致秒数并保持顺序；时间只用于回看定位，不要据此拆成镜头。无法判断时才填 0。"
        "没有把握的内容使用‘不明’、‘未知说话人’或‘[听不清]’，不要编造视频之外的事实；优先保证剧情因果、台词准确和人物一致。"
        f"视频标题：{title}；视频时长：{duration_sec:.1f} 秒。"
    )


def ark_response_text(payload: Any) -> str:
    if isinstance(payload, dict) and isinstance(payload.get("output_text"), str):
        return payload["output_text"].strip()
    parts: list[str] = []
    if isinstance(payload, dict):
        output = payload.get("output") or []
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    continue
                content = item.get("content") or []
                if isinstance(content, list):
                    for chunk in content:
                        if isinstance(chunk, dict) and isinstance(chunk.get("text"), str):
                            parts.append(chunk["text"])
                elif isinstance(content, str):
                    parts.append(content)
    if parts:
        return "\n".join(parts).strip()

    # Keep compatibility with minor Responses API response-shape changes.
    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"output_text", "text"} and isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, (dict, list)):
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(payload)
    return "\n".join(parts).strip()


_PUNCT_TRANSLATION = str.maketrans({
    ",": "，",
    ".": "。",
    "!": "！",
    "?": "？",
    ";": "；",
    ":": "：",
    "~": "～",
})
_SENTENCE_END_RE = re.compile(r"[。！？…]$")
_QUESTION_END_RE = re.compile(r"(?:吗|嘛|呢|么|什么|怎么|哪(?:里|个|些)|谁|为何|为什么|是不是|有没有|要不要)$")
_CAMERA_LANGUAGE_RE = re.compile(
    r"(?:镜头(?:切到|切回|给到|对准)?|画面(?:切到|切回|给到)?|特写|近景|中景|全景|远景|正反打|反打|推近|拉远|俯拍|仰拍|摇镜头|跟拍)"
)
_MICRO_DETAIL_RE = re.compile(
    r"(?:指尖|手指|指节|眼里|眼中|眼底|眼眶|血丝|发白|泛白|泛红|通红|睫毛|瞳孔|嘴角|眉头|呼吸|颤抖|发抖|微微|轻轻|细微|眼神一闪)"
)
_MAJOR_ACTION_RE = re.compile(
    r"(?:打|拳|踢|推|拽|抓|抱|吻|亲|拿|递|抢|夺|摔|砸|撞|开门|关门|进门|出门|上车|下车|离开|转身|倒地|站起|冲|追|挡|拦|撕|拔|掏|递给|扔|躲|扑)"
)


def normalize_text(text: Any, *, sentence: bool = False) -> str:
    """Normalize model punctuation without changing the words it recognized."""
    value = unicodedata.normalize("NFKC", str(text or "")).strip()
    if not value:
        return ""
    value = value.translate(_PUNCT_TRANSLATION)
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"\s*([，。！？；：、])\s*", r"\1", value)
    value = re.sub(r"([，。！？；：、])\1+", r"\1", value)
    value = re.sub(r"^[，。；：、]+", "", value)
    value = re.sub(r"[，；：、]+([。！？])", r"\1", value)
    value = re.sub(r"([。！？…])[，；、：]+", r"\1", value)
    if sentence and value and not _SENTENCE_END_RE.search(value):
        value += "？" if _QUESTION_END_RE.search(value) else "。"
    return value


def clean_action_text(text: Any) -> str:
    value = normalize_text(text)
    if not value:
        return ""
    # Camera language belongs to the editing plan, not the screenplay. Remove
    # it defensively because models occasionally echo it despite the prompt.
    value = _CAMERA_LANGUAGE_RE.sub("", value)
    value = re.sub(r"\s*([，。；、])\s*", r"\1", value)
    value = re.sub(r"^[，。；、]+", "", value)
    value = re.sub(r"[，；、]{2,}", "，", value)
    if is_micro_action(value):
        return ""
    # Remove standalone close-up details even when the model embedded them in
    # a longer action paragraph. Keep a clause when it also contains a causal
    # meaningful plot action (for example “擦去嘴角的血迹” or “挥出第一拳”).
    clauses = [part.strip() for part in re.split(r"[，；]", value) if part.strip()]
    if len(clauses) > 1:
        clauses = [part for part in clauses if not is_micro_action(part)]
        value = "，".join(clauses)
    # Action beats are prose too: close an unfinished sentence so exports do
    # not alternate between complete lines and dangling fragments.
    return normalize_text(value.strip(), sentence=True)


def is_micro_action(text: str) -> bool:
    """Return true for a non-causal close-up detail that should not be a beat."""
    major_candidate = text.replace("拳头", "")
    return bool(_MICRO_DETAIL_RE.search(text)) and not _MAJOR_ACTION_RE.search(major_candidate) and len(text) <= 100


def compact_action_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse adjacent action fragments into story beats, not shot fragments."""
    compacted: list[dict[str, Any]] = []
    for block in blocks:
        if block.get("type") != "action":
            compacted.append(block)
            continue
        text = clean_action_text(block.get("text"))
        if not text:
            continue
        block["text"] = text
        if compacted and compacted[-1].get("type") == "action":
            previous = compacted[-1]
            if previous["text"].endswith(("，", "；", "：", "、", "。", "！", "？", "…")):
                joiner = ""
            else:
                joiner = "，"
            previous["text"] = normalize_text(f"{previous['text']}{joiner}{text}")
            if block.get("endSec") is not None:
                previous_end = previous.get("endSec")
                previous["endSec"] = max(previous_end or 0, block["endSec"])
            # Keep the first meaningful timestamp; the merged end locates the
            # whole beat when the user jumps back to the source video.
            continue
        compacted.append(block)

    # A detail may precede the action it belongs to. Attach it to an adjacent
    # action instead of exposing a standalone “fingertips/eyes” block. If no
    # adjacent story action exists, omit the detail: it is an editing cue, not
    # a screenplay beat.
    for index, block in list(enumerate(compacted)):
        if block is None or block.get("type") != "action" or not is_micro_action(block.get("text", "")):
            continue
        if index + 1 < len(compacted) and compacted[index + 1] is not None and compacted[index + 1].get("type") == "action":
            next_block = compacted[index + 1]
            joiner = "" if block["text"].endswith(("，", "；", "：", "、", "。", "！", "？", "…")) else "，"
            next_block["text"] = normalize_text(f"{block['text']}{joiner}{next_block['text']}")
        elif index > 0 and compacted[index - 1] is not None and compacted[index - 1].get("type") == "action":
            previous = compacted[index - 1]
            joiner = "" if previous["text"].endswith(("，", "；", "：", "、", "。", "！", "？", "…")) else "，"
            previous["text"] = normalize_text(f"{previous['text']}{joiner}{block['text']}")
        compacted[index] = None  # type: ignore[assignment]
    return [block for block in compacted if block is not None]


def repair_action_subjects(blocks: list[dict[str, Any]], characters: list[str]) -> None:
    """Add an omitted action subject when the surrounding scene makes it clear.

    Vision models often describe a continuous beat as ``狠狠拽住衣领`` after
    naming the actor in the preceding clause. That is understandable in a
    shot list but reads as a fragment in a screenplay. We only repair clauses
    that begin with an unmistakable action verb and use the last explicit actor
    (or the next speaker when the action directly introduces a line). We never
    invent a new character name.
    """
    known = sorted({str(name).strip() for name in characters if str(name).strip()}, key=len, reverse=True)
    if not known:
        return

    def explicit_subject(clause: str) -> str | None:
        for name in known:
            if clause.startswith(name):
                return name
        return None

    def next_speaker(index: int) -> str | None:
        for following in blocks[index + 1 :]:
            if following.get("type") not in {"dialogue", "vo"}:
                continue
            speaker = str(following.get("speaker") or "").strip()
            if speaker and speaker in known:
                return speaker
            break
        return None

    # These words commonly introduce a subjectless continuation of the same
    # physical action. The regex deliberately excludes emotional-only clauses.
    continuation = re.compile(
        r"^(?:(?:随后|然后|接着|紧接着|同时|并且|故意|狠狠|重重|直接|一把|缓缓|猛地|再次|再度|继续|立刻|马上|抬手|低头|转身)\s*)?"
        r"(?:第[一二三四五六七八九十百0-9]+拳|打|挥|拽|抓|扯|推|踢|抱|拿|递|抢|夺|摔|砸|撞|开门|关门|进门|出门|上车|下车|离开|转身|倒地|站起|冲|追|挡|拦|撕|拔|掏|扔|躲|扑|看向|望向|走向|扶起|搀扶|松开|攥住|捂住|蹲下|站稳)"
    )
    directional_intro = re.compile(r"^(?:故意|随后|然后|接着|紧接着)[^，；。！？…]{0,20}(?:看向|望向|开口)")

    active_subject: str | None = None
    for index, block in enumerate(blocks):
        if block.get("type") != "action":
            continue
        text = str(block.get("text") or "").strip()
        if not text:
            continue
        speaker_hint = next_speaker(index)
        block_subject = explicit_subject(text)
        if block_subject:
            active_subject = block_subject
        clause_subject = block_subject or active_subject
        rebuilt: list[str] = []
        # Keep the punctuation attached to each clause while repairing only
        # the clauses that actually need a grammatical subject.
        clauses = re.findall(r"[^，；。！？…]+[，；。！？…]?", text)
        for raw_clause in clauses:
            clause = raw_clause.strip()
            if not clause:
                continue
            subject = explicit_subject(clause)
            if subject:
                # Do not repeat a character name inside the same beat (e.g.
                # “江川快步冲上前，江川第一拳…”). The first clause already
                # establishes the actor, while the action itself is retained.
                if subject == clause_subject and rebuilt:
                    clause = clause[len(subject) :].lstrip()
                else:
                    clause_subject = subject
            elif continuation.match(clause) or directional_intro.match(clause):
                # A clause that looks toward or introduces the following line
                # usually belongs to that line's speaker; physical follow-up
                # actions stay with the actor named at the start of the beat.
                directional = bool(directional_intro.match(clause) and speaker_hint and re.search(r"(?:看向|望向|开口)", clause))
                # Only the first subjectless clause of a beat needs an explicit
                # name. Later clauses inherit it, which keeps prose natural
                # while still making every action block self-contained.
                subject = speaker_hint if directional else (clause_subject if not rebuilt else None)
                if subject:
                    clause = f"{subject}{clause}"
                    clause_subject = subject
            rebuilt.append(clause)
        if rebuilt:
            block["text"] = normalize_text("".join(rebuilt), sentence=True)
        if block_subject:
            active_subject = block_subject
        elif clause_subject:
            active_subject = clause_subject


def clean_environment(text: Any) -> str:
    value = normalize_text(text)
    if not value:
        return ""
    # Drop generic opening/meta sentences; retain concrete spatial information
    # that follows them in the same model response.
    sentences = re.findall(r"[^。！？…]+[。！？…]?", value)
    kept: list[str] = []
    decorative = re.compile(r"暖光|冷光|照明|灯光|路灯|光斑|背景墙|装饰画|绿植|家常菜|家居氛围|氛围感|对峙气息|气氛|树叶|风声|空气")
    for sentence in sentences:
        sentence = sentence.strip()
        if re.match(r"^(?:视频|画面|镜头)?(?:时长|一开始|开始时|开场|内容是|显示|呈现)", sentence):
            continue
        clauses = [part.strip() for part in re.split(r"[，；]", sentence) if part.strip()]
        clauses = [part for part in clauses if not decorative.search(part)]
        if clauses:
            kept.append("，".join(clauses).rstrip("。！？…") + ("。" if sentence.endswith(("。", "！", "？", "…")) else ""))
    return "".join(kept).strip()


def derive_scene_summary(blocks: list[dict[str, Any]]) -> str:
    """Give legacy scenes a useful continuity line when the model omitted one."""
    events = [
        str(block.get("text") or "").strip()
        for block in blocks
        if block.get("type") in {"action", "dialogue", "vo"} and str(block.get("text") or "").strip()
    ]
    if not events:
        return "本场缺少足够的动作和台词信息，无法确认剧情结果。"

    def excerpt(value: str, limit: int = 46) -> str:
        value = normalize_text(value)
        return value if len(value) <= limit else f"{value[:limit].rstrip('，。！？；：、')}……"

    spoken = [
        str(block.get("text") or "").strip()
        for block in blocks
        if block.get("type") in {"dialogue", "vo"} and str(block.get("text") or "").strip()
    ]
    start, end = (spoken[0], spoken[-1]) if len(spoken) >= 2 else (events[0], events[-1] if len(events) > 1 else events[0])
    if start == end:
        return f"本场围绕“{excerpt(start)}”展开，当前素材在该行动后收束。"
    return f"本场从“{excerpt(start)}”展开，经过对白与行动推进，最终以“{excerpt(end)}”收束。"


def normalize_script(script: Any, title: str) -> dict[str, Any]:
    if not isinstance(script, dict) or not isinstance(script.get("scenes"), list):
        raise ArkError("方舟返回的剧本缺少 scenes 数组")

    alias_map: dict[str, str] = {}

    def names(values: Any) -> list[str]:
        if not isinstance(values, list):
            return []
        result: list[str] = []
        for value in values:
            if isinstance(value, dict):
                value = value.get("name") or value.get("id") or ""
            value = str(value).strip()
            if value:
                result.append(alias_map.get(value.casefold(), value))
        return result

    def number(value: Any) -> float | None:
        if isinstance(value, (int, float)) and value >= 0:
            return round(float(value), 3)
        try:
            parsed = float(str(value).strip())
            return round(parsed, 3) if parsed >= 0 else None
        except (TypeError, ValueError):
            return None

    def truthy(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "是", "推断"}

    def profiles(values: Any) -> list[dict[str, Any]]:
        if isinstance(values, dict):
            values = [{"name": key, "appearance": value} for key, value in values.items()]
        if not isinstance(values, list):
            return []
        result: list[dict[str, Any]] = []
        for index, value in enumerate(values, start=1):
            if not isinstance(value, dict):
                continue
            name = str(value.get("name") or value.get("id") or "").strip()
            if not name:
                continue
            aliases = names(value.get("aliases"))
            row = {
                "id": str(value.get("id") or f"character_{index:03d}"),
                "name": name,
                "aliases": aliases,
                "appearance": str(value.get("appearance") or "").strip(),
                "clothing": str(value.get("clothing") or "").strip(),
            }
            result.append(row)
        return result

    character_profiles = profiles(script.get("characterProfiles"))
    for profile in character_profiles:
        canonical_name = str(profile["name"])
        alias_map[canonical_name.casefold()] = canonical_name
        for alias in profile.get("aliases", []):
            alias_map[str(alias).casefold()] = canonical_name

    def scene_heading(raw_scene: dict[str, Any], index: int, location: str) -> str:
        raw_heading = str(raw_scene.get("heading") or "").strip()
        valid = re.match(r"^\s*\d+-\d+\s+(日|夜|清晨|黄昏|不明)\s+(内|外|不明)\s+.+", raw_heading)
        if valid:
            return raw_heading
        time_of_day = str(raw_scene.get("timeOfDay") or "不明").strip()
        if time_of_day not in {"日", "夜", "清晨", "黄昏", "不明"}:
            time_of_day = "不明"
        interior_exterior = str(raw_scene.get("interiorExterior") or "不明").strip()
        if interior_exterior not in {"内", "外", "不明"}:
            interior_exterior = "不明"
        heading_location = location if location != "待补充" else (raw_heading or "未标注地点")
        return f"1-{index} {time_of_day} {interior_exterior} {heading_location}"

    scenes: list[dict[str, Any]] = []
    for index, raw_scene in enumerate(script["scenes"], start=1):
        if not isinstance(raw_scene, dict):
            continue
        raw_segment_type = str(raw_scene.get("segmentType") or "main").strip().lower()
        raw_heading = str(raw_scene.get("heading") or "").strip()
        if raw_segment_type in {"recap", "trailer", "title_card", "credits"} or any(
            marker in raw_heading for marker in ("上集回顾", "精彩预告", "下集预告", "片尾", "演员表", "片头")
        ):
            continue
        blocks: list[dict[str, Any]] = []
        scene_location = str(raw_scene.get("location") or "待补充").strip()
        raw_blocks: list[Any] = []
        if raw_scene.get("sound"):
            raw_blocks.extend(
                {"type": "sound", "text": item if isinstance(item, str) else item.get("text", "")}
                for item in (raw_scene["sound"] if isinstance(raw_scene["sound"], list) else [raw_scene["sound"]])
            )
        if raw_scene.get("emotion"):
            raw_blocks.append({"type": "emotion", "text": raw_scene["emotion"]})
        raw_blocks.extend(raw_scene.get("blocks") or [])
        for raw_block in raw_blocks:
            if not isinstance(raw_block, dict):
                continue
            if raw_block.get("isNonPlot") or str(raw_block.get("segmentType") or "").lower() in {"recap", "trailer", "title_card", "credits"}:
                continue
            text = str(raw_block.get("text") or raw_block.get("description") or "").strip()
            if not text:
                continue
            raw_type = str(raw_block.get("type") or "action").strip().lower()
            block_type = {
                "narration": "vo",
                "voiceover": "vo",
                "voice_over": "vo",
                "inner_monologue": "vo",
                "os": "vo",  # Backward compatibility with older responses.
                "music": "sound",
                "effect": "sound",
                "ambience": "sound",
                "visual": "action",
            }.get(raw_type, raw_type)
            if block_type not in {"action", "dialogue", "vo", "sound", "emotion"}:
                block_type = "action"
            if block_type == "action":
                text = clean_action_text(text)
            elif block_type in {"dialogue", "vo"}:
                text = normalize_text(text, sentence=True)
            else:
                text = normalize_text(text)
            if not text:
                continue
            block: dict[str, Any] = {"type": block_type, "text": text}
            start = number(raw_block.get("startSec"))
            end = number(raw_block.get("endSec"))
            if start is not None:
                block["startSec"] = start
            if end is not None:
                block["endSec"] = end
            if block_type in {"action", "vo"}:
                block["emotion"] = normalize_text(raw_block.get("emotion"))
            if block_type == "dialogue":
                speaker = str(raw_block.get("speaker") or "未知说话人").strip()
                block["speaker"] = alias_map.get(speaker.casefold(), speaker)
                block["confidence"] = str(raw_block.get("confidence") or "medium").strip().lower()
                block["uncertain"] = truthy(raw_block.get("uncertain")) or block["confidence"] == "low"
                # Dialogue emotion is intentionally omitted from the screenplay
                # body. Repeated parentheticals make every line feel like a
                # shot list; a genuinely plot-changing turn can be represented
                # by its own emotion block instead.
                block["emotion"] = ""
                block["emotionImportant"] = False
            elif block_type == "vo":
                speaker = str(raw_block.get("speaker") or "旁白").strip()
                speaker = alias_map.get(speaker.casefold(), speaker)
                block["speaker"] = speaker
                block["inferred"] = truthy(raw_block.get("inferred"))
                block["confidence"] = str(raw_block.get("confidence") or "medium").strip().lower()
                block["uncertain"] = truthy(raw_block.get("uncertain")) or block["confidence"] == "low"
            elif block_type == "sound":
                block["category"] = str(raw_block.get("category") or "effect").strip()
                block["source"] = str(raw_block.get("source") or "heard").strip()
                # The extraction script should not mix guessed background music
                # into factual scene content. Creative sound suggestions belong
                # in a later adaptation pass, not in the recovered transcript.
                if block["source"] != "heard" or block["category"] == "music":
                    continue
            blocks.append(block)
        block_priority = {"sound": 0, "emotion": 1, "action": 2, "dialogue": 3, "vo": 4}
        blocks.sort(key=lambda item: (
            item.get("startSec") is None,
            item.get("startSec", 0),
            block_priority.get(item.get("type"), 9),
        ))
        blocks = compact_action_blocks(blocks)
        repair_action_subjects(blocks, names(raw_scene.get("characters")) or names(script.get("characters")))
        summary = normalize_text(
            raw_scene.get("summary")
            or raw_scene.get("transition")
            or raw_scene.get("continuity"),
            sentence=True,
        )
        if not summary:
            summary = derive_scene_summary(blocks)
        scene: dict[str, Any] = {
            "id": str(raw_scene.get("id") or f"scene_{index:03d}"),
            "heading": scene_heading(raw_scene, index, scene_location),
            "location": scene_location,
            "timeOfDay": str(raw_scene.get("timeOfDay") or "不明"),
            "interiorExterior": str(raw_scene.get("interiorExterior") or "不明"),
            "characters": names(raw_scene.get("characters")),
            "environment": clean_environment(raw_scene.get("environment")),
            "summary": summary,
            "blocks": blocks,
        }
        scene_start = number(raw_scene.get("startSec"))
        scene_end = number(raw_scene.get("endSec"))
        if scene_start is not None:
            scene["startSec"] = scene_start
        if scene_end is not None:
            scene["endSec"] = scene_end
        scenes.append(scene)
    if not scenes:
        raise ArkError("方舟返回的剧本没有可用场景")
    used_characters: list[str] = []
    for scene in scenes:
        for character in scene.get("characters", []):
            if character not in used_characters:
                used_characters.append(character)
        for block in scene.get("blocks", []):
            speaker = block.get("speaker")
            if block.get("type") in {"dialogue", "vo"} and speaker and speaker not in {"未知说话人", "未知男声", "未知女声", "旁白"} and speaker not in used_characters:
                used_characters.append(speaker)
    used_profiles = [profile for profile in character_profiles if profile["name"] in used_characters]
    return {
        "version": str(script.get("version") or "1.0"),
        "title": str(script.get("title") or title),
        "characters": used_characters or names(script.get("characters")),
        "characterProfiles": used_profiles,
        "scenes": scenes,
    }


def ark_usage(payload: Any) -> dict[str, int | None]:
    usage = payload.get("usage") if isinstance(payload, dict) else {}
    usage = usage if isinstance(usage, dict) else {}

    def number(*keys: str) -> int | None:
        for key in keys:
            value = usage.get(key)
            if isinstance(value, (int, float)):
                return int(value)
        return None

    input_tokens = number("input_tokens", "prompt_tokens")
    output_tokens = number("output_tokens", "completion_tokens")
    total_tokens = number("total_tokens")
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    return {"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": total_tokens}


def ark_recognize(path: Path, title: str, duration_sec: float) -> tuple[dict[str, Any], dict[str, int | None]]:
    started = time.perf_counter()
    logger.info("provider_start provider=ark operation=video_recognize file=%s duration_sec=%.1f", path.name, duration_sec)
    upload_started = time.perf_counter()
    file_id = ark_upload_video(path)
    logger.info("provider_step provider=ark operation=upload file=%s duration_ms=%.1f", path.name, (time.perf_counter() - upload_started) * 1000)
    wait_started = time.perf_counter()
    ark_wait_for_file(file_id)
    logger.info("provider_step provider=ark operation=file_ready file=%s duration_ms=%.1f", path.name, (time.perf_counter() - wait_started) * 1000)
    response_started = time.perf_counter()
    payload = ark_http(
        "POST",
        "/responses",
        json.dumps({
            "model": ARK_MODEL,
            "input": [{
                "role": "user",
                "content": [
                    {"type": "input_video", "file_id": file_id},
                    {"type": "input_text", "text": ark_prompt(title, duration_sec)},
                ],
            }],
        }, ensure_ascii=False).encode("utf-8"),
    )
    logger.info("provider_step provider=ark operation=response file=%s duration_ms=%.1f", path.name, (time.perf_counter() - response_started) * 1000)
    text = ark_response_text(payload)
    if not text:
        raise ArkError("方舟没有返回剧本文本")
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    if not cleaned.startswith("{"):
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            cleaned = cleaned[start:end + 1]
    try:
        raw_script = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ArkError("方舟返回的剧本不是合法 JSON") from exc
    script = normalize_script(raw_script, title)
    logger.info(
        "provider_done provider=ark operation=video_recognize file=%s duration_ms=%.1f scenes=%s",
        path.name,
        (time.perf_counter() - started) * 1000,
        len(script.get("scenes") or []),
    )
    return script, ark_usage(payload)


def openai_transcribe(path: Path) -> str:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not OPENAI_API_KEY:
        return ""
    audio_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", dir=DATA_DIR, delete=False) as handle:
            audio_path = Path(handle.name)
        subprocess.run(
            [ffmpeg, "-y", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k", str(audio_path)],
            capture_output=True,
            timeout=180,
            check=True,
        )
        body, boundary = multipart_body(
            {"model": OPENAI_TRANSCRIPTION_MODEL, "language": "zh"},
            "file",
            "audio.mp3",
            audio_path.read_bytes(),
            "audio/mpeg",
        )
        request = UrlRequest(
            f"{OPENAI_BASE_URL}/audio/transcriptions",
            data=body,
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        with urlopen(request, timeout=180) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return str(payload.get("text") or "").strip()
    finally:
        if audio_path:
            audio_path.unlink(missing_ok=True)


def openai_script(title: str, duration_sec: float, transcript: str) -> dict[str, Any] | None:
    if not OPENAI_API_KEY or not transcript:
        return None
    prompt = (
        "请把下面这段短视频转写整理为可编辑的中文短剧场景剧本。只返回 JSON，不要 Markdown。"
        "不要做逐镜头分镜，不要写特写、正反打、推拉摇移或剪辑指令；同一场景连续动作合并成剧情段落。"
        "JSON 必须包含 version、title、characters、characterProfiles、scenes；scenes 内包含 id、heading、location、timeOfDay、interiorExterior、segmentType、summary、characters、environment、blocks。"
        "summary 必须说明本场景的起因、行动、结果和下一步动机，作为剧情衔接。environment 只保留必要空间关系和道具，不要写‘画面一开始’或视频时长。"
        "blocks 可使用 action、dialogue、vo、sound、emotion；动作要完整写出每一次明确的拳击、推搡、进出和取放，不能漏掉第一步，也不要把指尖发白、血丝等微表情单独拆成块。每个 action 必须明确人物名和动作对象，不能省略动作主语。"
        "dialogue 必须完整保留转写原话，一人一句，不省略不改写，并恢复准确中文标点；对白默认不写 emotion。人物/旁白画外音、内心声、电话声必须使用 vo，speaker 写对应人物或旁白，不能漏标。"
        "sound 只描述原片中确实听到且有用的动作音效或环境声，不输出背景音乐。"
        f"视频标题：{title}\n视频时长：{duration_sec:.1f} 秒\n转写：{transcript}"
    )
    payload = json.dumps({
        "model": OPENAI_TEXT_MODEL,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": "你是严谨的中文短视频编剧与台词整理助手。"},
            {"role": "user", "content": prompt},
        ],
    }).encode("utf-8")
    request = UrlRequest(
        f"{OPENAI_BASE_URL}/chat/completions",
        data=payload,
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=180) as response:
        result = json.loads(response.read().decode("utf-8"))
    content = result["choices"][0]["message"]["content"]
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.IGNORECASE)
    script = json.loads(content)
    if not isinstance(script, dict) or not isinstance(script.get("scenes"), list):
        return None
    script.setdefault("version", "1.0")
    script.setdefault("title", title)
    return script


def script_to_markdown(task: dict[str, Any]) -> str:
    script = task.get("result") or {}
    lines = [f"# {script.get('title') or task['title']}", ""]
    profiles = script.get("characterProfiles") or []
    if profiles:
        lines.extend(["## 人物表", ""])
        for profile in profiles:
            name = profile.get("name") or "人物"
            appearance = profile.get("appearance") or ""
            clothing = profile.get("clothing") or ""
            details = "；".join(item for item in (appearance, clothing) if item)
            lines.append(f"- {name}：{details}" if details else f"- {name}")
        lines.append("")
    for scene in script.get("scenes", []):
        lines.extend([f"## {scene.get('heading', '未标注场景')}", ""])
        if scene.get("location"):
            lines.extend([f"地点：{scene['location']}", ""])
        if scene.get("summary"):
            lines.extend([f"【剧情衔接】{scene['summary']}", ""])
        characters = scene.get("characters") or []
        if characters:
            lines.extend([f"出场人物：{'、'.join(characters)}", ""])
        if scene.get("environment"):
            lines.extend([f"【环境】{scene['environment']}", ""])
        for block in scene.get("blocks", []):
            block_type = block.get("type")
            if block_type == "dialogue":
                warning = "【需核对】" if block.get("uncertain") else ""
                lines.append(f"{block.get('speaker', '人物')}{warning}：{block.get('text', '')}")
            elif block_type in {"vo", "os"}:
                inferred = "（推断）" if block.get("inferred") else ""
                speaker = block.get("speaker") or "旁白"
                label = "VO" if speaker in {"旁白", "未知说话人", "OS"} else f"{speaker} VO"
                lines.append(f"{label}{inferred}：{block.get('text', '')}")
            elif block_type == "sound":
                category = block.get("category") or "音效"
                source = "推断" if block.get("source") == "inferred" else "听到"
                lines.append(f"【{category} / {source}】{block.get('text', '')}")
            elif block_type == "emotion":
                lines.append(f"【情绪】{block.get('text', '')}")
            else:
                lines.append(f"▲ {block.get('text', '')}")
        lines.append("")
    return "\n".join(lines)


def script_quality(script: dict[str, Any], provider: str) -> dict[str, Any]:
    """Expose useful, deterministic QA signals instead of placeholder zeros."""
    scenes = script.get("scenes") or []
    dialogue = [
        block
        for scene in scenes
        for block in scene.get("blocks", [])
        if block.get("type") in {"dialogue", "vo"}
    ]
    punctuation_ok = [bool(_SENTENCE_END_RE.search(str(block.get("text") or ""))) for block in dialogue]
    uncertain = sum(1 for block in dialogue if block.get("uncertain"))
    missing_summary = sum(1 for scene in scenes if not str(scene.get("summary") or "").strip())
    coverage = round(100 * sum(punctuation_ok) / len(punctuation_ok)) if punctuation_ok else 0
    confidence = round(100 * (1 - uncertain / len(dialogue))) if dialogue else 0
    warnings = sum(1 for ok in punctuation_ok if not ok) + uncertain + missing_summary
    return {
        "dialogueCoverage": coverage,
        "speakerConfidence": max(0, confidence),
        "warnings": warnings,
        "provider": provider,
    }


async def run_recognizer(row: sqlite3.Row) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Run the configured provider, degrading gracefully when a provider is unavailable.

    Ark is preferred for video understanding. A provider quota/rate-limit error
    must not strand a test task in ``failed``: use OpenAI when configured and
    finally the deterministic local result so the upload/export loop remains
    testable while the provider account is repaired.
    """
    path = Path(row["stored_path"])
    duration = float(row["duration_sec"] or 0)
    probed = await asyncio.to_thread(probe_duration, path)
    if probed:
        duration = probed
        update_task(row["id"], duration_sec=duration)
    await asyncio.sleep(0.25)
    ark_error: ArkError | None = None
    if ARK_API_KEY:
        try:
            script, usage = await asyncio.to_thread(ark_recognize, path, row["title"], duration)
            quality = script_quality(script, "ark")
            return script, quality, usage
        except ArkError as exc:
            ark_error = exc
            logger.warning(
                "provider_failed provider=ark task_id=%s status_code=%s error=%s",
                row["id"],
                exc.status_code,
                safe_error_text(exc),
            )
            if not ARK_FALLBACK_ON_ERROR:
                raise

    transcript = ""
    script = None
    openai_error: Exception | None = None
    if OPENAI_API_KEY:
        try:
            transcript = await asyncio.to_thread(openai_transcribe, path)
            script = await asyncio.to_thread(openai_script, row["title"], duration, transcript)
        except Exception as exc:
            openai_error = exc
            logger.warning("provider_failed provider=openai task_id=%s error=%s", row["id"], safe_error_text(exc))
            # A provider outage should not break the local task/export loop.
            transcript = ""
            script = None
    script = script or sample_script(row["title"], duration, transcript)
    provider = "openai" if script and OPENAI_API_KEY and transcript else "local-fallback"
    script = normalize_script(script, row["title"])
    quality = script_quality(script, provider)
    if ark_error:
        quality["warning"] = ark_fallback_message(ark_error)
        if openai_error and provider == "local-fallback":
            quality["warning"] += " OpenAI 备用服务也不可用，已使用本地兜底结果。"
    usage = {"input_tokens": None, "output_tokens": None, "total_tokens": None}
    return script, quality, {
        **usage,
        "provider": provider,
        "model": OPENAI_TEXT_MODEL if provider == "openai" else None,
    }


async def process_task(task_id: str) -> None:
    row = task_row(task_id)
    if not row:
        return
    started = time.perf_counter()
    logger.info("task_start task_id=%s title=%s file=%s", task_id, row["title"], row["file_name"])
    try:
        mark_task_stage(task_id, status="running", stage="probing", progress_percent=12, message="正在读取视频信息")
        await asyncio.sleep(0.15)
        mark_task_stage(task_id, status="running", stage="transcribing", progress_percent=34, message="正在整理语音和对白")
        await asyncio.sleep(0.15)
        mark_task_stage(task_id, status="running", stage="vision", progress_percent=62, message="正在识别画面与动作")
        script, quality, usage = await run_recognizer(task_row(task_id))
        await asyncio.sleep(0.15)
        mark_task_stage(task_id, status="running", stage="merging", progress_percent=82, message="正在合并场景和人物")
        await asyncio.sleep(0.15)
        mark_task_stage(task_id, status="running", stage="exporting", progress_percent=94, message="正在生成可下载剧本")
        await asyncio.sleep(0.15)
        elapsed_ms = (time.perf_counter() - started) * 1000
        update_task(
            task_id,
            status="done",
            stage="done",
            progress_percent=100,
            result_json=json.dumps(script, ensure_ascii=False),
            quality_json=json.dumps(quality, ensure_ascii=False),
            provider=usage.get("provider", "ark" if ARK_API_KEY else "local-fallback"),
            model=usage.get("model", ARK_MODEL if ARK_API_KEY else None),
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            total_tokens=usage.get("total_tokens"),
            credits_used=0,
            completed_at=now_iso(),
        )
        record_task_event(
            task_id,
            "completed",
            "识别完成",
            status="done",
            stage="done",
            progress_percent=100,
            duration_ms=elapsed_ms,
        )
        logger.info(
            "task_done task_id=%s provider=%s duration_ms=%.1f scenes=%s",
            task_id,
            usage.get("provider", "unknown"),
            elapsed_ms,
            len(script.get("scenes") or []),
        )
    except Exception as exc:  # pragma: no cover - defensive boundary for background work
        message = f"处理失败：{safe_error_text(exc)}"
        update_task(
            task_id,
            status="failed",
            stage="failed",
            progress_percent=100,
            error=message,
        )
        record_task_event(
            task_id,
            "failed",
            message,
            status="failed",
            stage="failed",
            progress_percent=100,
            duration_ms=(time.perf_counter() - started) * 1000,
        )
        logger.error("task_failed task_id=%s duration_ms=%.1f error=%s", task_id, (time.perf_counter() - started) * 1000, message)


@app.get("/api/health")
def health() -> Any:
    try:
        with db() as connection:
            connection.execute("SELECT 1").fetchone()
        counts = task_counts()
        return {
            "status": "ok",
            "environment": JBB_ENVIRONMENT,
            "uptimeSec": round(time.monotonic() - APP_STARTED_MONOTONIC, 1),
            "activeTasks": counts["queued"] + counts["running"],
            "timestamp": now_iso(),
        }
    except sqlite3.Error as exc:
        logger.exception("health_degraded error=%s", safe_error_text(exc))
        return JSONResponse(status_code=503, content={"status": "degraded", "environment": JBB_ENVIRONMENT})


@app.get("/api/metrics")
def metrics() -> dict[str, Any]:
    counts = task_counts()
    log_size = (LOG_DIR / "app.log").stat().st_size if (LOG_DIR / "app.log").exists() else 0
    return {
        "status": "ok",
        "environment": JBB_ENVIRONMENT,
        "uptimeSec": round(time.monotonic() - APP_STARTED_MONOTONIC, 1),
        "tasks": counts,
        "provider": {
            "arkConfigured": bool(ARK_API_KEY),
            "openaiConfigured": bool(OPENAI_API_KEY),
            "arkModel": ARK_MODEL if ARK_API_KEY else None,
        },
        "logging": {
            "level": JBB_LOG_LEVEL,
            "fileBytes": log_size,
        },
        "timestamp": now_iso(),
    }


@app.get("/api/me")
def profile() -> dict[str, Any]:
    return {"id": "local-user", "name": "本地用户", "credits": 9999, "plan": "本地测试"}


@app.get("/api/tasks")
def list_tasks(keyword: str = "", status: str = "all") -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if keyword.strip():
        clauses.append("(title LIKE ? OR file_name LIKE ?)")
        value = f"%{keyword.strip()}%"
        params.extend([value, value])
    if status and status != "all":
        clauses.append("status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with db() as connection:
        rows = connection.execute(f"SELECT * FROM tasks {where} ORDER BY created_at DESC", params).fetchall()
    return [row_to_task(row) for row in rows]


@app.get("/api/tasks/{task_id}")
def task_detail(task_id: str) -> dict[str, Any]:
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在或已被删除")
    task["events"] = task_events(task_id)
    return task


@app.get("/api/tasks/{task_id}/events")
def task_event_list(task_id: str, limit: int = Query(100, ge=1, le=200)) -> list[dict[str, Any]]:
    if not task_row(task_id):
        raise HTTPException(status_code=404, detail="任务不存在或已被删除")
    return task_events(task_id, limit)


@app.post("/api/tasks")
async def create_task(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    title: str = Form(""),
    durationSec: float = Form(0),
) -> dict[str, Any]:
    file_name = safe_filename(file.filename or "video.mp4")
    if not file_name.lower().endswith(".mp4"):
        raise HTTPException(status_code=400, detail="目前只支持 MP4 视频")
    task_id = f"task-{uuid.uuid4().hex}"
    target = UPLOAD_DIR / f"{task_id}.mp4"
    size = 0
    try:
        with target.open("wb") as output:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="文件超过 500 MB")
                output.write(chunk)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    duration = probe_duration(target) or max(0, float(durationSec or 0))
    if duration > MAX_DURATION_SECONDS:
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"视频超过 {MAX_DURATION_MINUTES} 分钟")
    created = now_iso()
    task_title = title.strip() or title_from_filename(file_name)
    with db() as connection:
        connection.execute(
            """
            INSERT INTO tasks
              (id, title, file_name, stored_path, file_size, mime_type, duration_sec,
               estimated_minutes, credits_used, status, stage, progress_percent,
               created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 'queued', 'queued', 4, ?, ?)
            """,
            (
                task_id,
                task_title,
                file_name,
                str(target),
                size,
                file.content_type or mimetypes.guess_type(file_name)[0] or "video/mp4",
                duration,
                max(1, int((duration + 59) // 60)) if duration else 1,
                created,
                created,
            ),
        )
    record_task_event(task_id, "created", "任务已创建", status="queued", stage="queued", progress_percent=4)
    logger.info(
        "task_created task_id=%s title=%s file=%s size_bytes=%s request_id=%s",
        task_id,
        task_title,
        file_name,
        size,
        REQUEST_ID.get(),
    )
    background.add_task(process_task, task_id)
    return get_task(task_id)  # type: ignore[return-value]


@app.post("/api/tasks/{task_id}/retry")
async def retry_task(task_id: str, background: BackgroundTasks) -> dict[str, Any]:
    row = task_row(task_id)
    if not row:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not Path(row["stored_path"]).exists():
        raise HTTPException(status_code=409, detail="原始视频已不存在，无法重试")
    update_task(task_id, status="queued", stage="queued", progress_percent=4, error=None, completed_at=None)
    record_task_event(task_id, "retry", "任务已重新排队", status="queued", stage="queued", progress_percent=4)
    logger.info("task_retry task_id=%s request_id=%s", task_id, REQUEST_ID.get())
    background.add_task(process_task, task_id)
    return get_task(task_id)  # type: ignore[return-value]


@app.delete("/api/tasks/{task_id}", status_code=204)
def delete_task(task_id: str) -> Response:
    row = task_row(task_id)
    if not row:
        return Response(status_code=204)
    Path(row["stored_path"]).unlink(missing_ok=True)
    with db() as connection:
        connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        connection.execute("DELETE FROM task_events WHERE task_id = ?", (task_id,))
    logger.info("task_deleted task_id=%s request_id=%s", task_id, REQUEST_ID.get())
    return Response(status_code=204)


@app.get("/api/tasks/{task_id}/download")
def download_task(task_id: str, fmt: str = Query("md", pattern="^(md|txt)$")) -> Response:
    task = get_task(task_id)
    if not task or task["status"] != "done" or not task.get("result"):
        raise HTTPException(status_code=409, detail="剧本还不能下载")
    markdown = script_to_markdown(task)
    content = markdown if fmt == "md" else re.sub(r"^#{1,6}\s+", "", markdown, flags=re.MULTILINE)
    filename = f"{safe_filename(task['title'])}.{fmt}"
    encoded_filename = quote(filename)
    return Response(
        content=content,
        media_type="text/markdown" if fmt == "md" else "text/plain",
        headers={
            "X-Filename": encoded_filename,
            "Content-Disposition": f"attachment; filename=script.{fmt}; filename*=UTF-8''{encoded_filename}",
        },
    )


if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
