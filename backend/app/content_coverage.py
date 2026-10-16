"""Time-local completeness checks and additive, bounded video repairs."""

from __future__ import annotations

from copy import deepcopy
import math
import re
from uuid import uuid4
from typing import Any


def valid_time(value: Any) -> bool:
    return type(value) in {int, float} and math.isfinite(value)


def content_gaps(script: dict[str, Any], duration: float, minimum: float = 20) -> list[dict[str, float]]:
    """Find unrepresented time, including the start and interior of an episode.

    Captions and scene-level bounds cannot stand in for dramatic content.
    Silence is not a gap if an observed action represents that interval.
    """
    if not valid_time(duration) or duration <= 0:
        return []
    intervals = sorted(
        (max(0.0, float(block["startSec"])), min(duration, float(block["endSec"])))
        for scene in script.get("scenes") or []
        for block in scene.get("blocks") or []
        if block.get("type") in {"action", "dialogue", "vo"}
        and valid_time(block.get("startSec")) and valid_time(block.get("endSec"))
        and 0 <= block["startSec"] <= block["endSec"] <= duration + 1
    )
    cursor = 0.0
    result = []
    for start, end in intervals + [(duration, duration)]:
        if start - cursor >= minimum:
            result.append({"startSec": round(cursor, 3), "endSec": round(start, 3)})
        cursor = max(cursor, end)
    return result


def repair_windows(gaps: list[dict[str, float]], seconds: float = 50) -> list[dict[str, float]]:
    result = []
    for gap in gaps:
        start, end = gap["startSec"], gap["endSec"]
        # Equal divisions avoid a tiny final request at a chunk boundary.
        count = max(1, math.ceil((end - start) / seconds))
        for index in range(count):
            result.append({"startSec": start + (end - start) * index / count,
                           "endSec": start + (end - start) * (index + 1) / count})
    return result


def slice_evidence(evidence: dict[str, Any], start: float, end: float) -> dict[str, Any]:
    """Rebase text and word times to the physical clip, without touching source evidence."""
    result = {key: deepcopy(value) for key, value in evidence.items()
              if key not in {"sources", "summary", "timingsMs", "durationSec"}}
    result["durationSec"] = end - start
    result["sources"] = []
    for source in evidence.get("sources") or []:
        copied = {key: deepcopy(value) for key, value in source.items() if key != "items"}
        items = []
        for item in source.get("items") or []:
            left = item.get("startSec")
            right = item.get("endSec")
            if right is None:
                right = left
            if not valid_time(left) or not valid_time(right) or right < start or left > end:
                continue
            entry = deepcopy(item)
            entry["startSec"] = max(0, left - start)
            entry["endSec"] = min(end - start, right - start)
            if "words" in entry:
                entry["words"] = [
                    {**word, "startSec": max(0, word["startSec"] - start),
                     "endSec": min(end - start, word["endSec"] - start)}
                    for word in entry["words"]
                    if valid_time(word.get("startSec")) and valid_time(word.get("endSec"))
                    and word["endSec"] >= start and word["startSec"] <= end
                ]
            items.append(entry)
        copied["items"] = items
        result["sources"].append(copied)
    return result


def merge_gap_repair(original: dict[str, Any], clip: dict[str, Any], window: dict[str, float],
                     offset: float, clip_duration: float) -> tuple[dict[str, Any], int]:
    """Only add observed blocks inside the owned gap. Never replace original words.

    Adjacent clips have context overlap; midpoint ownership emits each beat
    once. Original scene numbers are discarded so normalization sorts by time.
    """
    result = deepcopy(original)
    added = 0
    existing = [block for scene in original.get("scenes") or [] for block in scene.get("blocks") or []]
    new_scenes = []
    for scene in clip.get("scenes") or []:
        blocks = []
        for block in scene.get("blocks") or []:
            left, right = block.get("startSec"), block.get("endSec")
            if not valid_time(left) or not valid_time(right) or not 0 <= left <= right <= clip_duration + .5:
                continue
            midpoint = offset + (left + right) / 2
            if not window["startSec"] <= midpoint < window["endSec"]:
                continue
            copied = deepcopy(block)
            copied["startSec"] = round(left + offset, 3)
            copied["endSec"] = round(min(right, clip_duration) + offset, 3)
            if any(
                all(old.get(key) == copied.get(key) for key in ("type", "text", "speaker"))
                and valid_time(old.get("startSec")) and valid_time(old.get("endSec"))
                and min(old["endSec"], copied["endSec"]) > max(old["startSec"], copied["startSec"])
                for old in existing
            ):
                continue
            blocks.append(copied)
            existing.append(copied)
        if blocks:
            copied_scene = deepcopy(scene)
            copied_scene["blocks"] = blocks
            new_scenes.append(copied_scene)
            added += len(blocks)
    result["scenes"] = [
        {**deepcopy(scene), "blocks": [deepcopy(block)]}
        for scene in list(result.get("scenes") or []) + new_scenes
        for block in scene.get("blocks") or []
    ]
    for scene in result["scenes"]:
        # The title supplies the episode number during normalization.
        scene["heading"] = re.sub(r"^\s*\d+-\d+\s*", "", str(scene.get("heading") or ""))
        scene.pop("startSec", None)
        scene.pop("endSec", None)
    result["characters"] = list(dict.fromkeys(list(result.get("characters") or []) + list(clip.get("characters") or [])))
    profiles = result.setdefault("characterProfiles", [])
    names = {profile.get("name") for profile in profiles}
    for profile in clip.get("characterProfiles") or []:
        if profile.get("name") not in names:
            profiles.append(deepcopy(profile))
            names.add(profile.get("name"))
    return result, added


def normalize_additive_repair(original: dict[str, Any], candidate: dict[str, Any], title: str) -> dict[str, Any]:
    """Normalize scene structure while keeping existing blocks verbatim.

    New character aliases and appearance profiles can otherwise rewrite old
    action prose during normalization. Temporary transitions protect the old
    blocks from all prose/speaker transforms; none reach QA or a model call.
    """
    from .script import normalize_script

    remaining = [(block, scene) for scene in original.get("scenes") or []
                 for block in scene.get("blocks") or []]
    prepared = deepcopy(candidate)
    protected = {}
    prefix = "contentRepair" + uuid4().hex
    for scene in prepared.get("scenes") or []:
        for index, block in enumerate(scene.get("blocks") or []):
            match = next((i for i, (old, _) in enumerate(remaining) if old == block), None)
            if match is None:
                continue
            old, old_scene = remaining.pop(match)
            marker = prefix + str(len(protected))
            protected[marker] = (deepcopy(old), list(old_scene.get("characters") or []))
            scene["blocks"][index] = {"type": "transition", "text": marker,
                                      **{key: block[key] for key in ("startSec", "endSec") if key in block}}
    if remaining:
        raise ValueError("additive repair lost an original block before normalization")
    result = normalize_script(prepared, title)
    for scene in result.get("scenes") or []:
        for index, block in enumerate(scene.get("blocks") or []):
            saved = protected.pop(block.get("text"), None)
            if saved is None:
                continue
            old, cast = saved
            scene["blocks"][index] = old
            scene["characters"] = list(dict.fromkeys(list(scene.get("characters") or []) + cast))
    if protected:
        raise ValueError("normalization lost a protected original block")
    return result
