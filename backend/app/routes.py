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
    BUILD_VERSION,
    AUTH_ALLOW_DEV_CODE,
    AUTH_CODE_RESEND_SECONDS,
    AUTH_CODE_TTL_SECONDS,
    JBB_ENVIRONMENT,
    JBB_LOG_LEVEL,
    LOG_DIR,
    MAX_DURATION_MINUTES,
    MAX_DURATION_SECONDS,
    MAX_UPLOAD_BYTES,
    POINTS_PER_MINUTE,
    QUEUE_MAX_WAIT_MINUTES,
    REGISTER_POINTS,
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
from .billing import fetch_monthly_ark_cost
from .logging_setup import _email_log_id, logger, safe_error_text
from .media import (
    episode_sort_key,
    probe_duration,
    safe_filename,
    title_from_filename,
    title_with_episode,
)
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
            "build": BUILD_VERSION,
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


def _queue_summary(connection: sqlite3.Connection, additional_minutes: int = 0) -> dict[str, Any]:
    """Estimate backlog time using the same worker concurrency as the runner."""
    from .config import WORKER_CONCURRENCY

    rows = connection.execute(
        "SELECT status, estimated_minutes, progress_percent FROM tasks WHERE status IN ('queued', 'running')"
    ).fetchall()
    workload = 0.0
    queued_count = 0
    running_count = 0
    for row in rows:
        minutes = max(1.0, float(row["estimated_minutes"] or 1))
        if row["status"] == "running":
            running_count += 1
            workload += minutes * max(0.05, 1 - float(row["progress_percent"] or 0) / 100)
        else:
            queued_count += 1
            workload += minutes
    workload += max(0, int(additional_minutes or 0))
    wait_minutes = int((workload + WORKER_CONCURRENCY - 1) // WORKER_CONCURRENCY)
    return {
        "waitMinutes": wait_minutes,
        "waitHours": round(wait_minutes / 60, 1),
        "blocked": wait_minutes > QUEUE_MAX_WAIT_MINUTES,
        "thresholdMinutes": QUEUE_MAX_WAIT_MINUTES,
        "workerConcurrency": WORKER_CONCURRENCY,
        "queuedCount": queued_count,
        "runningCount": running_count,
    }


def queue_summary(request: Request) -> dict[str, Any]:
    current_user(request)
    with db() as connection:
        return _queue_summary(connection)


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
                (user_id, email, _password_hash(password), name, REGISTER_POINTS, "体验版", created),
            )
            connection.execute(
                "INSERT INTO credit_ledger(user_id, amount, balance_after, entry_type, reason, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, REGISTER_POINTS, REGISTER_POINTS, "grant", "新用户注册赠送 25 积分", created),
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
    if "is_active" in user.keys() and not user["is_active"]:
        raise HTTPException(status_code=403, detail="account disabled")
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


def user_credit_ledger(request: Request, limit: int = Query(100, ge=1, le=200)) -> dict[str, Any]:
    user = current_user(request)
    with db() as connection:
        rows = connection.execute(
            "SELECT id, amount, balance_after, entry_type, reason, created_at FROM credit_ledger WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user["id"], limit),
        ).fetchall()
    return {"balance": int(user["credits"]), "items": [dict(row) for row in rows]}


# Administrator console APIs.  The console deliberately exposes operational
# controls only; invitation/code management and product analytics are kept for
# a later phase.
def _require_admin(request: Request) -> sqlite3.Row:
    user = current_user(request)
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="无管理员权限")
    return user


def _admin_audit(admin_id: str, action: str, target_type: str = "", target_id: str = "", detail: str = "") -> None:
    with db() as connection:
        connection.execute(
            "INSERT INTO admin_audit_logs(admin_user_id, action, target_type, target_id, detail, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (admin_id, action, target_type, target_id, detail[:1000], now_iso()),
        )


def admin_overview(request: Request) -> dict[str, Any]:
    _require_admin(request)
    with db() as connection:
        users = connection.execute("SELECT COUNT(*) AS total, SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END) AS active, COALESCE(SUM(credits), 0) AS credits FROM users").fetchone()
        task_status = connection.execute("SELECT status, COUNT(*) AS count FROM tasks GROUP BY status").fetchall()
        # API cost belongs to the provider call, not to the user-facing point
        # charge. Include review completions as well as successful tasks.
        usage = connection.execute("SELECT COALESCE(SUM(credits_used), 0) AS used, COALESCE(SUM(api_cost_rmb), 0) AS api_cost FROM tasks WHERE status IN ('done', 'review')").fetchone()
        recent = connection.execute("SELECT t.id, t.user_id, t.title, t.status, t.stage, t.credits_used, t.created_at, COALESCE(u.email, t.user_id, '-') AS email, u.name FROM tasks t LEFT JOIN users u ON u.id = t.user_id ORDER BY t.created_at DESC LIMIT 10").fetchall()
    statuses = {row["status"]: int(row["count"]) for row in task_status}
    # Provider billing is delayed and may be temporarily unavailable; retain
    # the locally recorded amount as a safe fallback in that case.
    billing_cost = fetch_monthly_ark_cost(now_iso()[:7])
    return {
        "users": {"total": int(users["total"] or 0), "active": int(users["active"] or 0), "credits": int(users["credits"] or 0)},
        "tasks": {"total": sum(statuses.values()), "queued": statuses.get("queued", 0), "running": statuses.get("running", 0), "done": statuses.get("done", 0), "failed": statuses.get("failed", 0)},
        "creditsUsed": int(usage["used"] or 0),
        "apiCostRmb": billing_cost if billing_cost is not None else round(float(usage["api_cost"] or 0), 2),
        "recentTasks": [dict(row) for row in recent],
        "sections": {"analytics": False, "funnel": False, "retention": False, "codeManagement": False},
        "timestamp": now_iso(),
    }


def admin_users(request: Request, keyword: str = "", status: str = "all", limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)) -> dict[str, Any]:
    _require_admin(request)
    clauses: list[str] = []
    params: list[Any] = []
    if keyword.strip():
        clauses.append("(u.email LIKE ? OR u.name LIKE ?)")
        value = f"%{keyword.strip()}%"
        params.extend([value, value])
    if status == "active":
        clauses.append("u.is_active = 1")
    elif status == "disabled":
        clauses.append("u.is_active = 0")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with db() as connection:
        total = connection.execute(f"SELECT COUNT(*) AS count FROM users u {where}", params).fetchone()["count"]
        rows = connection.execute(
            f"SELECT u.id, u.email, u.name, u.role, u.is_active, u.credits, u.plan, u.created_at, u.last_login_at, COUNT(t.id) AS task_count, COALESCE(SUM(CASE WHEN t.status = 'done' THEN t.credits_used ELSE 0 END), 0) AS total_used FROM users u LEFT JOIN tasks t ON t.user_id = u.id {where} GROUP BY u.id ORDER BY u.created_at DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
    return {"items": [{**dict(row), "isActive": bool(row["is_active"]), "createdAt": row["created_at"], "lastLoginAt": row["last_login_at"], "taskCount": int(row["task_count"]), "totalUsed": int(row["total_used"])} for row in rows], "total": int(total), "limit": limit, "offset": offset}


async def admin_user_status(request: Request, user_id: str) -> dict[str, Any]:
    admin = _require_admin(request)
    payload = await request.json()
    active = bool(payload.get("isActive"))
    if user_id == admin["id"] and not active:
        raise HTTPException(status_code=400, detail="不能禁用当前管理员账号")
    with db() as connection:
        row = connection.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="用户不存在")
        connection.execute("UPDATE users SET is_active = ? WHERE id = ?", (1 if active else 0, user_id))
        if not active:
            connection.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    _admin_audit(admin["id"], "user_status", "user", user_id, f"isActive={active}")
    return {"id": user_id, "isActive": active}


async def admin_user_credits(request: Request, user_id: str) -> dict[str, Any]:
    admin = _require_admin(request)
    payload = await request.json()
    try:
        rmb_amount = float(payload.get("rmb_amount"))
    except (TypeError, ValueError):
        rmb_amount = 0
    if rmb_amount <= 0 or rmb_amount > 1_000_000:
        raise HTTPException(status_code=400, detail="人民币金额必须大于 0")
    points_amount = round(rmb_amount * 13.8, 1)
    reason = str(payload.get("reason") or "管理员人民币充值")[:200]
    with db() as connection:
        row = connection.execute("SELECT credits FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="用户不存在")
        amount = points_amount
        balance = round(float(row["credits"]) + points_amount, 1)
        connection.execute("UPDATE users SET credits = ? WHERE id = ?", (balance, user_id))
        connection.execute("INSERT INTO credit_ledger(user_id, amount, balance_after, entry_type, reason, admin_user_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (user_id, amount, balance, "recharge" if amount > 0 else "adjustment", reason, admin["id"], now_iso()))
        connection.execute("INSERT INTO admin_recharge_ledger(user_id, rmb_amount, points_amount, balance_after, admin_user_id, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (user_id, rmb_amount, points_amount, balance, admin["id"], reason, now_iso()))
    _admin_audit(admin["id"], "credit_recharge_rmb", "user", user_id, f"rmb={rmb_amount}; points={points_amount}; reason={reason}")
    return {"userId": user_id, "rmbAmount": rmb_amount, "pointsAmount": points_amount, "balance": balance, "reason": reason}


def admin_recharge_ledger(request: Request, keyword: str = "", date_from: str = "", date_to: str = "", limit: int = Query(20, ge=1, le=200), offset: int = Query(0, ge=0)) -> dict[str, Any]:
    _require_admin(request)
    clauses: list[str] = []
    params: list[Any] = []
    if keyword.strip():
        value = f"%{keyword.strip()}%"
        clauses.append("(u.email LIKE ? OR u.name LIKE ? OR reason LIKE ?)")
        params.extend([value, value, value])
    if date_from:
        clauses.append("created_at >= ?")
        params.append(f"{date_from}T00:00:00")
    if date_to:
        clauses.append("created_at <= ?")
        params.append(f"{date_to}T23:59:59")
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    with db() as connection:
        recharge = connection.execute("SELECT r.id, r.user_id, r.rmb_amount, r.points_amount, r.balance_after, r.reason, r.created_at, u.email, u.name, 'recharge' AS entry_type FROM admin_recharge_ledger r LEFT JOIN users u ON u.id = r.user_id").fetchall()
        credit = connection.execute("SELECT c.id, c.user_id, 0 AS rmb_amount, c.amount AS points_amount, c.balance_after, c.reason, c.created_at, u.email, u.name, c.entry_type FROM credit_ledger c LEFT JOIN users u ON u.id = c.user_id").fetchall()
    combined = [dict(row) for row in (*recharge, *credit)]
    if clauses:
        def matches(item: dict[str, Any]) -> bool:
            if keyword.strip() and keyword.strip().lower() not in " ".join(str(item.get(key) or "") for key in ("email", "name", "reason")).lower(): return False
            if date_from and str(item.get("created_at") or "") < f"{date_from}T00:00:00": return False
            if date_to and str(item.get("created_at") or "") > f"{date_to}T23:59:59": return False
            return True
        combined = [item for item in combined if matches(item)]
    combined.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return {"total": len(combined), "items": combined[offset:offset + limit]}


def admin_user_ledger(request: Request, user_id: str, limit: int = Query(100, ge=1, le=200)) -> list[dict[str, Any]]:
    _require_admin(request)
    with db() as connection:
        rows = connection.execute("SELECT id, amount, balance_after, entry_type, reason, admin_user_id, created_at FROM credit_ledger WHERE user_id = ? ORDER BY id DESC LIMIT ?", (user_id, limit)).fetchall()
    return [dict(row) for row in rows]


def admin_tasks(request: Request, keyword: str = "", status: str = "all", user_id: str = "", date_from: str = "", date_to: str = "", limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)) -> dict[str, Any]:
    _require_admin(request)
    clauses: list[str] = []
    params: list[Any] = []
    if keyword.strip():
        clauses.append("(t.title LIKE ? OR t.file_name LIKE ? OR u.email LIKE ?)")
        value = f"%{keyword.strip()}%"
        params.extend([value, value, value])
    if status != "all":
        clauses.append("t.status = ?"); params.append(status)
    if user_id:
        clauses.append("t.user_id = ?"); params.append(user_id)
    if date_from.strip():
        clauses.append("t.created_at >= ?"); params.append(date_from.strip())
    if date_to.strip():
        clauses.append("t.created_at < ?"); params.append(date_to.strip() + "T23:59:59.999999+00:00")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with db() as connection:
        rows = connection.execute(f"SELECT t.id, t.user_id, t.title, t.file_name, t.status, t.stage, t.progress_percent, t.estimated_minutes, t.credits_used, t.error, t.created_at, t.updated_at, t.batch_id, t.batch_title, t.batch_index, t.batch_total, COALESCE(u.email, t.user_id, '-') AS email, u.name FROM tasks t LEFT JOIN users u ON u.id=t.user_id {where} ORDER BY t.created_at DESC", params).fetchall()
    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        key = item.get("batch_id") or item["id"]
        group = groups.get(key)
        if not group:
            group = {**item, "id": item["id"], "title": item.get("batch_title") or item["title"], "file_name": "", "task_count": 0, "done_count": 0, "failed_count": 0, "credits_used": 0, "estimated_minutes": 0, "progress_percent": 0}
            groups[key] = group
        group["task_count"] += 1
        group["done_count"] += int(item["status"] in {"done", "review"})
        group["failed_count"] += int(item["status"] == "failed")
        group["credits_used"] += int(item.get("credits_used") or 0)
        group["estimated_minutes"] += int(item.get("estimated_minutes") or 0)
        group["progress_percent"] = round((group["progress_percent"] * (group["task_count"] - 1) + int(item.get("progress_percent") or 0)) / group["task_count"])
        if item["status"] == "failed": group["status"] = "failed"
        elif group["status"] != "failed" and item["status"] == "running": group["status"] = "running"
        elif group["status"] not in {"failed", "running"} and item["status"] == "queued": group["status"] = "queued"
        group["file_name"] = f"{group['task_count']} 集"
    grouped = list(groups.values())
    grouped.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return {"items": grouped[offset:offset + limit], "total": len(grouped), "limit": limit, "offset": offset}


async def admin_retry_task(request: Request, task_id: str, background: BackgroundTasks) -> dict[str, Any]:
    admin = _require_admin(request)
    row = task_row(task_id)
    if not row:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not Path(row["stored_path"]).exists():
        raise HTTPException(status_code=409, detail="原始视频不存在")
    update_task(task_id, status="queued", stage="queued", progress_percent=4, error=None, completed_at=None)
    record_task_event(task_id, "admin_retry", "管理员重新排队", status="queued", stage="queued", progress_percent=4)
    _admin_audit(admin["id"], "task_retry", "task", task_id)
    background.add_task(process_task, task_id)
    return {"id": task_id, "status": "queued"}


def admin_audit_logs(request: Request, limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0)) -> dict[str, Any]:
    _require_admin(request)
    with db() as connection:
        total = connection.execute("SELECT COUNT(*) AS count FROM admin_audit_logs").fetchone()["count"]
        rows = connection.execute("SELECT a.*, u.email AS admin_email FROM admin_audit_logs a LEFT JOIN users u ON u.id=a.admin_user_id ORDER BY a.id DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
    return {"items": [dict(row) for row in rows], "total": int(total), "limit": limit, "offset": offset}


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
        # The workspace is a recency-based task inbox: the most recently
        # created task must be visible first. Keep the id as a deterministic
        # tie-breaker for tasks created in the same timestamp tick.
        rows = connection.execute(f"SELECT * FROM tasks {where} ORDER BY created_at DESC, id DESC", params).fetchall()
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
    task_title = title_with_episode(title.strip() or title_from_filename(file_name), file_name)
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
        charge_points = estimated * POINTS_PER_MINUTE
        if charge_points > int(user["credits"]):
            target.unlink(missing_ok=True)
            raise HTTPException(status_code=402, detail=f"积分不足，当前剩余 {user['credits']} 积分")
        balance_after = int(user["credits"]) - charge_points
        connection.execute("UPDATE users SET credits = ? WHERE id = ?", (balance_after, user["id"]))
        connection.execute("INSERT INTO credit_ledger(user_id, amount, balance_after, entry_type, reason, created_at) VALUES (?, ?, ?, ?, ?, ?)", (user["id"], -charge_points, balance_after, "consume", "视频识别消耗", created))
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


async def create_tasks_batch(
    request: Request,
    files: list[UploadFile] = File(...),
    title: str = Form(""),
) -> dict[str, Any]:
    """Persist up to 999 independent MP4 jobs for the durable worker queue."""
    user = current_user(request)
    if not files or len(files) > 999:
        raise HTTPException(status_code=400, detail="一次最多上传 999 个视频")
    prepared: list[dict[str, Any]] = []
    try:
        for upload in files:
            name = safe_filename(upload.filename or "video.mp4")
            if not name.lower().endswith(".mp4"):
                raise HTTPException(status_code=400, detail="目前只支持 MP4 视频")
            task_id = f"task-{uuid.uuid4().hex}"
            target = UPLOAD_DIR / f"{task_id}.mp4"
            size = 0
            with target.open("wb") as output:
                while chunk := await upload.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise HTTPException(status_code=413, detail=f"{name} 超过 500 MB")
                    output.write(chunk)
            duration = probe_duration(target) or 0
            if duration <= 0 or duration > MAX_DURATION_SECONDS:
                raise HTTPException(status_code=400, detail=f"{name} 时长必须不超过 {MAX_DURATION_MINUTES} 分钟")
            minutes = max(1, int((duration + 59) // 60))
            prepared.append({"id": task_id, "name": name, "target": target, "size": size, "duration": duration, "minutes": minutes, "input_index": len(prepared)})
    except Exception:
        for item in prepared:
            item["target"].unlink(missing_ok=True)
        raise
    total_points = sum(item["minutes"] * POINTS_PER_MINUTE for item in prepared)
    if total_points > int(user["credits"]):
        for item in prepared:
            item["target"].unlink(missing_ok=True)
        raise HTTPException(status_code=402, detail=f"积分不足，预计需要 {total_points:.1f} 积分")
    with db() as connection:
        queue = _queue_summary(connection, sum(item["minutes"] for item in prepared))
    if queue["blocked"]:
        for item in prepared:
            item["target"].unlink(missing_ok=True)
        raise HTTPException(status_code=429, detail=f"当前任务排队预计超过 {QUEUE_MAX_WAIT_MINUTES // 60} 小时，请稍后再上传", headers={"Retry-After": "600"})
    created = now_iso()
    batch_id = f"batch-{uuid.uuid4().hex}"
    batch_title = title.strip() or f"短剧批次 {created[:16].replace('T', ' ')}"
    prepared.sort(key=lambda item: episode_sort_key(item["name"], int(item.get("input_index") or 0)))
    with db() as connection:
        for index, item in enumerate(prepared, start=1):
            connection.execute("INSERT INTO tasks (id, user_id, title, file_name, stored_path, file_size, mime_type, duration_sec, estimated_minutes, credits_used, status, stage, progress_percent, created_at, updated_at, batch_id, batch_title, batch_index, batch_total) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 'queued', 4, ?, ?, ?, ?, ?, ?)", (item["id"], user["id"], title_with_episode(title.strip() or title_from_filename(item["name"]), item["name"]), item["name"], str(item["target"]), item["size"], "video/mp4", item["duration"], item["minutes"], item["minutes"], created, created, batch_id, batch_title, index, len(prepared)))
        balance = int(user["credits"]) - total_points
        connection.execute("UPDATE users SET credits = ? WHERE id = ?", (balance, user["id"]))
        connection.execute("INSERT INTO credit_ledger(user_id, amount, balance_after, entry_type, reason, created_at) VALUES (?, ?, ?, ?, ?, ?)", (user["id"], -total_points, balance, "consume", "视频识别消费", created))
    result = []
    for item in prepared:
        record_task_event(item["id"], "created", "任务已进入队列", status="queued", stage="queued", progress_percent=4)
        result.append(get_task(item["id"], user["id"]))
    return {"tasks": result, "count": len(result)}


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


def download_all_tasks(request: Request, fmt: str = Query("md", pattern="^(md|txt)$")) -> Response:
    user = current_user(request)
    with db() as connection:
        rows = connection.execute("SELECT * FROM tasks WHERE user_id = ? AND status = 'done' ORDER BY created_at ASC", (user["id"],)).fetchall()
    if not rows:
        raise HTTPException(status_code=409, detail="暂无已完成剧本")
    rows = [
        row
        for _, row in sorted(
            enumerate(rows),
            key=lambda pair: episode_sort_key(str(pair[1]["file_name"] or ""), pair[0]),
        )
    ]
    content_parts = []
    for row in rows:
        task = row_to_task(row)
        markdown = script_to_markdown(task)
        content_parts.append(markdown if fmt == "md" else re.sub(r"^#{1,6}\\s+", "", markdown, flags=re.MULTILINE))
    content = "\n\n---\n\n".join(content_parts)
    filename = f"全部剧本.{fmt}"
    encoded_filename = quote(filename)
    return Response(content=content, media_type="text/markdown" if fmt == "md" else "text/plain", headers={"X-Filename": encoded_filename, "Content-Disposition": f"attachment; filename=all-scripts.{fmt}; filename*=UTF-8''{encoded_filename}"})


def download_batch(request: Request, batch_id: str, fmt: str = Query("md", pattern="^(md|txt)$")) -> Response:
    user = current_user(request)
    with db() as connection:
        rows = connection.execute(
            "SELECT * FROM tasks WHERE user_id = ? AND batch_id = ? AND status = 'done' ORDER BY created_at ASC",
            (user["id"], batch_id),
        ).fetchall()
    if not rows:
        raise HTTPException(status_code=409, detail="该批次暂无可下载的剧本")
    rows = sorted(rows, key=lambda row: episode_sort_key(str(row["file_name"] or ""), int(row["batch_index"] or 0)))
    parts = []
    for row in rows:
        markdown = script_to_markdown(row_to_task(row))
        parts.append(markdown if fmt == "md" else re.sub(r"^#{1,6}\s+", "", markdown, flags=re.MULTILINE))
    content = "\n\n---\n\n".join(parts)
    title = safe_filename(rows[0]["batch_title"] or "批次剧本")
    encoded_filename = quote(f"{title}.{fmt}")
    return Response(content=content, media_type="text/markdown" if fmt == "md" else "text/plain", headers={"X-Filename": encoded_filename, "Content-Disposition": f"attachment; filename=batch-scripts.{fmt}; filename*=UTF-8''{encoded_filename}"})


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
