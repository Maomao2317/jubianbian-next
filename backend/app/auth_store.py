"""Authentication persistence, password hashing, sessions, and email codes."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request as UrlRequest, urlopen

from fastapi import HTTPException, Request, Response

from .config import (
    ADMIN_EMAILS,
    AUTH_CODE_MAX_ATTEMPTS,
    AUTH_CODE_RESEND_SECONDS,
    AUTH_CODE_TTL_SECONDS,
    COOKIE_SECURE,
    DB_PATH,
    SESSION_COOKIE,
    SESSION_TTL_SECONDS,
    TENCENTCLOUD_REGION,
    TENCENTCLOUD_SECRET_ID,
    TENCENTCLOUD_SECRET_KEY,
    TENCENTCLOUD_SES_ENDPOINT,
    TENCENTCLOUD_SES_FROM_EMAIL,
    TENCENTCLOUD_SES_FROM_NAME,
    TENCENTCLOUD_SES_TEMPLATE_ID,
)
from .logging_setup import _email_log_id, logger, safe_error_text

# Passwords and session cookies
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 180_000)
    return f"pbkdf2_sha256$180000${salt.hex()}${digest.hex()}"


def _password_matches(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt_hex, digest_hex = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds))
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _user_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "email": row["email"],
        "name": row["name"],
        "credits": row["credits"],
        "plan": row["plan"],
        "createdAt": row["created_at"],
        "role": row["role"] if "role" in row.keys() else "user",
        "isActive": bool(row["is_active"]) if "is_active" in row.keys() else True,
    }


def _get_user_by_id(user_id: str) -> sqlite3.Row | None:
    with db() as connection:
        return connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def current_user(request: Request) -> sqlite3.Row:
    token = request.cookies.get(SESSION_COOKIE, "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="请先登录")
    with db() as connection:
        row = connection.execute(
            """
            SELECT users.* FROM sessions JOIN users ON users.id = sessions.user_id
            WHERE sessions.token_hash = ? AND sessions.expires_at > ?
            """,
            (_token_hash(token), time.time()),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    if "is_active" in row.keys() and not row["is_active"]:
        raise HTTPException(status_code=403, detail="账号已被禁用，请联系管理员")
    return row


def _set_session(response: Response, user_id: str) -> None:
    token = secrets.token_urlsafe(32)
    with db() as connection:
        connection.execute(
            "INSERT INTO sessions(token_hash, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
            (_token_hash(token), user_id, time.time() + SESSION_TTL_SECONDS, now_iso()),
        )
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=COOKIE_SECURE,
    )


def _send_tencentcloud_code(email: str, code: str) -> bool:
    """Send a transactional email through Tencent Cloud SES (TC3 signature)."""
    email_id = _email_log_id(email)
    sender_ok = bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", TENCENTCLOUD_SES_FROM_EMAIL))
    if not TENCENTCLOUD_SECRET_ID or not TENCENTCLOUD_SECRET_KEY or not sender_ok or not TENCENTCLOUD_SES_TEMPLATE_ID:
        logger.warning(
            "tencentcloud_ses_not_configured email_id=%s sender_configured=%s credentials_configured=%s",
            email_id,
            sender_ok,
            bool(TENCENTCLOUD_SECRET_ID and TENCENTCLOUD_SECRET_KEY),
        )
        return False
    service = "ses"
    endpoint = TENCENTCLOUD_SES_ENDPOINT.strip()
    parsed_endpoint = urlsplit(endpoint if "://" in endpoint else f"https://{endpoint}")
    host = parsed_endpoint.netloc or parsed_endpoint.path
    if not host:
        logger.warning("tencentcloud_ses_invalid_endpoint email_id=%s", email_id)
        return False
    version = "2020-10-02"
    action = "SendEmail"
    timestamp = int(time.time())
    date = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")
    subject = "剧编编邮箱验证码"
    body = {
        "FromEmailAddress": TENCENTCLOUD_SES_FROM_EMAIL,
        "Destination": [email],
        "Subject": subject,
        "Template": {
            "TemplateID": TENCENTCLOUD_SES_TEMPLATE_ID,
            "TemplateData": json.dumps({"code": code}, ensure_ascii=False, separators=(",", ":")),
        },
    }
    payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    content_type = "application/json; charset=utf-8"
    canonical_headers = f"content-type:{content_type}\nhost:{host}\n"
    signed_headers = "content-type;host"
    hashed_payload = hashlib.sha256(payload).hexdigest()
    canonical_request = "\n".join(["POST", "/", "", canonical_headers, signed_headers, hashed_payload])
    credential_scope = f"{date}/{service}/tc3_request"
    string_to_sign = "\n".join([
        "TC3-HMAC-SHA256",
        str(timestamp),
        credential_scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    secret_date = hmac.new(("TC3" + TENCENTCLOUD_SECRET_KEY).encode("utf-8"), date.encode("utf-8"), hashlib.sha256).digest()
    secret_service = hmac.new(secret_date, service.encode("utf-8"), hashlib.sha256).digest()
    secret_signing = hmac.new(secret_service, b"tc3_request", hashlib.sha256).digest()
    signature = hmac.new(secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    authorization = (
        f"TC3-HMAC-SHA256 Credential={TENCENTCLOUD_SECRET_ID}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    request = UrlRequest(
        f"{parsed_endpoint.scheme or 'https'}://{host}/",
        data=payload,
        headers={
            "Content-Type": content_type,
            "Host": host,
            "X-TC-Action": action,
            "X-TC-Version": version,
            "X-TC-Region": TENCENTCLOUD_REGION,
            "X-TC-Timestamp": str(timestamp),
            "Authorization": authorization,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=20) as response:
            response_payload = json.loads(response.read().decode("utf-8") or "{}")
            error = response_payload.get("Response", {}).get("Error")
            if error:
                logger.warning(
                    "tencentcloud_ses_rejected email_id=%s code=%s message=%s",
                    email_id,
                    error.get("Code"),
                    safe_error_text(RuntimeError(str(error.get("Message") or "unknown")), 300),
                )
                return False
            return 200 <= response.status < 300
    except HTTPError as exc:
        detail = ""
        try:
            raw_error = exc.read().decode("utf-8", errors="replace")
            payload_error = json.loads(raw_error).get("Response", {}).get("Error", {})
            detail = f"{payload_error.get('Code') or ''} {payload_error.get('Message') or ''}".strip()
        except (OSError, ValueError, AttributeError):
            pass
        logger.warning(
            "tencentcloud_ses_failed email_id=%s status=%s error=%s",
            email_id,
            exc.code,
            safe_error_text(RuntimeError(detail or str(exc)), 300),
        )
        return False
    except (URLError, OSError) as exc:
        logger.warning("tencentcloud_ses_failed email_id=%s error=%s", email_id, safe_error_text(exc, 300))
        return False
    except Exception as exc:  # pragma: no cover - defensive boundary for provider failures
        logger.exception("tencentcloud_ses_unexpected_failure email_id=%s error=%s", email_id, safe_error_text(exc, 300))
        return False


def _issue_auth_code(email: str, purpose: str) -> tuple[str, bool]:
    code = f"{secrets.randbelow(1_000_000):06d}"
    issued_at = time.time()
    with db() as connection:
        connection.execute("UPDATE auth_codes SET used_at = ? WHERE email = ? AND purpose = ? AND used_at IS NULL", (now_iso(), email, purpose))
        connection.execute(
            "INSERT INTO auth_codes(email, purpose, code_hash, expires_at, attempts, issued_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (email, purpose, _token_hash(code), issued_at + AUTH_CODE_TTL_SECONDS, 0, issued_at, now_iso()),
        )
    return code, _send_tencentcloud_code(email, code)


def _auth_code_retry_after(email: str, purpose: str) -> int:
    with db() as connection:
        row = connection.execute(
            "SELECT issued_at, created_at FROM auth_codes WHERE email = ? AND purpose = ? AND used_at IS NULL AND expires_at > ? ORDER BY id DESC LIMIT 1",
            (email, purpose, time.time()),
        ).fetchone()
    if not row:
        return 0
    issued_at = row["issued_at"]
    if issued_at is None:
        try:
            issued_at = datetime.fromisoformat(str(row["created_at"])).timestamp()
        except (TypeError, ValueError, OverflowError):
            return 0
    return max(0, int(AUTH_CODE_RESEND_SECONDS - (time.time() - float(issued_at)) + 0.999))


def _invalidate_auth_code(email: str, purpose: str) -> None:
    with db() as connection:
        connection.execute(
            "UPDATE auth_codes SET used_at = ? WHERE email = ? AND purpose = ? AND used_at IS NULL",
            (now_iso(), email, purpose),
        )


def _verify_auth_code(email: str, purpose: str, code: str) -> bool:
    with db() as connection:
        row = connection.execute(
            "SELECT id, code_hash, attempts FROM auth_codes WHERE email = ? AND purpose = ? AND used_at IS NULL AND expires_at > ? ORDER BY id DESC LIMIT 1",
            (email, purpose, time.time()),
        ).fetchone()
        if not row:
            return False
        if not hmac.compare_digest(row["code_hash"], _token_hash(code)):
            attempts = int(row["attempts"] or 0) + 1
            if attempts >= AUTH_CODE_MAX_ATTEMPTS:
                connection.execute("UPDATE auth_codes SET attempts = ?, used_at = ? WHERE id = ?", (attempts, now_iso(), row["id"]))
            else:
                connection.execute("UPDATE auth_codes SET attempts = ? WHERE id = ?", (attempts, row["id"]))
            return False
        connection.execute("UPDATE auth_codes SET used_at = ? WHERE id = ?", (now_iso(), row["id"]))
    return True


def db() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    with db() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                name TEXT NOT NULL,
                credits INTEGER NOT NULL DEFAULT 9999,
                plan TEXT NOT NULL DEFAULT '体验版',
                created_at TEXT NOT NULL,
                last_login_at TEXT
            )
            """
        )
        columns = {item[1] for item in connection.execute("PRAGMA table_info(users)").fetchall()}
        for name, definition in (("role", "TEXT NOT NULL DEFAULT 'user'"), ("is_active", "INTEGER NOT NULL DEFAULT 1")):
            if name not in columns:
                connection.execute(f"ALTER TABLE users ADD COLUMN {name} {definition}")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_users_role ON users(role, created_at)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_users_created ON users(created_at DESC, id)")
        if ADMIN_EMAILS:
            placeholders = ",".join("?" for _ in ADMIN_EMAILS)
            connection.execute(
                f"UPDATE users SET role = 'admin' WHERE lower(email) IN ({placeholders})",
                tuple(sorted(ADMIN_EMAILS)),
            )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS auth_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                purpose TEXT NOT NULL,
                code_hash TEXT NOT NULL,
                expires_at REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                issued_at REAL,
                used_at TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        auth_code_columns = {item[1] for item in connection.execute("PRAGMA table_info(auth_codes)").fetchall()}
        for name, definition in (("attempts", "INTEGER NOT NULL DEFAULT 0"), ("issued_at", "REAL")):
            if name not in auth_code_columns:
                connection.execute(f"ALTER TABLE auth_codes ADD COLUMN {name} {definition}")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_auth_codes_email ON auth_codes(email, purpose, id)")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                expires_at REAL NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
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
                evidence_json TEXT,
                provider TEXT,
                model TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                total_tokens INTEGER,
                api_cost_rmb REAL NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0,
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
            ("user_id", "TEXT"),
            ("provider", "TEXT"),
            ("model", "TEXT"),
            ("evidence_json", "TEXT"),
            ("input_tokens", "INTEGER"),
            ("output_tokens", "INTEGER"),
            ("total_tokens", "INTEGER"),
            ("api_cost_rmb", "REAL NOT NULL DEFAULT 0"),
            ("attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("batch_id", "TEXT"),
            ("batch_title", "TEXT"),
            ("batch_index", "INTEGER"),
            ("batch_total", "INTEGER"),
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
        connection.execute("CREATE INDEX IF NOT EXISTS idx_tasks_user_id ON tasks(user_id, created_at)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_tasks_user_status_created ON tasks(user_id, status, created_at DESC, id DESC)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_tasks_admin_created ON tasks(created_at DESC, id DESC)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_tasks_admin_status_created ON tasks(status, created_at DESC, id DESC)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_tasks_batch_created ON tasks(batch_id, created_at DESC, id DESC)")
        # A completed task may be sent through the quality pipeline again by
        # an administrator. Keep the delivered version outside ``tasks`` so a
        # candidate result can never destroy it.
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS task_recheck_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                resolved_at TEXT
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_recheck_snapshots_task ON task_recheck_snapshots(task_id, id DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_task_recheck_snapshots_pending ON task_recheck_snapshots(task_id, state, id DESC)"
        )
        # ``review`` is a durable delivery state: the screenplay may be
        # inspected by its owner, but it must not be downloadable until an
        # administrator has confirmed it.  Do not silently migrate legacy
        # review rows to ``done``; doing so would bypass the quality gate.
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS credit_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                amount INTEGER NOT NULL,
                balance_after INTEGER NOT NULL,
                entry_type TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                admin_user_id TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_credit_ledger_user ON credit_ledger(user_id, id)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_credit_ledger_created ON credit_ledger(created_at DESC, id DESC)")
        # Backfill the visible signup grant for accounts created before the
        # ledger entry was introduced. This only records history; it never
        # changes the user's existing balance.
        connection.execute(
            """
            INSERT INTO credit_ledger(user_id, amount, balance_after, entry_type, reason, created_at)
            SELECT u.id, 25, 25, 'grant', '新用户注册赠送 25 积分', u.created_at
            FROM users u
            WHERE u.credits = 25
              AND NOT EXISTS (
                SELECT 1 FROM credit_ledger c
                WHERE c.user_id = u.id AND c.entry_type = 'grant'
                  AND c.reason LIKE '%注册赠送%'
              )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_recharge_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                rmb_amount REAL NOT NULL,
                points_amount REAL NOT NULL,
                balance_after REAL NOT NULL,
                admin_user_id TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_admin_recharge_created ON admin_recharge_ledger(created_at, id)")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_user_id TEXT NOT NULL,
                action TEXT NOT NULL,
                target_type TEXT NOT NULL DEFAULT '',
                target_id TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_admin_audit_created ON admin_audit_logs(created_at)")
