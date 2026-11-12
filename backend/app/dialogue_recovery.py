"""Bounded second looks at speech evidence, never OCR-to-dialogue guesses."""

from copy import deepcopy
import re

from .content_coverage import valid_time


def _text(value):
    return re.sub(r"[\W_]+", "", str(value or ""), flags=re.UNICODE)


def _span(item):
    if not isinstance(item, dict):
        return None
    start, end = item.get("startSec"), item.get("endSec")
    return (start, end) if valid_time(start) and valid_time(end) and 0 <= start < end else None


def _near(a, b, pad=0):
    return a[0] < b[1] + pad and b[0] < a[1] + pad


def dialogue_evidence_issues(script, evidence):
    """Flag contradictions for listening, not as proof of omitted speech.

    Action coverage cannot establish speech coverage. Conversely, a caption
    alone is not speech: require a sequence of distinct sentence-length
    subtitles before asking for a listening check.
    """
    sources = (evidence or {}).get("sources") or []
    audio = [item for source in sources if source.get("kind") == "audio"
             and source.get("status") == "available" for item in source.get("items") or []
             if _span(item) and _text(item.get("text"))]
    # No independent audio source is already handled by the normal QA gate.
    if not audio:
        return []
    blocks = [(si, bi, block) for si, scene in enumerate(script.get("scenes") or [], 1)
              for bi, block in enumerate(scene.get("blocks") or [], 1) if _span(block)]
    speech = [block for _, _, block in blocks if block.get("type") in {"dialogue", "vo"}]
    issues = []
    for si, bi, block in blocks:
        if block.get("type") not in {"dialogue", "vo"} or not _text(block.get("text")):
            continue
        # Segment time overlap is sufficient to avoid calling a word error a
        # blind spot. Ordinary per-line fidelity QA handles those separately.
        if not any(_near(_span(block), _span(item), .5) for item in audio):
            issues.append({"severity": "P1", "tag": "语音证据盲区", "scene": si, "block": bi,
                           "startSec": block["startSec"], "endSec": block["endSec"],
                           "description": "该对白时段没有独立语音转写，需局部回听核实，不能以动作覆盖视为台词完整。"})
    captions = []
    for si, bi, block in blocks:
        text = _text(block.get("text"))
        if (block.get("type") != "screen_text" or block.get("screenType", "subtitle") != "subtitle"
                or block.get("suppressInOutput") or len(text) < 6
                or block["endSec"] - block["startSec"] > 12):
            continue
        if any(_near(_span(block), _span(line), .35) and text in _text(line.get("text")) for line in speech):
            continue
        captions.append((si, bi, block))
    captions.sort(key=lambda row: row[2]["startSec"])
    for index, (si, bi, block) in enumerate(captions):
        nearby = [row[2] for row in captions[index:index + 3]
                  if row[2]["endSec"] - block["startSec"] <= 12]
        if len({_text(row.get("text")) for row in nearby}) < 3:
            continue
        issues.append({"severity": "P0", "tag": "字幕语音待核实", "scene": si, "block": bi,
                       "startSec": block["startSec"], "endSec": max(row["endSec"] for row in nearby),
                       "description": "连续句子仅保留为字幕，需听原声确认是否为对白或旁白；无声画面文字应标记为其他画面文字，禁止猜测说话人。"})
    return issues


def recovery_windows(issues, duration, *, limit=3, max_seconds=24):
    windows = []
    for issue in sorted(issues, key=lambda row: (row.get("tag") != "字幕语音待核实", row.get("startSec", 0))):
        if not _span(issue):
            continue
        start, end = max(0, issue["startSec"] - 2), min(duration, issue["endSec"] + 2)
        if start >= end:
            continue
        end = min(end, start + max_seconds)
        merge = next((window for window in windows if start <= window["endSec"] + 1
                      and end >= window["startSec"] - 1
                      and max(end, window["endSec"]) - min(start, window["startSec"]) <= max_seconds), None)
        if merge:
            merge.update(startSec=min(start, merge["startSec"]), endSec=max(end, merge["endSec"]))
        elif len(windows) < limit:
            windows.append({"startSec": start, "endSec": end})
    return windows


def recover_audio_blind_spots(path, duration, script, evidence, cache=None):
    """Use the existing private GPU worker for at most 72 seconds per task.

    Successful and failed attempts are checkpointed. No cloud ASR fallback,
    GPU model/config change, or full-episode second scan is permitted here.
    """
    from . import evidence as collector
    from .remote_asr import request_asr

    issues = dialogue_evidence_issues(script, evidence)
    windows = recovery_windows(issues, duration)
    if not windows or not collector.ASR_REMOTE_URL or not path.is_file():
        return evidence
    result = deepcopy(evidence)
    audio = next((source for source in result.get("sources") or [] if source.get("kind") == "audio"), None)
    if audio is None:
        return evidence
    state = cache.load("dialogue_recovery_state_v1") if cache else None
    spent = list((state or {}).get("attempts") or [])
    attempts = []
    for window in windows:
        start, end = window["startSec"], window["endSec"]
        key = f"audio_recovery_v1_{round(start * 1000)}_{round(end * 1000)}"
        record = cache.load(key) if cache else None
        if record is None:
            if window in spent:
                attempts.append({**window, "status": "interrupted", "addedSegments": 0})
                continue
            if len(spent) >= 3 or sum(row["endSec"] - row["startSec"] for row in spent) + end - start > 72.001:
                break
            # Checkpoint admission before network IO, including interrupted
            # calls, so automatic task retries cannot multiply GPU work.
            spent.append(window)
            if cache:
                cache.save("dialogue_recovery_state_v1", {"attempts": spent})
            try:
                source = request_asr(collector._decode_audio_pcm(path, start=start, duration=end - start), end - start)
                record = {"status": "completed", "items": source.get("items") or []}
            except Exception:
                # A failed retry is not evidence. Keep raw source and findings.
                record = {"status": "unavailable", "items": []}
            if cache:
                cache.save(key, record)
        added = 0
        for item in record.get("items") or []:
            span = _span(item)
            if not span or span[1] > end - start + .1 or not _text(item.get("text")):
                continue
            candidate = deepcopy(item)
            candidate.update(startSec=round(start + span[0], 3), endSec=round(start + span[1], 3))
            for word in candidate.get("words") or []:
                word.update(startSec=round(start + word["startSec"], 3), endSec=round(start + word["endSec"], 3))
            overlapping = [old for old in audio.get("items") or [] if _span(old) and _near(_span(old), _span(candidate))]
            if any(_text(candidate["text"]) == _text(old.get("text")) for old in overlapping):
                continue
            # Keep contradictory overlapping ASR only when it independently
            # confirms a timed, sentence-length subtitle from the video.
            if overlapping and not any(
                block.get("type") == "screen_text" and block.get("screenType", "subtitle") == "subtitle"
                and _span(block) and _near(_span(block), _span(candidate))
                and len(_text(block.get("text"))) >= 6
                and _text(block["text"]) in _text(candidate["text"])
                for scene in script.get("scenes") or [] for block in scene.get("blocks") or []
            ):
                continue
            candidate["recoverySource"] = "bounded_gpu_clip"
            audio.setdefault("items", []).append(candidate)
            added += 1
        attempts.append({**window, "status": record["status"], "addedSegments": added})
    audio["items"].sort(key=lambda item: item.get("startSec", 0))
    result["dialogueRecovery"] = {"attempts": attempts, "maxSeconds": 72, "maxCalls": 3,
                                 "admittedCalls": len(spent)}
    result["summary"] = collector.evidence_summary(result)
    if cache:
        cache.save("evidence", result)
    return result
