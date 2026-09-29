"""Background task orchestration and provider fallback policy."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from .config import (
    POINTS_PER_MINUTE,
    ARK_API_KEY,
    ARK_FALLBACK_ON_ERROR,
    ARK_MODEL,
    OPENAI_API_KEY,
    OPENAI_TEXT_MODEL,
)
from .auth_store import db, now_iso
from .errors import ArkError
from .logging_setup import logger, safe_error_text
from .media import probe_duration, sample_script
from .providers import ark_fallback_message, ark_recognize, openai_script, openai_transcribe
from .script import normalize_script, quality_gate, script_quality
from .task_store import mark_task_stage, record_task_event, task_row, update_task

# Provider selection and fallback policy live here; HTTP routes only enqueue work.
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
    ark_error: ArkError | None = None
    if ARK_API_KEY:
        try:
            script, usage = await asyncio.to_thread(ark_recognize, path, row["title"], duration)
            script = normalize_script(script, row["title"])
            quality = script_quality(script, "ark")
            approved, blocking = quality_gate(quality)
            quality["deliveryStatus"] = "approved" if approved else "review_required"
            quality["blockingIssues"] = blocking
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
    approved, blocking = quality_gate(quality)
    quality["deliveryStatus"] = "approved" if approved else "review_required"
    quality["blockingIssues"] = blocking
    if ark_error:
        quality["warning"] = ark_fallback_message(ark_error)
        if openai_error and provider == "local-fallback":
            quality["warning"] += " OpenAI 备用服务也不可用，已使用本地兜底结果。"
    usage = {"input_tokens": None, "output_tokens": None, "total_tokens": None, "api_cost_rmb": 0}
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
        mark_task_stage(task_id, status="running", stage="transcribing", progress_percent=34, message="正在整理语音和对白")
        mark_task_stage(task_id, status="running", stage="vision", progress_percent=62, message="正在识别画面与动作")
        script, quality, usage = await run_recognizer(task_row(task_id))
        mark_task_stage(task_id, status="running", stage="merging", progress_percent=82, message="正在合并场景和人物")
        mark_task_stage(task_id, status="running", stage="exporting", progress_percent=94, message="正在生成可下载剧本")
        elapsed_ms = (time.perf_counter() - started) * 1000
        approved = quality.get("deliveryStatus") == "approved"
        final_status = "done" if approved else "review"
        final_stage = "done" if approved else "review"
        final_message = "识别完成" if approved else "已生成，等待人工复核（存在 P0/P1 级质量问题）"
        update_task(
            task_id,
            status=final_status,
            stage=final_stage,
            progress_percent=100,
            result_json=json.dumps(script, ensure_ascii=False),
            quality_json=json.dumps(quality, ensure_ascii=False),
            provider=usage.get("provider", "ark" if ARK_API_KEY else "local-fallback"),
            model=usage.get("model", ARK_MODEL if ARK_API_KEY else None),
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            total_tokens=usage.get("total_tokens"),
            api_cost_rmb=usage.get("api_cost_rmb", 0),
            completed_at=now_iso(),
        )
        record_task_event(
            task_id,
            "completed" if approved else "review_required",
            final_message,
            status=final_status,
            stage=final_stage,
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
        # Return the pre-charged minutes exactly once when a recognition fails.
        charged = int(row["credits_used"] or 0)
        if row["user_id"] and charged > 0:
            with db() as connection:
                refund_points = charged * POINTS_PER_MINUTE
                user_row = connection.execute("SELECT credits FROM users WHERE id = ?", (row["user_id"],)).fetchone()
                balance_after = int(user_row["credits"] or 0) + refund_points if user_row else refund_points
                connection.execute("UPDATE users SET credits = ? WHERE id = ?", (balance_after, row["user_id"]))
                connection.execute("INSERT INTO credit_ledger(user_id, amount, balance_after, entry_type, reason, created_at) VALUES (?, ?, ?, ?, ?, ?)", (row["user_id"], refund_points, balance_after, "refund", "识别失败退回", now_iso()))
            update_task(task_id, credits_used=0)
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
