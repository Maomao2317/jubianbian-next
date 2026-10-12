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
    MAX_DURATION_SECONDS,
    OPENAI_API_KEY,
    OPENAI_TEXT_MODEL,
)
from .auth_store import db, now_iso
from .evidence import collect_evidence, evidence_json, transcript_from_evidence
from .errors import ArkError
from .logging_setup import logger, safe_error_text
from .media import probe_duration
from .providers import ark_fallback_message, ark_prepare_video, ark_recognize, openai_script, openai_transcribe
from .rechecks import has_pending_recheck, restore_recheck_snapshot
from .script import (
    normalize_script,
    quality_gate,
    restore_evidence_boundary_segments,
    restore_transcript_gap_segments,
    restore_short_transcript_segments,
    script_quality,
)
from .storage import cleanup_materialized, is_object_uri, materialize_media
from .task_store import batch_character_context, mark_task_stage, reconcile_batch_character_continuity, record_task_event, task_row, update_task


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
        # Several subtitle/name samples are useful evidence, not proof that
        # the episode needs the slower model.  Keep the signal for metrics but
        # let audio fidelity, duration and the post-generation quality gate
        # decide whether a Turbo pass is actually necessary.
        reasons.append("ocr>=3items")
    audio_available = bool(audio and audio.get("status") == "available")
    ocr_available = bool(ocr and ocr.get("status") == "available")
    if not audio_available:
        # Missing independent audio evidence is a real fidelity risk because
        # there is no second source against which to check short lines.
        score += 2
        reasons.append("audio_unavailable")
    if not ocr_available:
        # OCR is an optional subtitle/name aid on the current image. The
        # container can legitimately have no usable OCR frames while local
        # ASR is healthy; treating that as two extra complexity points sent
        # every short episode to the much slower Turbo model. Keep the fact
        # in the route metadata, but do not penalize a task that has audio
        # evidence. When both independent sources are absent, add one small
        # penalty so the conservative path still wins.
        reasons.append("ocr_unavailable_optional" if audio_available else "ocr_unavailable")
        if not audio_available:
            score += 1
    if (not audio or audio.get("status") != "available") and (not ocr or ocr.get("status") != "available"):
        reasons.append("independent_audio_and_ocr_unavailable")
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


def _evaluate_quality(
    script: dict[str, Any],
    provider: str,
    evidence: dict[str, Any],
    route: dict[str, Any],
) -> tuple[dict[str, Any], bool, list[dict[str, Any]]]:
    boundary_recovered = restore_evidence_boundary_segments(script, evidence)
    gap_recovered = restore_transcript_gap_segments(script, evidence)
    short_recovered = restore_short_transcript_segments(script, evidence)
    recovered = {
        "segments": boundary_recovered["segments"] + gap_recovered["segments"] + short_recovered["segments"],
        "blocks": boundary_recovered["blocks"] + gap_recovered["blocks"] + short_recovered["blocks"],
        "boundarySegments": boundary_recovered["segments"],
        "interiorGapSegments": gap_recovered["segments"],
        "shortSegments": short_recovered["segments"],
    }
    quality = script_quality(script, provider, evidence)
    quality["shortDialogueRecovery"] = recovered
    approved, blocking = quality_gate(quality)
    quality["deliveryStatus"] = "approved" if approved else "review_required"
    quality["blockingIssues"] = blocking
    quality["evidenceStatus"] = evidence.get("status")
    quality["evidenceSources"] = {
        source.get("kind"): source.get("status")
        for source in evidence.get("sources") or []
        if isinstance(source, dict)
    }
    quality["evidenceSummary"] = evidence.get("summary", "")
    quality["modelRoute"] = route["band"]
    quality["routingScore"] = route["score"]
    quality["routingReasons"] = route["reasons"]
    return quality, approved, blocking


def _merge_usage(*records: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        values = [record.get(key) for record in records if isinstance(record.get(key), (int, float))]
        result[key] = int(sum(values)) if values else None
    result["api_cost_rmb"] = round(
        sum(float(record.get("api_cost_rmb") or 0) for record in records),
        6,
    )
    return result


def _issue_identity(issue: dict[str, Any]) -> str:
    return "|".join(
        str(issue.get(key) or "")
        for key in ("severity", "tag", "scene", "block", "description")
    )


def _review_regression_reason(initial: dict[str, Any], reviewed: dict[str, Any]) -> str | None:
    """Do not replace a draft with a review that loses independent evidence."""
    if len(quality_gate(reviewed)[1]) > len(quality_gate(initial)[1]):
        return "more_blocking_issues"
    for key in ("transcriptCoverage", "dialogueEvidencePrecision"):
        before, after = initial.get(key), reviewed.get(key)
        if isinstance(before, (int, float)) and isinstance(after, (int, float)) and after < before:
            return f"lower_{key}"
    return None


def _automated_review_context(
    script: dict[str, Any],
    blocking: list[dict[str, Any]],
) -> dict[str, Any]:
    ranges = []
    for issue in blocking:
        start = issue.get("startSec")
        end = issue.get("endSec")
        if not isinstance(start, (int, float)) and not isinstance(end, (int, float)):
            continue
        if not isinstance(start, (int, float)):
            start = max(0.0, end - 3.0)
        if not isinstance(end, (int, float)):
            end = start + 3.0
        if not 0 <= start <= end:
            continue
        ranges.append({
            "startSec": start,
            "endSec": end,
            "tag": issue.get("tag"),
            "severity": issue.get("severity"),
        })
    # Normalized results contain several copies of each line and display-only
    # defaults. Keep the verbatim line and fidelity metadata once so review
    # spends its context on the video/issues rather than repeated JSON.
    block_keys = {
        "type", "text", "speaker", "visibleSpeaker", "mouthMoving", "speakerEvidence",
        "voKind", "confidence", "uncertain", "inferred", "startSec", "endSec",
        "object", "result", "performance", "emotionType", "emotionEvidence",
        "emotionIntensity", "emotionTrigger", "emotionChange", "emotionTarget",
        "speechTone", "category", "source", "importance", "screenType", "transitionType",
    }
    draft = {
        "title": script.get("title"),
        "characters": script.get("characters") or [],
        "characterProfiles": script.get("characterProfiles") or [],
        "scenes": [],
    }
    for scene in script.get("scenes") or []:
        compact_scene = {key: scene[key] for key in (
            "heading", "location", "timeOfDay", "interiorExterior", "segmentType", "characters",
        ) if key in scene}
        compact_scene["blocks"] = []
        for block in scene.get("blocks") or []:
            compact_block = {key: value for key, value in block.items() if key in block_keys and value not in (None, "")}
            if block.get("type") in {"dialogue", "vo"}:
                compact_block["text"] = block.get("sourceText") or block.get("rawText") or block.get("text") or ""
            compact_scene["blocks"].append(compact_block)
        draft["scenes"].append(compact_scene)
    return {
        "scope": "issue_patches" if ranges else "full_video_with_issue_ranges",
        "issues": blocking[:40],
        "ranges": ranges[:40],
        "draft": draft,
        "_sourceScript": script,
    }


def _needs_automated_content_review(quality: dict[str, Any]) -> bool:
    """Review source-fidelity risks, not every advisory screenplay heuristic."""
    critical_tags = {
        "台词归属错",
        "漏台词",
        "OS漏标",
        "OS/VO混淆",
        "音频对白不一致",
        "音频对白需核对",
        "疑似漏台词片段",
        "正文混入校对备注",
        "事实臆造",
    }
    return any(
        isinstance(issue, dict)
        and (
            str(issue.get("severity") or "").upper() == "P0"
            or str(issue.get("tag") or "") in critical_tags
        )
        for issue in (quality.get("issues") or [])
    )


def _automated_review_issues(quality: dict[str, Any]) -> list[dict[str, Any]]:
    critical_tags = {
        "台词归属错", "漏台词", "OS漏标", "OS/VO混淆", "音频对白不一致",
        "音频对白需核对", "疑似漏台词片段", "正文混入校对备注", "事实臆造",
    }
    return [
        issue
        for issue in (quality.get("issues") or [])
        if isinstance(issue, dict)
        and (str(issue.get("severity") or "").upper() == "P0" or str(issue.get("tag") or "") in critical_tags)
    ]


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
            "modelnotopen",
            "model_not_open",
            "not active",
            "未激活",
        )
    )


def _task_error_message(error: BaseException) -> str:
    text = str(error).casefold()
    if _is_non_retryable_provider_error(error):
        return "当前 Lite 模型未启用，已停止重复重试；请启用该模型或改用 Turbo 后再识别。"
    if any(marker in text for marker in ("timed out", "timeout", "超时")):
        return "方舟识别暂时超时，自动恢复与重试均未成功；请稍后重新识别。"
    return safe_error_text(error)


# Provider selection and fallback policy live here; HTTP routes only enqueue work.
async def run_recognizer(row: sqlite3.Row) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Collect evidence, then use Ark with an explicit OpenAI fallback."""
    path, materialized_dir = await asyncio.to_thread(materialize_media, row["stored_path"], row["id"])
    duration = float(row["duration_sec"] or 0)
    try:
        probed = await asyncio.to_thread(probe_duration, path)
        if probed:
            if probed > MAX_DURATION_SECONDS:
                raise ArkError("视频实际时长超过系统允许的单集上限")
            duration = probed
            update_task(row["id"], duration_sec=duration)

        # Video upload/preprocessing does not depend on ASR/OCR.  Starting it
        # alongside evidence collection hides the network latency, and the
        # resulting file_id is reused by a possible Turbo review.
        evidence_task = asyncio.create_task(asyncio.to_thread(collect_evidence, path, row["title"], duration))
        prepare_task = (
            asyncio.create_task(asyncio.to_thread(
                ark_prepare_video, path, duration,
                **({"source_uri": row["stored_path"]} if is_object_uri(row["stored_path"]) else {}),
            ))
            if ARK_API_KEYS
            else None
        )
        evidence = await evidence_task
        cast_context = await asyncio.to_thread(batch_character_context, row)
        if cast_context:
            evidence = {**evidence, "batchCharacterContext": cast_context}
        prepared_video: dict[str, str] | None = None
        prepare_error: ArkError | None = None
        if prepare_task is not None:
            try:
                prepared_video = await prepare_task
            except ArkError as exc:
                prepare_error = exc
        route = select_ark_route(duration, evidence)
        return await _run_recognizer_with_media(
            row,
            path,
            duration,
            evidence,
            route,
            prepared_video,
            prepare_error,
        )
    finally:
        await asyncio.to_thread(cleanup_materialized, materialized_dir)


async def _run_recognizer_with_media(
    row: sqlite3.Row,
    path: Path,
    duration: float,
    evidence: dict[str, Any],
    route: dict[str, Any],
    prepared_video: dict[str, str] | None,
    prepare_error: ArkError | None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    ark_error: ArkError | None = prepare_error
    if ARK_API_KEYS and prepared_video is not None:
        ark_script: dict[str, Any] | None = None
        ark_usage_data: dict[str, Any] | None = None
        try:
            ark_script, ark_usage_data = await asyncio.to_thread(
                ark_recognize,
                path,
                row["title"],
                duration,
                evidence,
                route["model"],
                prepared_video=prepared_video,
            )
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
                    ark_script, ark_usage_data = await asyncio.to_thread(
                        ark_recognize,
                        path,
                        row["title"],
                        duration,
                        evidence,
                        turbo_model,
                        prepared_video=prepared_video,
                    )
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
            quality, _approved, blocking = _evaluate_quality(script, "ark", evidence, route)
            initial_model = route["model"]
            final_model = initial_model
            final_usage = dict(ark_usage_data)
            review_metadata: dict[str, Any] = {
                "initialModel": initial_model,
                "reviewModel": None,
                "attemptCount": 1,
                "triggeringIssues": blocking,
                "resolvedIssues": [],
                "unresolvedIssues": blocking,
                "reviewScope": None,
            }

            turbo_model = ARK_TURBO_MODEL.strip() or ARK_MODEL
            may_auto_review = (
                _needs_automated_content_review(quality)
                and route.get("mode") not in {"lite", "lite_only"}
                and bool(turbo_model)
            )
            if may_auto_review:
                review_issues = _automated_review_issues(quality)
                context = _automated_review_context(script, review_issues)
                review_metadata.update({
                    "reviewModel": turbo_model,
                    "attemptCount": 2,
                    "reviewScope": context["scope"],
                    "triggeringIssues": review_issues,
                })
                logger.info(
                    "provider_review provider=ark task_id=%s from_model=%s to_model=%s issues=%s",
                    row["id"],
                    initial_model,
                    turbo_model,
                    len(review_issues),
                )
                try:
                    reviewed_script, reviewed_usage = await asyncio.to_thread(
                        ark_recognize,
                        path,
                        row["title"],
                        duration,
                        evidence,
                        turbo_model,
                        review_context=context,
                        prepared_video=prepared_video,
                    )
                    review_route = {
                        **route,
                        "model": turbo_model,
                        "band": "turbo_review",
                        "reasons": [*route["reasons"], "lite_quality_gate_failed_turbo_review"],
                    }
                    reviewed_script = normalize_script(reviewed_script, row["title"])
                    reviewed_quality, _reviewed_approved, _reviewed_blocking = _evaluate_quality(
                        reviewed_script,
                        "ark",
                        evidence,
                        review_route,
                    )
                    reviewed_content_issues = _automated_review_issues(reviewed_quality)
                    final_usage = _merge_usage(ark_usage_data, reviewed_usage)
                    regression = _review_regression_reason(quality, reviewed_quality)
                    review_metadata["accepted"] = regression is None
                    if regression:
                        review_metadata["retainedDraftReason"] = regression
                        review_metadata["unresolvedIssues"] = review_issues
                        logger.warning("provider_review_retained_draft task_id=%s reason=%s", row["id"], regression)
                    else:
                        unresolved_ids = {_issue_identity(issue) for issue in reviewed_content_issues}
                        review_metadata["resolvedIssues"] = [
                            issue for issue in review_issues if _issue_identity(issue) not in unresolved_ids
                        ]
                        review_metadata["unresolvedIssues"] = reviewed_content_issues
                        script = reviewed_script
                        quality = reviewed_quality
                        final_model = turbo_model
                except ArkError as review_exc:
                    review_metadata["reviewError"] = safe_error_text(review_exc)
                    quality["warning"] = (
                        "Turbo 自动复核失败，已保留 Lite 草稿并转管理员复核。"
                        if not quality_gate(quality)[0]
                        else "Turbo 自动复核失败，已保留通过 P0 门禁的 Lite 结果并记录提示。"
                    )
                    logger.warning(
                        "provider_review_failed provider=ark task_id=%s error=%s",
                        row["id"],
                        safe_error_text(review_exc),
                    )

            quality["automatedReview"] = review_metadata
            return script, quality, {**final_usage, "provider": "ark", "model": final_model}, evidence
    elif ARK_API_KEYS and prepare_error is not None:
        logger.warning(
            "provider_failed provider=ark operation=prepare task_id=%s error=%s",
            row["id"],
            safe_error_text(prepare_error),
        )
        if not ARK_FALLBACK_ON_ERROR or not OPENAI_API_KEY:
            raise prepare_error
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
    quality, _approved, _blocking = _evaluate_quality(script, provider, evidence, route)
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
        # screenplay. P0 source-fidelity findings keep the generated script
        # available for inspection but put the task behind the admin review
        # gate. P1 editing hints stay visible without blocking delivery.
        approved, blocking = quality_gate(quality)
        quality["deliveryStatus"] = "approved" if approved else "review_required"
        quality["blockingIssues"] = blocking
        completed_recheck = has_pending_recheck(task_id)
        if completed_recheck:
            # Even a clean automatic gate cannot replace a version that was
            # already delivered. An administrator must compare the candidate
            # with the preserved snapshot and explicitly accept it.
            quality["deliveryStatus"] = "review_required"
            quality["recheck"] = {
                "previousVersionPreserved": True,
                "adminApprovalRequired": True,
            }
        can_deliver = approved and not completed_recheck
        final_status = "done" if can_deliver else "review"
        final_stage = "done" if can_deliver else "review"
        final_message = "识别完成" if can_deliver else "识别完成，等待管理员复核"
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
        if row["batch_id"]:
            try:
                continuity_changes = reconcile_batch_character_continuity(row["batch_id"])
                if continuity_changes:
                    logger.info(
                        "batch_character_continuity batch_id=%s task_id=%s changes=%s",
                        row["batch_id"],
                        task_id,
                        continuity_changes,
                    )
            except Exception as continuity_error:  # pragma: no cover - defensive persistence boundary
                # A naming reconciliation must never turn an otherwise valid
                # screenplay into a failed task. The next episode completion
                # will retry the same deterministic pass.
                logger.warning(
                    "batch_character_continuity_failed batch_id=%s task_id=%s error=%s",
                    row["batch_id"],
                    task_id,
                    safe_error_text(continuity_error),
                )
        record_task_event(
            task_id,
            "completed" if can_deliver else "review_required",
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
        if has_pending_recheck(task_id):
            # A failed recheck must not replace or refund the already
            # delivered task. Restore the preserved version automatically.
            restored = restore_recheck_snapshot(
                task_id,
                resolution="failed_restored",
                allowed_statuses=("queued", "running", "review", "failed"),
            )
            if restored:
                record_task_event(
                    task_id,
                    "admin_recheck_failed_restored",
                    "复核重试失败，已自动恢复上次完成版本",
                    status="done",
                    stage="done",
                    progress_percent=100,
                    duration_ms=(time.perf_counter() - started) * 1000,
                )
                logger.error(
                    "task_recheck_failed_restored task_id=%s duration_ms=%.1f error=%s",
                    task_id,
                    (time.perf_counter() - started) * 1000,
                    safe_error_text(exc),
                )
                return
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
