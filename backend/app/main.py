"""Application entrypoint.

The web service is intentionally assembled here in one place.  Domain code is
kept in small modules so a page/API area can be found without reading the
entire backend file, while route paths and middleware remain easy to audit.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .auth_store import init_db
from .config import FRONTEND_DIR
from .middleware import RateLimitMiddleware, RequestLogMiddleware
from .routes import (
    admin_audit_logs,
    admin_overview,
    admin_recharge_ledger,
    admin_retry_task,
    admin_tasks,
    admin_user_credits,
    admin_user_ledger,
    admin_user_status,
    admin_users,
    create_task,
    delete_task,
    download_task,
    health,
    list_tasks,
    login,
    logout,
    metrics,
    profile,
    register,
    request_auth_code,
    reset_password,
    retry_task,
    task_detail,
    task_event_list,
    user_credit_ledger,
)


# ---------------------------------------------------------------------------
# FastAPI application and cross-cutting middleware
# ---------------------------------------------------------------------------

app = FastAPI(title="剧编编 API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_origin_regex=r".*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(RequestLogMiddleware)


# ---------------------------------------------------------------------------
# API route table (the handlers themselves live in routes.py)
# ---------------------------------------------------------------------------

app.add_api_route("/api/health", health, methods=["GET"])
app.add_api_route("/api/metrics", metrics, methods=["GET"])
app.add_api_route("/api/auth/request-code", request_auth_code, methods=["POST"])
app.add_api_route("/api/auth/register", register, methods=["POST"])
app.add_api_route("/api/auth/login", login, methods=["POST"])
app.add_api_route("/api/auth/reset-password", reset_password, methods=["POST"])
app.add_api_route("/api/auth/logout", logout, methods=["POST"])
app.add_api_route("/api/me", profile, methods=["GET"])
app.add_api_route("/api/me/ledger", user_credit_ledger, methods=["GET"])
app.add_api_route("/api/admin/overview", admin_overview, methods=["GET"])
app.add_api_route("/api/admin/users", admin_users, methods=["GET"])
app.add_api_route("/api/admin/users/{user_id}/status", admin_user_status, methods=["PATCH"])
app.add_api_route("/api/admin/users/{user_id}/credits", admin_user_credits, methods=["POST"])
app.add_api_route("/api/admin/recharges", admin_recharge_ledger, methods=["GET"])
app.add_api_route("/api/admin/users/{user_id}/ledger", admin_user_ledger, methods=["GET"])
app.add_api_route("/api/admin/tasks", admin_tasks, methods=["GET"])
app.add_api_route("/api/admin/tasks/{task_id}/retry", admin_retry_task, methods=["POST"])
app.add_api_route("/api/admin/audit-logs", admin_audit_logs, methods=["GET"])
app.add_api_route("/api/tasks", list_tasks, methods=["GET"])
app.add_api_route("/api/tasks/{task_id}", task_detail, methods=["GET"])
app.add_api_route("/api/tasks/{task_id}/events", task_event_list, methods=["GET"])
app.add_api_route("/api/tasks", create_task, methods=["POST"])
app.add_api_route("/api/tasks/{task_id}/retry", retry_task, methods=["POST"])
app.add_api_route("/api/tasks/{task_id}", delete_task, methods=["DELETE"], status_code=204)
app.add_api_route("/api/tasks/{task_id}/download", download_task, methods=["GET"])


# Create tables before the first request, matching the original startup order.
init_db()


# The frontend is served last so API routes always win over static fallback.
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")
