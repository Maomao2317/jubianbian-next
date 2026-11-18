"""Create a language-specific delivery copy without changing the QA source."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from copy import deepcopy
import re
from threading import BoundedSemaphore
import time
from typing import Any, Callable

from .config import TRANSLATION_MAX_IN_FLIGHT, TRANSLATION_MAX_IN_FLIGHT_PER_TASK
from .errors import ArkError
from .providers import ark_translate_items


TRANSLATION_MAX_ITEMS = 64
TRANSLATION_MAX_SOURCE_CHARS = 5000
_translation_semaphore = BoundedSemaphore(TRANSLATION_MAX_IN_FLIGHT)
_NAME_PLACEHOLDER_TEMPLATE = "[[JBB_NAME_{index:03d}]]"
_GENERIC_CHARACTER_LABELS = {
    "旁白", "未知说话人", "未知男声", "未知女声", "人物", "角色", "OS", "VO", "内心独白",
    "Narrator", "Unknown speaker", "Character", "Inner monologue",
}


def _protect_character_names(
    text: str, protected_names: list[str],
) -> tuple[str, dict[str, tuple[str, int]]]:
    """Replace cast names with stable, non-translatable placeholders.

    The provider still receives the cast glossary as context, but it never has
    to decide whether a name inside screenplay prose should be translated.
    Longer names are replaced first so overlapping aliases remain stable.
    """
    names = sorted(
        {name for name in protected_names if name and name in text},
        key=lambda value: (-len(value), value),
    )
    if not names:
        return text, {}
    placeholders = {
        name: _NAME_PLACEHOLDER_TEMPLATE.format(index=index)
        for index, name in enumerate(names)
    }
    counts = {name: 0 for name in names}

    def replace(match: re.Match[str]) -> str:
        name = match.group(0)
        counts[name] += 1
        return placeholders[name]

    # A single pass is important: a Latin-character name such as "JBB"
    # must never match inside a placeholder produced for another character.
    pattern = re.compile("|".join(re.escape(name) for name in names))
    protected = pattern.sub(replace, text)
    replacements = {
        placeholders[name]: (name, counts[name])
        for name in names
        if counts[name]
    }
    return protected, replacements


def _restore_character_names(
    translated: str, replacements: dict[str, tuple[str, int]],
) -> str:
    restored = translated
    for placeholder, (name, expected_count) in replacements.items():
        label = re.escape(placeholder[2:-2])
        pattern = re.compile(
            rf"(?:\[\[|［［)\s*{label}\s*(?:\]\]|］］)",
            flags=re.IGNORECASE,
        )
        if len(pattern.findall(restored)) != expected_count:
            raise ArkError(
                "翻译改变了人物姓名占位符，未开放交付",
                provider_code="translation_name_changed",
            )
        restored = pattern.sub(lambda _match: name, restored)
    return restored


def _translate_chunk_with_retry(
    translate: Callable[..., tuple[list[dict[str, str]], dict[str, Any]]],
    items: list[dict[str, str]],
    source_language: str,
    target_language: str,
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """Retry an oversized translation response with smaller request batches."""
    try:
        with _translation_semaphore:
            returned, usage = translate(items, source_language, target_language)
        return returned, [usage if isinstance(usage, dict) else {}]
    except ArkError as exc:
        if exc.provider_code != "output_limit" or len(items) <= 1:
            raise
        midpoint = max(1, len(items) // 2)
        protected_names = next(
            (item.get("protectedNames") for item in items if item.get("protectedNames")), ""
        )
        left_items = [dict(item) for item in items[:midpoint]]
        right_items = [dict(item) for item in items[midpoint:]]
        if protected_names:
            left_items[0].setdefault("protectedNames", protected_names)
            right_items[0].setdefault("protectedNames", protected_names)
        left, left_usage = _translate_chunk_with_retry(
            translate, left_items, source_language, target_language,
        )
        right, right_usage = _translate_chunk_with_retry(
            translate, right_items, source_language, target_language,
        )
        return [*left, *right], [*left_usage, *right_usage]


def _character_names(script: dict[str, Any]) -> list[str]:
    values: list[Any] = list(script.get("characters") or [])
    values.extend(script.get("characterProfiles") or [])
    for scene in script.get("scenes") or []:
        if not isinstance(scene, dict):
            continue
        values.extend(scene.get("characters") or [])
        values.extend(
            block.get("speaker")
            for block in scene.get("blocks") or []
            if isinstance(block, dict) and block.get("speaker")
        )
    names = []
    for value in values:
        name = value.get("name") if isinstance(value, dict) else value
        if (isinstance(name, str) and name.strip()
                and name.strip() not in _GENERIC_CHARACTER_LABELS):
            names.append(name.strip())
    return list(dict.fromkeys(names))


def _text_fields(
    script: dict[str, Any],
) -> list[tuple[dict[str, Any], str, str, dict[str, str]]]:
    fields: list[tuple[dict[str, Any], str, str, dict[str, str]]] = []
    protected_names = " | ".join(_character_names(script))
    if isinstance(script.get("title"), str) and script["title"].strip():
        fields.append((script, "title", script["title"], {
            "kind": "screenplay_title", "field": "title", "protectedNames": protected_names,
        }))
    for scene in script.get("scenes") or []:
        if not isinstance(scene, dict):
            continue
        heading = str(scene.get("heading") or "").strip()
        for key in ("heading",):
            if isinstance(scene.get(key), str) and scene[key].strip():
                fields.append((scene, key, scene[key], {
                    "kind": "scene_heading", "field": key, "scene": heading,
                    "protectedNames": protected_names,
                }))
        for block in scene.get("blocks") or []:
            if not isinstance(block, dict):
                continue
            for key in ("text", "performance", "emotion"):
                if isinstance(block.get(key), str) and block[key].strip():
                    context = {
                        "kind": str(block.get("type") or "action"),
                        "field": key,
                        "scene": heading,
                        "protectedNames": protected_names,
                    }
                    if block.get("speaker"):
                        context["speaker"] = str(block["speaker"])
                    fields.append((block, key, block[key], context))
    return fields


def _translation_chunks(
    fields: list[tuple[dict[str, Any], str, str, dict[str, str]]],
    protected_names: list[str],
) -> tuple[list[list[dict[str, str]]], dict[str, dict[str, tuple[str, int]]]]:
    chunks: list[list[dict[str, str]]] = []
    name_replacements: dict[str, dict[str, tuple[str, int]]] = {}
    current: list[dict[str, str]] = []
    source_chars = 0
    for index, (_owner, _key, value, context) in enumerate(fields):
        item_id = f"t{index}"
        protected_value, replacements = _protect_character_names(value, protected_names)
        name_replacements[item_id] = replacements
        if current and (
            len(current) >= TRANSLATION_MAX_ITEMS
            or source_chars + len(protected_value) > TRANSLATION_MAX_SOURCE_CHARS
        ):
            chunks.append(current)
            current = []
            source_chars = 0
        item_context = {
            key: context_value
            for key, context_value in context.items()
            if key != "protectedNames" and context_value
        }
        # The cast glossary applies to the whole chunk. Send it once instead
        # of repeating a potentially long roster for every screenplay line.
        if not current and context.get("protectedNames"):
            item_context["protectedNames"] = context["protectedNames"]
        current.append({"id": item_id, "text": protected_value, **item_context})
        source_chars += len(protected_value)
    if current:
        chunks.append(current)
    return chunks, name_replacements


def _validate_translation(
    original: str, translated: str, context: dict[str, str], protected_names: list[str],
) -> None:
    _validate_translation_structure(original, translated, context)
    for name in protected_names:
        if name in original and name not in translated:
            raise ArkError("翻译改变了人物姓名，未开放交付", provider_code="translation_name_changed")


def _validate_translation_structure(
    original: str, translated: str, context: dict[str, str],
) -> None:
    if original.count("\n") != translated.count("\n"):
        raise ArkError("翻译改变了原文行数，未开放交付", provider_code="translation_structure_changed")
    if context.get("kind") == "scene_heading":
        original_number = re.match(r"^\s*(\d+-\d+)(?=\s|$)", original)
        translated_number = re.match(r"^\s*(\d+-\d+)(?=\s|$)", translated)
        # A heading's episode/scene number is structural, not prose. Require
        # the same number to remain present even if the translator adds a
        # natural-language prefix before it.
        translated_numbers = re.findall(r"(?<!\d)(\d+-\d+)(?!\d)", translated)
        if original_number and original_number.group(1) not in translated_numbers:
            raise ArkError("翻译改变了场次编号，未开放交付", provider_code="translation_structure_changed")


def _usage_summary(
    usages: list[dict[str, Any]], *, chunks: int, fields: int, started: float,
) -> dict[str, Any]:
    return {
        "provider": "ark",
        "model": next((usage.get("model") for usage in usages if usage.get("model")), None),
        "callCount": len(usages),
        "chunkCount": chunks,
        "fieldCount": fields,
        "elapsedMs": round((time.perf_counter() - started) * 1000),
        "input_tokens": sum(int(usage.get("input_tokens") or 0) for usage in usages),
        "output_tokens": sum(int(usage.get("output_tokens") or 0) for usage in usages),
        "total_tokens": sum(int(usage.get("total_tokens") or 0) for usage in usages),
        "api_cost_rmb": round(sum(float(usage.get("api_cost_rmb") or 0) for usage in usages), 6),
    }


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
    started = time.perf_counter()
    delivery = deepcopy(script)
    fields = _text_fields(delivery)
    needs_translation = output_language == "both" or output_language != source_language
    if not needs_translation or not fields:
        return delivery, {"provider": None, "callCount": 0, "chunkCount": 0,
                          "fieldCount": len(fields), "elapsedMs": 0,
                          "input_tokens": 0, "output_tokens": 0,
                          "total_tokens": 0, "api_cost_rmb": 0.0}

    translate = translate_items or ark_translate_items
    target = _target_language(source_language) if output_language == "both" else output_language
    protected_names = _character_names(delivery)
    chunks, name_replacements = _translation_chunks(fields, protected_names)

    # Each chunk is independent.  The real provider path can run a bounded
    # number of requests concurrently, while injected test doubles stay
    # serial so their call ordering remains deterministic.
    if translate_items is None and len(chunks) > 1:
        with ThreadPoolExecutor(
            max_workers=min(TRANSLATION_MAX_IN_FLIGHT_PER_TASK, len(chunks)),
            thread_name_prefix="jbb-translation",
        ) as executor:
            futures = [
                executor.submit(
                    copy_context().run,
                    _translate_chunk_with_retry,
                    translate, items, source_language, target,
                )
                for items in chunks
            ]
            results = [future.result() for future in futures]
    else:
        results = [
            _translate_chunk_with_retry(translate, items, source_language, target)
            for items in chunks
        ]

    translations: dict[str, str] = {}
    usages: list[dict[str, Any]] = []
    for items, (returned, chunk_usages) in zip(chunks, results):
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

    items_by_id = {item["id"]: item for chunk in chunks for item in chunk}
    restored_translations: dict[str, str] = {}
    for offset, (_owner, _key, _original, _context) in enumerate(fields):
        item_id = f"t{offset}"
        try:
            _validate_translation_structure(
                _original, translations[item_id], _context,
            )
            restored_translations[item_id] = _restore_character_names(
                translations[item_id], name_replacements[item_id],
            )
        except ArkError as initial_error:
            if initial_error.provider_code != "translation_name_changed":
                initial_error.usage = _usage_summary(
                    usages, chunks=len(chunks), fields=len(fields), started=started,
                )
                raise
            # A provider can occasionally rewrite even an explicit placeholder.
            # Retry only that field with an even clearer data-level constraint;
            # the second result still has to pass the same deterministic check.
            retry_item = dict(items_by_id[item_id])
            retry_item["placeholderRule"] = (
                "Keep every [[JBB_NAME_###]] placeholder byte-for-byte and in place."
            )
            try:
                returned, retry_usages = _translate_chunk_with_retry(
                    translate, [retry_item], source_language, target,
                )
                if (len(returned) != 1 or not isinstance(returned[0], dict)
                        or returned[0].get("id") != item_id
                        or not isinstance(returned[0].get("text"), str)
                        or not returned[0]["text"].strip()):
                    raise ArkError(
                        "豆包翻译结果缺项或重复，未开放交付",
                        provider_code="translation_incomplete",
                    )
                usages.extend(retry_usages)
                restored_translations[item_id] = _restore_character_names(
                    returned[0]["text"].strip(), name_replacements[item_id],
                )
            except ArkError as exc:
                exc.usage = _usage_summary(
                    usages, chunks=len(chunks), fields=len(fields), started=started,
                )
                raise

    usage_summary = _usage_summary(
        usages, chunks=len(chunks), fields=len(fields), started=started,
    )
    for offset, (owner, key, original, context) in enumerate(fields):
        translated = restored_translations[f"t{offset}"]
        try:
            _validate_translation(original, translated, context, protected_names)
        except ArkError as exc:
            exc.usage = usage_summary
            raise
        owner[key] = f"{original.strip()}\n{translated}" if output_language == "both" else translated

    return delivery, usage_summary
