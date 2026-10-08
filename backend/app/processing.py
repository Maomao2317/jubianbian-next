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
    ARK_API_KEYS,
    ARK_FALLBACK_ON_ERROR,
    ARK_MODEL,
    OPENAI_API_KEY,
    OPENAI_TEXT_MODEL,
)
from .auth_store import db, now_iso
from .evidence import collect_evidence, evidence_json, transcript_from_evidence
from .errors import ArkError
from .logging_setup import logger, safe_error_text
from .media import probe_duration
from .providers import ark_fallback_message, ark_recognize, openai_script, openai_transcribe
from .script import normalize_script, quality_gate, script_quality
from .task_store import mark_task_stage, record_task_event, task_row, update_task

# Provider selection and fallback policy live here; HTTP routes only enqueue work.
async def run_recognizer(row: sqlite3.Row) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Collect evidence, then use Ark with an explicit OpenAI fallback."""
    path = Path(row["stored_path"])
    duration = float(row["duration_sec"] or 0)
    probed = await asyncio.to_thread(probe_duration, path)
    if probed:
        duration = probed
        update_task(row["id"], duration_sec=duration)
    evidence = await asyncio.to_thread(collect_evidence, path, row["title"], duration)
    ark_error: ArkError | None = None
    if ARK_API_KEYS:
        try:
            script, usage = await asyncio.to_thread(ark_recognize, path, row["title"], duration, evidence)
            script = normalize_script(script, row["title"])
            quality = script_quality(script, "ark")
            approved, blocking = quality_gate(quality)
            quality["deliveryStatus"] = "approved" if approved else "review_required"
            quality["blockingIssues"] = blocking
            quality["evidenceStatus"] = evidence.get("status")
            quality["evidenceSources"] = {source.get("kind"): source.get("status") for source in evidence.get("sources") or [] if isinstance(source, dict)}
            quality["evidenceSummary"] = evidence.get("summary", "")
            return script, quality, {**usage, "provider": "ark", "model": ARK_MODEL}, evidence
        except ArkError as exc:
            ark_error = exc
            logger.warning(
                "provider_failed provider=ark task_id=%s status_code=%s error=%s",
                row["id"],
                exc.status_code,
                safe_error_text(exc),
            )
            if not ARK_FALLBACK_ON_ERROR or not OPENAI_API_KEY:
                raise
    elif not OPENAI_API_KEY:
        raise ArkError("未配置方舟或 OpenAI API Key，任务无法进行真实视频识别")

    transcript = transcript_from_evidence(evidence)
    try:
        if not transcript:
            transcript = await asyncio.to_thread(openai_transcribe, path)
        script = await asyncio.to_thread(openai_script, row["title"], duration, transcript, evidence)
    except Exception as exc:
        logger.warning("provider_failed provider=openai task_id=%s error=%s", row["id"], safe_error_text(exc))
        raise ArkError("OpenAI 备用识别失败，未生成未经证实的剧本") from exc
    if not script:
        raise ArkError("OpenAI 未返回可用剧本，未生成未经证实的剧本")
    provider = "openai"
    script = normalize_script(script, row["title"])
    quality = script_quality(script, provider)
    approved, blocking = quality_gate(quality)
    quality["deliveryStatus"] = "approved" if approved else "review_required"
    quality["blockingIssues"] = blocking
    quality["evidenceStatus"] = evidence.get("status")
    quality["evidenceSources"] = {source.get("kind"): source.get("status") for source in evidence.get("sources") or [] if isinstance(source, dict)}
    quality["evidenceSummary"] = evidence.get("summary", "")
    if ark_error:
        quality["warning"] = ark_fallback_message(ark_error)
        quality["warning"] += " 本次结果使用了 OpenAI 备用识别链路。"
    usage = {"input_tokens": None, "output_tokens": None, "total_tokens": None, "api_cost_rmb": 0}
    return script, quality, {
        **usage,
        "provider": provider,
        "model": OPENAI_TEXT_MODEL,
    }, evidence


def _refund_task_once(task_id: str, user_id: str, charged_minutes: int) -> bool:
    """Refund a failed task exactly once, including the ledger entry."""
    refund_points = max(0, int(charged_minutes)) * POINTS_PER_MINUTE
    with db() as connection:
        claimed = connection.execute(
            "UPDATE tasks SET credits_used = 0, updated_at = ? WHERE id = ? AND credits_used > 0",
            (now_iso(), task_id),
        ).rowcount
        if claimed != 1 or refund_points <= 0:
            return True
        user_row = connection.execute("SELECT credits FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user_row:
            return True
        balance_after = int(user_row["credits"] or 0) + refund_points
        connection.execute("UPDATE users SET credits = ? WHERE id = ?", (balance_after, user_id))
        connection.execute(
            "INSERT INTO credit_ledger(user_id, amount, balance_after, entry_type, reason, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, refund_points, balance_after, "refund", "task recognition failed", now_iso()),
        )
    return True


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
        script, quality, usage, evidence = await run_recognizer(task_row(task_id))
        mark_task_stage(task_id, status="running", stage="merging", progress_percent=82, message="正在整理字幕和画面文字")
        mark_task_stage(task_id, status="running", stage="exporting", progress_percent=94, message="正在生成可下载剧本")
        elapsed_ms = (time.perf_counter() - started) * 1000
        # A successful provider response is not automatically a deliverable
        # screenplay.  P0/P1 findings mean that the system could not prove
        # the result is faithful to the source video, so keep the generated
        # script available for inspection but put the task behind the admin
        # review gate.  Only a clean quality gate is downloadable by users.
        approved, blocking = quality_gate(quality)
        quality["deliveryStatus"] = "approved" if approved else "review_required"
        quality["blockingIssues"] = blocking
        final_status = "done" if approved else "review"
        final_stage = "done" if approved else "review"
        final_message = "识别完成" if approved else "识别完成，等待管理员复核"
        update_task(
            task_id,
            status=final_status,
            stage=final_stage,
            progress_percent=100,
            result_json=json.dumps(script, ensure_ascii=False),
            quality_json=json.dumps(quality, ensure_ascii=False),
            evidence_json=evidence_json(evidence),
            provider=usage.get("provider", "ark" if ARK_API_KEYS else "local-fallback"),
            model=usage.get("model", ARK_MODEL if ARK_API_KEYS else None),
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
        attempts = int(row["attempts"] or 0) + 1
        if attempts <= 2:
            update_task(task_id, status="queued", stage="queued", progress_percent=4, attempts=attempts, error=f"第 {attempts} 次处理失败，正在自动重试：{safe_error_text(exc)}")
            record_task_event(task_id, "retry", "处理失败，任务已自动重新排队", status="queued", stage="queued", progress_percent=4)
            logger.warning("task_retry_auto task_id=%s attempt=%s error=%s", task_id, attempts, safe_error_text(exc))
            return
        message = f"处理失败：{safe_error_text(exc)}"
        # Return the pre-charged minutes exactly once when a recognition fails.
        charged = int(row["credits_used"] or 0)
        if row["user_id"] and charged > 0:
            _refund_task_once(task_id, row["user_id"], charged)
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
