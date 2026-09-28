"""FastAPI route handlers grouped by authentication, health, and tasks."""

from __future__ import annotations

import asyncio
import mimetypes
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import BackgroundTasks, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from .auth_store import (
    _auth_code_retry_after,
    _invalidate_auth_code,
    _issue_auth_code,
    _password_hash,
    _password_matches,
    _set_session,
    _token_hash,
    _user_payload,
    _verify_auth_code,
    current_user,
    db,
    now_iso,
)
from .config import (
    APP_STARTED_MONOTONIC,
    ARK_API_KEY,
    ARK_MODEL,
    AUTH_ALLOW_DEV_CODE,
    AUTH_CODE_RESEND_SECONDS,
    AUTH_CODE_TTL_SECONDS,
    JBB_ENVIRONMENT,
    JBB_LOG_LEVEL,
    LOG_DIR,
    MAX_DURATION_MINUTES,
    MAX_DURATION_SECONDS,
    MAX_UPLOAD_BYTES,
    OPENAI_API_KEY,
    PASSWORD_DIGIT_RE,
    PASSWORD_LETTER_RE,
    PASSWORD_MIN_LENGTH,
    REQUEST_ID,
    SESSION_COOKIE,
    TENCENTCLOUD_SECRET_ID,
    TENCENTCLOUD_SECRET_KEY,
    TENCENTCLOUD_SES_FROM_EMAIL,
    TENCENTCLOUD_SES_TEMPLATE_ID,
    UPLOAD_DIR,
)
from .logging_setup import _email_log_id, logger, safe_error_text
from .media import probe_duration, safe_filename, title_from_filename
from .processing import process_task
from .script import script_to_markdown
from .task_store import (
    get_task,
    record_task_event,
    row_to_task,
    task_counts,
    task_events,
    task_row,
    update_task,
)

# Route handlers stay thin: validation and HTTP translation live here, while
# persistence, providers, and script processing remain in their own modules.

# Health and operational visibility
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
            "tencentSesConfigured": bool(TENCENTCLOUD_SECRET_ID and TENCENTCLOUD_SECRET_KEY and TENCENTCLOUD_SES_FROM_EMAIL and TENCENTCLOUD_SES_TEMPLATE_ID),
            "tencentSesSenderConfigured": bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", TENCENTCLOUD_SES_FROM_EMAIL)),
            "tencentSesTemplateConfigured": bool(TENCENTCLOUD_SES_TEMPLATE_ID),
            "tencentSesTemplateId": TENCENTCLOUD_SES_TEMPLATE_ID or None,
            "authDevCodeEnabled": AUTH_ALLOW_DEV_CODE,
            "authCodeResendSeconds": AUTH_CODE_RESEND_SECONDS,
        },
        "logging": {
            "level": JBB_LOG_LEVEL,
            "fileBytes": log_size,
        },
        "timestamp": now_iso(),
    }


# Authentication and account lifecycle
async def request_auth_code(request: Request) -> dict[str, Any]:
    payload = await request.json()
    email = str(payload.get("email") or "").strip().lower()
    purpose = str(payload.get("purpose") or "register").strip().lower()
    if purpose not in {"register", "reset"}:
        raise HTTPException(status_code=400, detail="验证码用途不正确")
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise HTTPException(status_code=400, detail="请输入正确的邮箱地址")
    with db() as connection:
        exists = connection.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone()
    if purpose == "register" and exists:
        raise HTTPException(status_code=409, detail="该邮箱已注册，请直接登录")
    if purpose == "reset" and not exists:
        raise HTTPException(status_code=404, detail="该邮箱尚未注册")
    retry_after = _auth_code_retry_after(email, purpose)
    if retry_after:
        raise HTTPException(
            status_code=429,
            detail=f"验证码已发送，请 {retry_after} 秒后再试",
            headers={"Retry-After": str(retry_after)},
        )
    code, delivered = await asyncio.to_thread(_issue_auth_code, email, purpose)
    result: dict[str, Any] = {
        "message": "验证码已发送，请查收邮箱",
        "expiresIn": AUTH_CODE_TTL_SECONDS,
        "resendAfter": AUTH_CODE_RESEND_SECONDS,
        "delivery": "tencent_ses" if delivered else "development_fallback",
    }
    # With no Tencent Cloud credentials, expose a one-time code for local smoke tests.
    # Production deployments should always configure Tencent Cloud SES and sender.
    if not delivered and not AUTH_ALLOW_DEV_CODE:
        await asyncio.to_thread(_invalidate_auth_code, email, purpose)
        raise HTTPException(status_code=503, detail="邮件服务暂未配置，请联系管理员")
    if not delivered:
        result["message"] = "腾讯云邮件服务尚未配置，已生成开发验证码"
        result["devCode"] = code
        logger.warning("tencentcloud_ses_dev_fallback email_id=%s purpose=%s", _email_log_id(email), purpose)
    return result


async def register(request: Request) -> JSONResponse:
    payload = await request.json()
    email = str(payload.get("email") or "").strip().lower()
    password = str(payload.get("password") or "")
    name = str(payload.get("name") or "").strip()[:40]
    code = str(payload.get("code") or "").strip()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise HTTPException(status_code=400, detail="请输入正确的邮箱地址")
    if (
        len(password) < PASSWORD_MIN_LENGTH
        or not PASSWORD_LETTER_RE.search(password)
        or not PASSWORD_DIGIT_RE.search(password)
    ):
        raise HTTPException(status_code=400, detail="密码至少 8 位，且必须同时包含字母和数字")
    if not name:
        name = email.split("@", 1)[0][:40] or "剧编编用户"
    if not re.fullmatch(r"\d{6}", code) or not _verify_auth_code(email, "register", code):
        raise HTTPException(status_code=400, detail="验证码错误或已过期")
    user_id = f"user-{uuid.uuid4().hex}"
    created = now_iso()
    try:
        with db() as connection:
            connection.execute(
                "INSERT INTO users(id, email, password_hash, name, credits, plan, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                # Testing mode: keep newly registered accounts unblocked while
                # screenplay quality is being evaluated in both environments.
                (user_id, email, _password_hash(password), name, 9999, "体验版", created),
            )
            user = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="该邮箱已注册，请直接登录")
    response = JSONResponse(_user_payload(user))
    _set_session(response, user_id)
    return response


async def login(request: Request) -> JSONResponse:
    payload = await request.json()
    email = str(payload.get("email") or "").strip().lower()
    password = str(payload.get("password") or "")
    with db() as connection:
        user = connection.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if not user or not _password_matches(password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="邮箱或密码不正确")
    with db() as connection:
        connection.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (now_iso(), user["id"]))
        user = connection.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
    response = JSONResponse(_user_payload(user))
    _set_session(response, user["id"])
    return response


async def reset_password(request: Request) -> JSONResponse:
    payload = await request.json()
    email = str(payload.get("email") or "").strip().lower()
    password = str(payload.get("password") or "")
    code = str(payload.get("code") or "").strip()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise HTTPException(status_code=400, detail="请输入正确的邮箱地址")
    if (
        len(password) < PASSWORD_MIN_LENGTH
        or not PASSWORD_LETTER_RE.search(password)
        or not PASSWORD_DIGIT_RE.search(password)
    ):
        raise HTTPException(status_code=400, detail="密码至少 8 位，且必须同时包含字母和数字")
    if not re.fullmatch(r"\d{6}", code) or not _verify_auth_code(email, "reset", code):
        raise HTTPException(status_code=400, detail="验证码错误或已过期")
    with db() as connection:
        user = connection.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if not user:
            raise HTTPException(status_code=404, detail="该邮箱尚未注册")
        connection.execute("UPDATE users SET password_hash = ? WHERE id = ?", (_password_hash(password), user["id"]))
        connection.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
    return {"message": "密码已重置，请使用新密码登录"}


def logout(request: Request) -> Response:
    token = request.cookies.get(SESSION_COOKIE, "").strip()
    if token:
        with db() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE)
    return response


def profile(request: Request) -> dict[str, Any]:
    return _user_payload(current_user(request))


# Task listing, detail, upload, and lifecycle actions
def list_tasks(request: Request, keyword: str = "", status: str = "all") -> list[dict[str, Any]]:
    user = current_user(request)
    clauses: list[str] = []
    params: list[Any] = [user["id"]]
    clauses.append("user_id = ?")
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


def task_detail(request: Request, task_id: str) -> dict[str, Any]:
    task = get_task(task_id, current_user(request)["id"])
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在或已被删除")
    task["events"] = task_events(task_id)
    return task


def task_event_list(request: Request, task_id: str, limit: int = Query(100, ge=1, le=200)) -> list[dict[str, Any]]:
    if not task_row(task_id, current_user(request)["id"]):
        raise HTTPException(status_code=404, detail="任务不存在或已被删除")
    return task_events(task_id, limit)


async def create_task(
    request: Request,
    background: BackgroundTasks,
    file: UploadFile = File(...),
    title: str = Form(""),
    durationSec: float = Form(0),
) -> dict[str, Any]:
    user = current_user(request)
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
    estimated = max(1, int((duration + 59) // 60)) if duration else 1
    with db() as connection:
        connection.execute(
            """
            INSERT INTO tasks
              (id, user_id, title, file_name, stored_path, file_size, mime_type, duration_sec,
               estimated_minutes, credits_used, status, stage, progress_percent,
               created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 'queued', 4, ?, ?)
            """,
            (
                task_id,
                user["id"],
                task_title,
                file_name,
                str(target),
                size,
                file.content_type or mimetypes.guess_type(file_name)[0] or "video/mp4",
                duration,
                estimated,
                estimated,
                created,
                created,
            ),
        )
        if estimated > int(user["credits"]):
            target.unlink(missing_ok=True)
            raise HTTPException(status_code=402, detail=f"额度不足，当前剩余 {user['credits']} 分钟")
        connection.execute("UPDATE users SET credits = credits - ? WHERE id = ?", (estimated, user["id"]))
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
    return get_task(task_id, user["id"])  # type: ignore[return-value]


async def retry_task(request: Request, task_id: str, background: BackgroundTasks) -> dict[str, Any]:
    user_id = current_user(request)["id"]
    row = task_row(task_id, user_id)
    if not row:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not Path(row["stored_path"]).exists():
        raise HTTPException(status_code=409, detail="原始视频已不存在，无法重试")
    update_task(task_id, status="queued", stage="queued", progress_percent=4, error=None, completed_at=None)
    record_task_event(task_id, "retry", "任务已重新排队", status="queued", stage="queued", progress_percent=4)
    logger.info("task_retry task_id=%s request_id=%s", task_id, REQUEST_ID.get())
    background.add_task(process_task, task_id)
    return get_task(task_id, user_id)  # type: ignore[return-value]


def delete_task(request: Request, task_id: str) -> Response:
    row = task_row(task_id, current_user(request)["id"])
    if not row:
        return Response(status_code=204)
    Path(row["stored_path"]).unlink(missing_ok=True)
    with db() as connection:
        connection.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        connection.execute("DELETE FROM task_events WHERE task_id = ?", (task_id,))
    logger.info("task_deleted task_id=%s request_id=%s", task_id, REQUEST_ID.get())
    return Response(status_code=204)


def download_task(request: Request, task_id: str, fmt: str = Query("md", pattern="^(md|txt)$")) -> Response:
    task = get_task(task_id, current_user(request)["id"])
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
