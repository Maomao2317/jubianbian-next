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
    ARK_LITE_MODEL,
    ARK_MODEL,
    ARK_ROUTING_MODE,
    ARK_TURBO_MODEL,
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


def select_ark_route(duration_sec: float, evidence: dict[str, Any] | None) -> dict[str, Any]:
    """Choose Lite/Turbo before the video call using only deterministic signals.

    The route is deliberately conservative: missing audio or visual anchors
    increases the score because the model has less independent evidence to
    work with.  The result is stored with the task so cost and quality can be
    compared later without inferring the chosen model from environment state.
    """
    duration = max(0.0, float(duration_sec or 0))
    score = 0
    reasons: list[str] = []
    if duration >= 90:
        score += 1
        reasons.append("duration>=90s")
    if duration >= 180:
        score += 1
        reasons.append("duration>=180s")
    sources = evidence.get("sources") if isinstance(evidence, dict) else []
    sources = [source for source in sources or [] if isinstance(source, dict)]
    audio = next((source for source in sources if source.get("kind") == "audio"), None)
    ocr = next((source for source in sources if source.get("kind") == "ocr"), None)
    keyframes = next((source for source in sources if source.get("kind") == "keyframes"), None)
    transcript_chars = sum(len(str(item.get("text") or "")) for item in (audio or {}).get("items", []) if isinstance(item, dict))
    ocr_items = len((ocr or {}).get("items") or [])
    if transcript_chars >= 800:
        score += 1
        reasons.append("transcript>=800chars")
    if ocr_items >= 3:
        score += 1
        reasons.append("ocr>=3items")
    if not audio or audio.get("status") != "available":
        score += 1
        reasons.append("audio_unavailable")
    if not keyframes or keyframes.get("status") != "available":
        score += 1
        reasons.append("keyframes_unavailable")
    mode = ARK_ROUTING_MODE
    if mode in {"turbo", "turbo_only"}:
        use_turbo = True
        reasons.append("routing_mode=turbo")
    elif mode in {"lite", "lite_only"}:
        use_turbo = False
        reasons.append("routing_mode=lite")
    else:
        use_turbo = score >= 2
    selected_model = (ARK_TURBO_MODEL if use_turbo else ARK_LITE_MODEL).strip() or ARK_MODEL
    return {
        "model": selected_model,
        "band": "complex" if use_turbo else "simple",
        "score": score,
        "reasons": reasons,
        "mode": mode,
    }


def _is_lite_model_unavailable(error: ArkError) -> bool:
    text = f"{error} {error.provider_code}".casefold()
    return error.status_code in {400, 404} and any(
        marker in text
        for marker in ("modelnotopen", "model_not_open", "not active", "not found", "未激活", "未找到")
    )


def _is_non_retryable_provider_error(error: BaseException) -> bool:
    text = str(error).casefold()
    return any(
        marker in text
        for marker in (
            "read operation timed out",
            "timed out",
            "timeout",
            "超时",
            "modelnotopen",
            "model_not_open",
            "not active",
            "未激活",
        )
    )


def _task_error_message(error: BaseException) -> str:
    if _is_non_retryable_provider_error(error):
        text = str(error).casefold()
        if "modelnotopen" in text or "model_not_open" in text or "未激活" in text:
            return "当前 Lite 模型未启用，已停止重复重试；请启用该模型或改用 Turbo 后再识别。"
        return "方舟识别响应超时，已停止重复重试；请稍后重新识别。"
    return safe_error_text(error)


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
    route = select_ark_route(duration, evidence)
    ark_error: ArkError | None = None
    if ARK_API_KEYS:
        ark_script: dict[str, Any] | None = None
        ark_usage_data: dict[str, Any] | None = None
        try:
            ark_script, ark_usage_data = await asyncio.to_thread(ark_recognize, path, row["title"], duration, evidence, route["model"])
        except ArkError as exc:
            ark_error = exc
            turbo_model = ARK_TURBO_MODEL.strip() or ARK_MODEL
            if route["model"] == ARK_LITE_MODEL.strip() and turbo_model and turbo_model != route["model"] and _is_lite_model_unavailable(exc):
                logger.warning(
                    "provider_fallback provider=ark reason=lite_model_unavailable task_id=%s from_model=%s to_model=%s",
                    row["id"],
                    route["model"],
                    turbo_model,
                )
                try:
                    ark_script, ark_usage_data = await asyncio.to_thread(ark_recognize, path, row["title"], duration, evidence, turbo_model)
                    route = {
                        **route,
                        "model": turbo_model,
                        "band": "turbo_fallback",
                        "reasons": [*route["reasons"], "lite_model_unavailable_fallback_turbo"],
                    }
                    ark_error = None
                except ArkError as turbo_exc:
                    ark_error = turbo_exc
            if ark_script is None:
                logger.warning(
                    "provider_failed provider=ark task_id=%s status_code=%s error=%s",
                    row["id"],
                    ark_error.status_code if ark_error else None,
                    safe_error_text(ark_error) if ark_error else "unknown",
                )
                if not ARK_FALLBACK_ON_ERROR or not OPENAI_API_KEY:
                    raise ark_error or exc
        if ark_script is not None and ark_usage_data is not None:
            script = normalize_script(ark_script, row["title"])
            quality = script_quality(script, "ark", evidence)
            approved, blocking = quality_gate(quality)
            quality["deliveryStatus"] = "approved" if approved else "review_required"
            quality["blockingIssues"] = blocking
            quality["evidenceStatus"] = evidence.get("status")
            quality["evidenceSources"] = {source.get("kind"): source.get("status") for source in evidence.get("sources") or [] if isinstance(source, dict)}
            quality["evidenceSummary"] = evidence.get("summary", "")
            quality["modelRoute"] = route["band"]
            quality["routingScore"] = route["score"]
            quality["routingReasons"] = route["reasons"]
            return script, quality, {**ark_usage_data, "provider": "ark", "model": route["model"]}, evidence
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
    quality = script_quality(script, provider, evidence)
    approved, blocking = quality_gate(quality)
    quality["deliveryStatus"] = "approved" if approved else "review_required"
    quality["blockingIssues"] = blocking
    quality["evidenceStatus"] = evidence.get("status")
    quality["evidenceSources"] = {source.get("kind"): source.get("status") for source in evidence.get("sources") or [] if isinstance(source, dict)}
    quality["evidenceSummary"] = evidence.get("summary", "")
    quality["modelRoute"] = route["band"]
    quality["routingScore"] = route["score"]
    quality["routingReasons"] = route["reasons"]
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
        if attempts <= 2 and not _is_non_retryable_provider_error(exc):
            update_task(task_id, status="queued", stage="queued", progress_percent=4, attempts=attempts, error=f"第 {attempts} 次处理失败，正在自动重试：{_task_error_message(exc)}")
            record_task_event(task_id, "retry", "处理失败，任务已自动重新排队", status="queued", stage="queued", progress_percent=4)
            logger.warning("task_retry_auto task_id=%s attempt=%s error=%s", task_id, attempts, safe_error_text(exc))
            return
        message = f"处理失败：{_task_error_message(exc)}"
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
