"""Create a language-specific delivery copy without changing the QA source."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable

from .errors import ArkError
from .providers import ark_translate_items


def _translate_chunk_with_retry(
    translate: Callable[..., tuple[list[dict[str, str]], dict[str, Any]]],
    items: list[dict[str, str]],
    source_language: str,
    target_language: str,
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """Retry an oversized translation response with smaller request batches."""
    try:
        returned, usage = translate(items, source_language, target_language)
        return returned, [usage if isinstance(usage, dict) else {}]
    except ArkError as exc:
        if exc.provider_code != "output_limit" or len(items) <= 1:
            raise
        midpoint = max(1, len(items) // 2)
        left, left_usage = _translate_chunk_with_retry(
            translate, items[:midpoint], source_language, target_language,
        )
        right, right_usage = _translate_chunk_with_retry(
            translate, items[midpoint:], source_language, target_language,
        )
        return [*left, *right], [*left_usage, *right_usage]


def _text_fields(script: dict[str, Any]) -> list[tuple[dict[str, Any], str, str]]:
    fields: list[tuple[dict[str, Any], str, str]] = []
    if isinstance(script.get("title"), str) and script["title"].strip():
        fields.append((script, "title", script["title"]))
    for scene in script.get("scenes") or []:
        if not isinstance(scene, dict):
            continue
        for key in ("heading",):
            if isinstance(scene.get(key), str) and scene[key].strip():
                fields.append((scene, key, scene[key]))
        for block in scene.get("blocks") or []:
            if not isinstance(block, dict):
                continue
            for key in ("text", "performance", "emotion"):
                if isinstance(block.get(key), str) and block[key].strip():
                    fields.append((block, key, block[key]))
    return fields


def _target_language(source_language: str) -> str:
    return "en" if source_language == "zh" else "zh"


def translate_delivery(
    script: dict[str, Any],
    source_language: str,
    output_language: str,
    *,
    translate_items: Callable[..., tuple[list[dict[str, str]], dict[str, Any]]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the requested delivery and aggregate provider usage.

    Translation is keyed by stable IDs and never changes scene/block counts or
    structural values. A mismatch fails closed instead of showing source text
    as if it were a successful translation.
    """
    if source_language not in {"zh", "en"} or output_language not in {"zh", "en", "both"}:
        raise ArkError("剧本语言选项无效", provider_code="translation_language_invalid")
    if not isinstance(script, dict):
        raise ArkError("原始剧本格式无效，无法生成交付稿", provider_code="translation_source_invalid")
    delivery = deepcopy(script)
    fields = _text_fields(delivery)
    needs_translation = output_language == "both" or output_language != source_language
    if not needs_translation or not fields:
        return delivery, {"provider": None, "callCount": 0, "input_tokens": 0, "output_tokens": 0,
                          "total_tokens": 0, "api_cost_rmb": 0.0}

    translate = translate_items or ark_translate_items
    target = _target_language(source_language) if output_language == "both" else output_language
    translations: dict[str, str] = {}
    usages: list[dict[str, Any]] = []
    for offset in range(0, len(fields), 32):
        chunk = fields[offset:offset + 32]
        items = [{"id": f"t{offset + index}", "text": value}
                 for index, (_owner, _key, value) in enumerate(chunk)]
        returned, chunk_usages = _translate_chunk_with_retry(
            translate, items, source_language, target,
        )
        expected = {item["id"] for item in items}
        received = [item.get("id") for item in returned if isinstance(item, dict)]
        if len(received) != len(expected) or set(received) != expected:
            raise ArkError("豆包翻译结果缺项或重复，未开放交付", provider_code="translation_incomplete")
        for item in returned:
            value = item.get("text")
            if not isinstance(value, str) or not value.strip():
                raise ArkError("豆包翻译结果为空，未开放交付", provider_code="translation_incomplete")
            translations[item["id"]] = value.strip()
        usages.extend(chunk_usages)

    for offset, (owner, key, original) in enumerate(fields):
        translated = translations[f"t{offset}"]
        owner[key] = f"{original.strip()}\n{translated}" if output_language == "both" else translated

    return delivery, {
        "provider": "ark",
        "model": next((usage.get("model") for usage in usages if usage.get("model")), None),
        "callCount": len(usages),
        "input_tokens": sum(int(usage.get("input_tokens") or 0) for usage in usages),
        "output_tokens": sum(int(usage.get("output_tokens") or 0) for usage in usages),
        "total_tokens": sum(int(usage.get("total_tokens") or 0) for usage in usages),
        "api_cost_rmb": round(sum(float(usage.get("api_cost_rmb") or 0) for usage in usages), 6),
    }
