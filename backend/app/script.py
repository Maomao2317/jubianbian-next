"""Script normalization, quality checks, and export formatting."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from .errors import ArkError

# These rules are deliberately kept next to the transformation functions so
# future prompt/format changes do not get mixed into HTTP or persistence code.
_PUNCT_TRANSLATION = str.maketrans({
    ",": "，",
    ".": "。",
    "!": "！",
    "?": "？",
    ";": "；",
    ":": "：",
    "~": "～",
})
_SENTENCE_END_RE = re.compile(r"[。！？…]$")
_QUESTION_END_RE = re.compile(r"(?:吗|嘛|呢|么|什么|怎么|哪(?:里|个|些)|谁|为何|为什么|是不是|有没有|要不要)$")
_CAMERA_LANGUAGE_RE = re.compile(
    r"(?:镜头(?:切到|切回|给到|对准)?|画面(?:切到|切回|给到)?|特写|近景|中景|全景|远景|正反打|反打|推近|拉远|俯拍|仰拍|摇镜头|跟拍)"
)
_MICRO_DETAIL_RE = re.compile(
    r"(?:指尖|手指|指节|眼里|眼中|眼底|眼眶|血丝|发白|泛白|泛红|通红|睫毛|瞳孔|嘴角|眉头|呼吸|颤抖|发抖|微微|轻轻|细微|眼神一闪)"
)
_MAJOR_ACTION_RE = re.compile(
    r"(?:打|拳|踢|推|拽|抓|抱|吻|亲|拿|递|抢|夺|摔|砸|撞|开门|关门|进门|出门|上车|下车|离开|转身|倒地|站起|冲|追|挡|拦|撕|拔|掏|递给|扔|躲|扑)"
)
_ACTION_VERB_RE = re.compile(
    r"(?:走|跑|站|坐|起身|转身|回头|看|望|盯|抬|低头|点头|摇头|拿|放|取|递|接|抓|拽|扯|推|拉|踢|打|挥|抱|吻|亲|抢|夺|摔|砸|撞|开|关|进|出|上车|下车|离开|冲|追|挡|拦|撕|拔|掏|扔|躲|扑|扶|搀|松开|攥|捂|擦|按|拨|拨打|挂断|签|撕开|撕碎|打开|合上|后退|退后|停下|停住|沉默|愣住|皱眉|呼吸|发抖)"
)
_ABSTRACT_ACTION_RE = re.compile(r"(?:发生争执|表现紧张|表现愤怒|场面混乱|进行对话|展开争吵|气氛紧张|情绪复杂|陷入沉默|两人交流|发生冲突|开始争执)")
_REACTION_ACTION_RE = re.compile(
    r"(?:愣|怔|沉默|停顿|停下|后退|退后|回头|看向|看着|望向|抬头|低头|点头|摇头|皱眉|哭|笑|吸气|呼吸|颤|发抖|捂住|转身|离开|躲开|松开|放下)"
)
_HIGH_IMPACT_DIALOGUE_RE = re.compile(
    r"(?:[！？]|不|别|不要|为什么|怎么|滚|住手|救命|我恨|杀)"
)
_SCENE_HEADING_RE = re.compile(r"^\s*\d+-\d+\s+(?:日|夜|清晨|黄昏|不明)\s+(?:内|外|不明)\s+.+")
_VO_KINDS = {"os", "narration", "memory", "phone", "unknown"}
_VO_KIND_ALIASES = {
    "inner_monologue": "os",
    "inner-monologue": "os",
    "monologue": "os",
    "thought": "os",
    "os": "os",
    "旁白": "narration",
    "画外音": "narration",
    "narration": "narration",
    "voiceover": "narration",
    "voice_over": "narration",
    "回忆": "memory",
    "回忆声": "memory",
    "memory": "memory",
    "电话": "phone",
    "电话声": "phone",
    "phone": "phone",
}
_DIALOGUE_LABEL_RE = re.compile(r"(?:^|[。！？…\n])\s*([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9· ]{0,14})\s*[：:]")


def normalize_text(text: Any, *, sentence: bool = False) -> str:
    """Normalize model punctuation without changing the words it recognized."""
    value = unicodedata.normalize("NFKC", str(text or "")).strip()
    if not value:
        return ""
    value = value.translate(_PUNCT_TRANSLATION)
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"\s*([，。！？；：、])\s*", r"\1", value)
    value = re.sub(r"([，。！？；：、])\1+", r"\1", value)
    value = re.sub(r"^[，。；：、]+", "", value)
    value = re.sub(r"[，；：、]+([。！？])", r"\1", value)
    value = re.sub(r"([。！？…])[，；、：]+", r"\1", value)
    if sentence and value and not _SENTENCE_END_RE.search(value):
        value += "？" if _QUESTION_END_RE.search(value) else "。"
    return value


def normalize_dialogue_text(text: Any) -> str:
    """Normalize only typography around a recovered line.

    Dialogue is treated as immutable content. This helper deliberately does not
    join, summarize, or remove speaker words; it only applies the same Unicode
    punctuation cleanup used by exports and restores a terminal mark.
    """
    return normalize_text(text, sentence=True)


def dialogue_integrity_issues(text: str, speaker: str) -> list[str]:
    """Return deterministic warnings for likely mixed-speaker dialogue.

    We do not guess how to rewrite a suspect line. Keeping the original text and
    surfacing a warning is safer than silently dropping or paraphrasing words.
    """
    labels = [match.group(1).strip() for match in _DIALOGUE_LABEL_RE.finditer(text)]
    unique_labels = list(dict.fromkeys(label for label in labels if label))
    issues: list[str] = []
    if len(unique_labels) > 1:
        issues.append("mixed_speakers")
    if unique_labels and speaker not in {"", "未知说话人"} and unique_labels[0] != speaker:
        issues.append("speaker_label_mismatch")
    if "\n" in text and len([line for line in text.splitlines() if line.strip()]) > 1:
        issues.append("multiple_lines")
    return issues


def split_explicitly_mixed_dialogue(block: dict[str, Any]) -> list[dict[str, Any]]:
    """Split only unambiguous ``甲：...乙：...`` lines.

    The spoken words are copied verbatim into separate blocks. If the pattern is
    not unambiguous, the original block is returned untouched and its warning is
    left for QA instead of risking a destructive guess.
    """
    if block.get("type") != "dialogue":
        return [block]
    text = str(block.get("_dialogueSource") or block.get("text") or "")
    matches = list(_DIALOGUE_LABEL_RE.finditer(text))
    labels = list(dict.fromkeys(match.group(1).strip() for match in matches if match.group(1).strip()))
    if len(labels) < 2 or not matches or matches[0].start() > len(text) - len(text.lstrip()):
        return [block]
    pieces: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        spoken = normalize_dialogue_text(text[match.end():end])
        if not spoken:
            continue
        piece = dict(block)
        piece["speaker"] = match.group(1).strip()
        piece["text"] = spoken
        if block.get("sourceText") is not None:
            piece["sourceText"] = text[match.end():end].strip()
        piece["dialogueIssues"] = ["split_mixed_speakers"]
        piece["uncertain"] = True
        pieces.append(piece)
    return pieces or [block]


def normalize_vo_kind(raw_kind: Any, raw_type: str, speaker: str) -> str:
    value = str(raw_kind or "").strip().casefold()
    if raw_type in {"inner_monologue", "os"} or value in {"inner_monologue", "inner-monologue", "monologue", "thought", "os"}:
        return "os"
    if value in _VO_KIND_ALIASES:
        return _VO_KIND_ALIASES[value]
    if speaker.strip().casefold() in {"os", "内心独白", "内心声"}:
        return "os"
    return "unknown" if value and value not in _VO_KINDS else (value or "narration")


def order_blocks_without_crossing_sentences(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order timestamped blocks only when every block has a timestamp.

    A partial timestamp set is not reliable enough to move an untimed dialogue
    line across a later action. In that case the model's original event order is
    the least destructive representation. Equal timestamps remain stable.
    """
    if not blocks or any(block.get("startSec") is None for block in blocks):
        return blocks
    return [
        block
        for _, block in sorted(
            enumerate(blocks),
            key=lambda pair: (pair[1].get("startSec", 0), pair[0]),
        )
    ]


def clean_action_text(text: Any) -> str:
    value = normalize_text(text)
    if not value:
        return ""
    # Camera language belongs to the editing plan, not the screenplay. Remove
    # it defensively because models occasionally echo it despite the prompt.
    value = _CAMERA_LANGUAGE_RE.sub("", value)
    value = re.sub(r"\s*([，。；、])\s*", r"\1", value)
    value = re.sub(r"^[，。；、]+", "", value)
    value = re.sub(r"[，；、]{2,}", "，", value)
    if is_micro_action(value):
        return ""
    # Remove standalone close-up details even when the model embedded them in
    # a longer action paragraph. Keep a clause when it also contains a causal
    # meaningful plot action (for example “擦去嘴角的血迹” or “挥出第一拳”).
    clauses = [part.strip() for part in re.split(r"[，；]", value) if part.strip()]
    if len(clauses) > 1:
        clauses = [part for part in clauses if not is_micro_action(part)]
        value = "，".join(clauses)
    # Action beats are prose too: close an unfinished sentence so exports do
    # not alternate between complete lines and dangling fragments.
    return normalize_text(value.strip(), sentence=True)


def is_micro_action(text: str) -> bool:
    """Return true for a non-causal close-up detail that should not be a beat."""
    major_candidate = text.replace("拳头", "")
    return bool(_MICRO_DETAIL_RE.search(text)) and not _MAJOR_ACTION_RE.search(major_candidate) and len(text) <= 100


def compact_action_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse adjacent action fragments into story beats, not shot fragments."""
    compacted: list[dict[str, Any]] = []
    for block in blocks:
        if block.get("type") != "action":
            compacted.append(block)
            continue
        text = clean_action_text(block.get("text"))
        if not text:
            continue
        block["text"] = text
        if compacted and compacted[-1].get("type") == "action":
            previous = compacted[-1]
            if previous["text"].endswith(("，", "；", "：", "、", "。", "！", "？", "…")):
                joiner = ""
            else:
                joiner = "，"
            previous["text"] = normalize_text(f"{previous['text']}{joiner}{text}")
            if block.get("endSec") is not None:
                previous_end = previous.get("endSec")
                previous["endSec"] = max(previous_end or 0, block["endSec"])
            # Keep the first meaningful timestamp; the merged end locates the
            # whole beat when the user jumps back to the source video.
            continue
        compacted.append(block)

    # A detail may precede the action it belongs to. Attach it to an adjacent
    # action instead of exposing a standalone “fingertips/eyes” block. If no
    # adjacent story action exists, omit the detail: it is an editing cue, not
    # a screenplay beat.
    for index, block in list(enumerate(compacted)):
        if block is None or block.get("type") != "action" or not is_micro_action(block.get("text", "")):
            continue
        if index + 1 < len(compacted) and compacted[index + 1] is not None and compacted[index + 1].get("type") == "action":
            next_block = compacted[index + 1]
            joiner = "" if block["text"].endswith(("，", "；", "：", "、", "。", "！", "？", "…")) else "，"
            next_block["text"] = normalize_text(f"{block['text']}{joiner}{next_block['text']}")
        elif index > 0 and compacted[index - 1] is not None and compacted[index - 1].get("type") == "action":
            previous = compacted[index - 1]
            joiner = "" if previous["text"].endswith(("，", "；", "：", "、", "。", "！", "？", "…")) else "，"
            previous["text"] = normalize_text(f"{previous['text']}{joiner}{block['text']}")
        compacted[index] = None  # type: ignore[assignment]
    return [block for block in compacted if block is not None]


def repair_action_subjects(blocks: list[dict[str, Any]], characters: list[str]) -> None:
    """Add an omitted action subject when the surrounding scene makes it clear.

    Vision models often describe a continuous beat as ``狠狠拽住衣领`` after
    naming the actor in the preceding clause. That is understandable in a
    shot list but reads as a fragment in a screenplay. We only repair clauses
    that begin with an unmistakable action verb and use the last explicit actor
    (or the next speaker when the action directly introduces a line). We never
    invent a new character name.
    """
    known = sorted({str(name).strip() for name in characters if str(name).strip()}, key=len, reverse=True)
    if not known:
        return

    def explicit_subject(clause: str) -> str | None:
        for name in known:
            if clause.startswith(name):
                return name
        return None

    def next_speaker(index: int) -> str | None:
        for following in blocks[index + 1 :]:
            if following.get("type") not in {"dialogue", "vo"}:
                continue
            speaker = str(following.get("speaker") or "").strip()
            if speaker and speaker in known:
                return speaker
            break
        return None

    # These words commonly introduce a subjectless continuation of the same
    # physical action. The regex deliberately excludes emotional-only clauses.
    continuation = re.compile(
        r"^(?:(?:随后|然后|接着|紧接着|同时|并且|故意|狠狠|重重|直接|一把|缓缓|猛地|再次|再度|继续|立刻|马上|抬手|低头|转身)\s*)?"
        r"(?:第[一二三四五六七八九十百0-9]+拳|打|挥|拽|抓|扯|推|踢|抱|拿|递|抢|夺|摔|砸|撞|开门|关门|进门|出门|上车|下车|离开|转身|倒地|站起|冲|追|挡|拦|撕|拔|掏|扔|躲|扑|看向|望向|走向|扶起|搀扶|松开|攥住|捂住|蹲下|站稳)"
    )
    directional_intro = re.compile(r"^(?:故意|随后|然后|接着|紧接着)[^，；。！？…]{0,20}(?:看向|望向|开口)")

    active_subject: str | None = None
    for index, block in enumerate(blocks):
        if block.get("type") in {"dialogue", "vo"}:
            speaker = str(block.get("speaker") or "").strip()
            if speaker in known:
                active_subject = speaker
            continue
        if block.get("type") != "action":
            continue
        text = str(block.get("text") or "").strip()
        if not text:
            continue
        speaker_hint = next_speaker(index)
        block_subject = explicit_subject(text)
        if block_subject:
            active_subject = block_subject
        clause_subject = block_subject or active_subject
        rebuilt: list[str] = []
        # Keep the punctuation attached to each clause while repairing only
        # the clauses that actually need a grammatical subject.
        clauses = re.findall(r"[^，；。！？…]+[，；。！？…]?", text)
        for raw_clause in clauses:
            clause = raw_clause.strip()
            if not clause:
                continue
            subject = explicit_subject(clause)
            if subject:
                # Do not repeat a character name inside the same beat (e.g.
                # “江川快步冲上前，江川第一拳…”). The first clause already
                # establishes the actor, while the action itself is retained.
                if subject == clause_subject and rebuilt:
                    clause = clause[len(subject) :].lstrip()
                else:
                    clause_subject = subject
            elif continuation.match(clause) or directional_intro.match(clause):
                # A clause that looks toward or introduces the following line
                # usually belongs to that line's speaker; physical follow-up
                # actions stay with the actor named at the start of the beat.
                directional = bool(directional_intro.match(clause) and speaker_hint and re.search(r"(?:看向|望向|开口)", clause))
                # Only the first subjectless clause of a beat needs an explicit
                # name. Later clauses inherit it, which keeps prose natural
                # while still making every action block self-contained.
                subject = speaker_hint if directional else (clause_subject if not rebuilt else None)
                if subject:
                    clause = f"{subject}{clause}"
                    clause_subject = subject
            rebuilt.append(clause)
        if rebuilt:
            block["text"] = normalize_text("".join(rebuilt), sentence=True)
        if block_subject:
            active_subject = block_subject
        elif clause_subject:
            active_subject = clause_subject


def clean_environment(text: Any) -> str:
    value = normalize_text(text)
    if not value:
        return ""
    # Drop generic opening/meta sentences; retain concrete spatial information
    # that follows them in the same model response.
    sentences = re.findall(r"[^。！？…]+[。！？…]?", value)
    kept: list[str] = []
    decorative = re.compile(r"暖光|冷光|照明|灯光|路灯|光斑|背景墙|装饰画|绿植|家常菜|家居氛围|氛围感|对峙气息|气氛|树叶|风声|空气")
    for sentence in sentences:
        sentence = sentence.strip()
        if re.match(r"^(?:视频|画面|镜头)?(?:时长|一开始|开始时|开场|内容是|显示|呈现)", sentence):
            continue
        clauses = [part.strip() for part in re.split(r"[，；]", sentence) if part.strip()]
        clauses = [part for part in clauses if not decorative.search(part)]
        if clauses:
            kept.append("，".join(clauses).rstrip("。！？…") + ("。" if sentence.endswith(("。", "！", "？", "…")) else ""))
    return "".join(kept).strip()


def derive_scene_summary(blocks: list[dict[str, Any]]) -> str:
    """Give legacy scenes a useful continuity line when the model omitted one."""
    events = [
        str(block.get("text") or "").strip()
        for block in blocks
        if block.get("type") in {"action", "dialogue", "vo"} and str(block.get("text") or "").strip()
    ]
    if not events:
        return "本场缺少足够的动作和台词信息，无法确认剧情结果。"

    def excerpt(value: str, limit: int = 46) -> str:
        value = normalize_text(value)
        return value if len(value) <= limit else f"{value[:limit].rstrip('，。！？；：、')}……"

    spoken = [
        str(block.get("text") or "").strip()
        for block in blocks
        if block.get("type") in {"dialogue", "vo"} and str(block.get("text") or "").strip()
    ]
    start, end = (spoken[0], spoken[-1]) if len(spoken) >= 2 else (events[0], events[-1] if len(events) > 1 else events[0])
    if start == end:
        return f"本场围绕“{excerpt(start)}”展开，当前素材在该行动后收束。"
    return f"本场从“{excerpt(start)}”展开，经过对白与行动推进，最终以“{excerpt(end)}”收束。"


def normalize_script(script: Any, title: str) -> dict[str, Any]:
    if not isinstance(script, dict) or not isinstance(script.get("scenes"), list):
        raise ArkError("方舟返回的剧本缺少 scenes 数组")

    alias_map: dict[str, str] = {}

    def names(values: Any) -> list[str]:
        if not isinstance(values, list):
            return []
        result: list[str] = []
        for value in values:
            if isinstance(value, dict):
                value = value.get("name") or value.get("id") or ""
            value = str(value).strip()
            if value:
                result.append(alias_map.get(value.casefold(), value))
        return result

    def number(value: Any) -> float | None:
        if isinstance(value, (int, float)) and value >= 0:
            return round(float(value), 3)
        try:
            parsed = float(str(value).strip())
            return round(parsed, 3) if parsed >= 0 else None
        except (TypeError, ValueError):
            return None

    def truthy(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "是", "推断"}

    def emotion_fields(value: Any) -> dict[str, Any]:
        """Normalize evidence-bound emotion metadata without inventing it."""
        if not isinstance(value, dict):
            return {}
        fields: dict[str, Any] = {}
        aliases = {
            "emotionType": ("emotionType", "emotion_type", "type"),
            "emotionTrigger": ("emotionTrigger", "emotion_trigger", "trigger"),
            "emotionChange": ("emotionChange", "emotion_change", "change"),
            "emotionTarget": ("emotionTarget", "emotion_target", "target"),
            "emotionEvidence": ("emotionEvidence", "emotion_evidence", "evidence"),
            "speechTone": ("speechTone", "speech_tone"),
        }
        for output, keys in aliases.items():
            for key in keys:
                text = normalize_text(value.get(key))
                if text:
                    fields[output] = text
                    break
        raw_intensity = value.get("emotionIntensity", value.get("emotion_intensity"))
        try:
            intensity = int(raw_intensity)
        except (TypeError, ValueError):
            intensity = -1
        if 0 <= intensity <= 5:
            fields["emotionIntensity"] = intensity
        return fields

    def profiles(values: Any) -> list[dict[str, Any]]:
        if isinstance(values, dict):
            values = [{"name": key, "appearance": value} for key, value in values.items()]
        if not isinstance(values, list):
            return []
        result: list[dict[str, Any]] = []
        for index, value in enumerate(values, start=1):
            if not isinstance(value, dict):
                continue
            name = str(value.get("name") or value.get("id") or "").strip()
            if not name:
                continue
            aliases = names(value.get("aliases"))
            appearance = str(value.get("appearance") or "").strip()
            clothing = str(value.get("clothing") or "").strip()
            first_appearance = str(
                value.get("firstAppearance")
                or value.get("introduction")
                or value.get("intro")
                or ""
            ).strip()
            if not first_appearance and (appearance or clothing):
                first_appearance = "；".join(item for item in (appearance, clothing) if item)
            row = {
                "id": str(value.get("id") or f"character_{index:03d}"),
                "name": name,
                "aliases": aliases,
                "appearance": appearance,
                "clothing": clothing,
                "firstAppearance": first_appearance,
            }
            result.append(row)
        return result

    character_profiles = profiles(script.get("characterProfiles"))
    for profile in character_profiles:
        canonical_name = str(profile["name"])
        alias_map[canonical_name.casefold()] = canonical_name
        for alias in profile.get("aliases", []):
            alias_map[str(alias).casefold()] = canonical_name

    def normalize_notes(values: Any, *, structured: bool = False) -> list[Any]:
        """Keep evidence-oriented provider notes without inventing content."""
        if isinstance(values, dict):
            values = [values]
        if not isinstance(values, list):
            values = [values] if values else []
        notes: list[Any] = []
        for index, value in enumerate(values, start=1):
            if structured and isinstance(value, dict):
                summary = normalize_text(value.get("summary") or value.get("text") or value.get("description"))
                if not summary:
                    continue
                note: dict[str, Any] = {
                    "id": str(value.get("id") or f"event_{index:03d}"),
                    "role": str(value.get("role") or value.get("type") or "event").strip().lower(),
                    "summary": summary,
                }
                evidence = normalize_text(value.get("evidence") or value.get("reason"))
                if evidence:
                    note["evidence"] = evidence
                start = number(value.get("startSec"))
                end = number(value.get("endSec"))
                if start is not None:
                    note["startSec"] = start
                if end is not None:
                    note["endSec"] = end
                notes.append(note)
                continue
            text = normalize_text(value if isinstance(value, str) else (value.get("text") if isinstance(value, dict) else value))
            if text:
                notes.append(text)
        return notes

    event_chain = normalize_notes(script.get("eventChain"), structured=True)
    setting_rules = normalize_notes(script.get("settingRules"))

    def scene_heading(raw_scene: dict[str, Any], index: int, location: str) -> str:
        raw_heading = str(raw_scene.get("heading") or "").strip()
        valid = re.match(r"^\s*\d+-\d+\s+(日|夜|清晨|黄昏|不明)\s+(内|外|不明)\s+.+", raw_heading)
        if valid:
            return raw_heading
        time_of_day = str(raw_scene.get("timeOfDay") or "不明").strip()
        if time_of_day not in {"日", "夜", "清晨", "黄昏", "不明"}:
            time_of_day = "不明"
        interior_exterior = str(raw_scene.get("interiorExterior") or "不明").strip()
        if interior_exterior not in {"内", "外", "不明"}:
            interior_exterior = "不明"
        heading_location = location if location != "待补充" else (raw_heading or "未标注地点")
        return f"1-{index} {time_of_day} {interior_exterior} {heading_location}"

    scenes: list[dict[str, Any]] = []
    for index, raw_scene in enumerate(script["scenes"], start=1):
        if not isinstance(raw_scene, dict):
            continue
        raw_segment_type = str(raw_scene.get("segmentType") or "main").strip().lower()
        raw_heading = str(raw_scene.get("heading") or "").strip()
        if raw_segment_type in {"recap", "trailer", "title_card", "credits"} or any(
            marker in raw_heading for marker in ("上集回顾", "精彩预告", "下集预告", "片尾", "演员表", "片头")
        ):
            continue
        blocks: list[dict[str, Any]] = []
        scene_location = str(raw_scene.get("location") or "待补充").strip()
        raw_blocks: list[Any] = []
        if raw_scene.get("sound"):
            raw_blocks.extend(
                {"type": "sound", "text": item if isinstance(item, str) else item.get("text", "")}
                for item in (raw_scene["sound"] if isinstance(raw_scene["sound"], list) else [raw_scene["sound"]])
            )
        if raw_scene.get("emotion"):
            raw_blocks.append({"type": "emotion", "text": raw_scene["emotion"]})
        raw_blocks.extend(raw_scene.get("blocks") or [])
        for raw_block in raw_blocks:
            if not isinstance(raw_block, dict):
                continue
            if raw_block.get("isNonPlot") or str(raw_block.get("segmentType") or "").lower() in {"recap", "trailer", "title_card", "credits"}:
                continue
            text = str(raw_block.get("text") or raw_block.get("description") or "").strip()
            if not text:
                continue
            raw_type = str(raw_block.get("type") or "action").strip().lower()
            block_type = {
                "narration": "vo",
                "voiceover": "vo",
                "voice_over": "vo",
                "inner_monologue": "vo",
                "os": "vo",  # Backward compatibility with older responses.
                "music": "sound",
                "effect": "sound",
                "ambience": "sound",
                "visual": "action",
                "subtitle": "screen_text",
                "caption": "screen_text",
                "system": "screen_text",
                "title_card": "screen_text",
                "flashback": "transition",
                "return": "transition",
            }.get(raw_type, raw_type)
            if block_type not in {"action", "dialogue", "vo", "sound", "emotion", "screen_text", "transition"}:
                block_type = "action"
            if block_type == "action":
                text = clean_action_text(text)
            elif block_type == "dialogue":
                # Never merge or paraphrase dialogue while normalizing it.
                # `sourceText`/`verbatimText` is accepted for providers that
                # return both the raw transcript and display text.
                source_text = raw_block.get("sourceText") or raw_block.get("verbatimText")
                dialogue_source = source_text if source_text else text
                text = normalize_dialogue_text(dialogue_source)
            elif block_type == "vo":
                text = normalize_dialogue_text(text)
            else:
                text = normalize_text(text)
            if not text:
                continue
            block: dict[str, Any] = {"type": block_type, "text": text}
            start = number(raw_block.get("startSec"))
            end = number(raw_block.get("endSec"))
            if start is not None and end is not None and end < start:
                start, end = end, start
            if start is not None:
                block["startSec"] = start
            if end is not None:
                block["endSec"] = end
            if block_type in {"action", "vo"}:
                block["emotion"] = normalize_text(raw_block.get("emotion"))
                block.update(emotion_fields(raw_block))
                performance = normalize_text(
                    raw_block.get("performance")
                    or raw_block.get("acting")
                    or raw_block.get("delivery")
                )
                if performance:
                    block["performance"] = performance
            if block_type == "dialogue":
                speaker = str(raw_block.get("speaker") or "未知说话人").strip()
                block["speaker"] = alias_map.get(speaker.casefold(), speaker)
                block["rawText"] = str(raw_block.get("rawText") or dialogue_source).strip()
                block["finalText"] = text
                if source_text:
                    block["sourceText"] = str(dialogue_source).strip()
                block["confidence"] = str(raw_block.get("confidence") or "medium").strip().lower()
                block["uncertain"] = truthy(raw_block.get("uncertain")) or block["confidence"] == "low"
                issues = dialogue_integrity_issues(str(dialogue_source), block["speaker"])
                if issues:
                    block["dialogueIssues"] = issues
                    block["uncertain"] = True
                    block["_dialogueSource"] = str(dialogue_source)
                # Dialogue emotion is intentionally omitted from the screenplay
                # body. Repeated parentheticals make every line feel like a
                # shot list; a genuinely plot-changing turn can be represented
                # by its own emotion block instead.
                block["emotion"] = ""
                block["emotionImportant"] = False
                block.update(emotion_fields(raw_block))
                performance_parts = [
                    raw_block.get("performance") or raw_block.get("acting") or raw_block.get("delivery"),
                    raw_block.get("speechTone"),
                    raw_block.get("tone"),
                    raw_block.get("volume"),
                    raw_block.get("pause"),
                    raw_block.get("emphasis"),
                ]
                performance = "；".join(
                    value for value in (normalize_text(item) for item in performance_parts) if value
                )
                if performance:
                    block["performance"] = performance
            elif block_type == "vo":
                speaker = str(raw_block.get("speaker") or ("未知说话人" if raw_type in {"os", "inner_monologue"} else "旁白")).strip()
                speaker = alias_map.get(speaker.casefold(), speaker)
                block["speaker"] = speaker
                block["voKind"] = normalize_vo_kind(raw_block.get("voKind") or raw_block.get("voiceType"), raw_type, speaker)
                block["isInnerMonologue"] = block["voKind"] == "os"
                block["inferred"] = truthy(raw_block.get("inferred"))
                block["confidence"] = str(raw_block.get("confidence") or "medium").strip().lower()
                block["uncertain"] = truthy(raw_block.get("uncertain")) or block["confidence"] == "low"
            elif block_type == "sound":
                block["category"] = str(raw_block.get("category") or "effect").strip()
                block["source"] = str(raw_block.get("source") or "heard").strip()
                block["importance"] = str(raw_block.get("importance") or "atmosphere").strip()
                # The extraction script should not mix guessed background music
                # into factual scene content. Creative sound suggestions belong
                # in a later adaptation pass, not in the recovered transcript.
                if block["source"] != "heard" or block["category"] == "music":
                    continue
            elif block_type == "screen_text":
                block["screenType"] = str(raw_block.get("screenType") or raw_type or "subtitle").strip()
            elif block_type == "transition":
                block["transitionType"] = str(raw_block.get("transitionType") or raw_type or "other").strip()
            elif block_type == "emotion":
                block.update(emotion_fields(raw_block))
            if block_type == "action":
                action_object = normalize_text(raw_block.get("object") or raw_block.get("target"))
                action_result = normalize_text(raw_block.get("result") or raw_block.get("impact"))
                if action_object:
                    block["object"] = action_object
                if action_result:
                    block["result"] = action_result
            if block_type == "dialogue" and "mixed_speakers" in block.get("dialogueIssues", []):
                blocks.extend(split_explicitly_mixed_dialogue(block))
            else:
                blocks.append(block)
        for block in blocks:
            if block.get("type") == "dialogue":
                block["speaker"] = alias_map.get(str(block.get("speaker") or "").casefold(), block.get("speaker"))
                block.pop("_dialogueSource", None)
        blocks = order_blocks_without_crossing_sentences(blocks)
        blocks = compact_action_blocks(blocks)
        scene_characters = names(raw_scene.get("characters"))
        fallback_characters = scene_characters + names(script.get("characters")) + [
            str(profile.get("name") or "").strip()
            for profile in character_profiles
            if str(profile.get("name") or "").strip()
        ]
        repair_action_subjects(blocks, list(dict.fromkeys(fallback_characters)))
        for block in blocks:
            speaker = str(block.get("speaker") or "").strip()
            if block.get("type") in {"dialogue", "vo"} and speaker and speaker not in {"旁白", "未知说话人", "OS", "内心独白"}:
                if speaker not in scene_characters:
                    scene_characters.append(speaker)
        summary = normalize_text(
            raw_scene.get("summary")
            or raw_scene.get("transition")
            or raw_scene.get("continuity"),
            sentence=True,
        )
        summary_generated = not bool(summary)
        if not summary:
            summary = derive_scene_summary(blocks)
        goal = normalize_text(raw_scene.get("goal") or raw_scene.get("objective") or raw_scene.get("task"))
        obstacle = normalize_text(raw_scene.get("obstacle") or raw_scene.get("resistance") or raw_scene.get("conflict"))
        result = normalize_text(raw_scene.get("result") or raw_scene.get("outcome") or raw_scene.get("ending"))
        continuity_in = normalize_text(raw_scene.get("continuityIn") or raw_scene.get("previousState") or raw_scene.get("inputState"))
        continuity_out = normalize_text(raw_scene.get("continuityOut") or raw_scene.get("nextState") or raw_scene.get("outputState"))
        hook = normalize_text(raw_scene.get("hook") or raw_scene.get("cliffhanger") or raw_scene.get("nextQuestion"))
        scene: dict[str, Any] = {
            "id": str(raw_scene.get("id") or f"scene_{index:03d}"),
            "heading": scene_heading(raw_scene, index, scene_location),
            "location": scene_location,
            "timeOfDay": str(raw_scene.get("timeOfDay") or "不明"),
            "interiorExterior": str(raw_scene.get("interiorExterior") or "不明"),
            "segmentType": raw_segment_type,
            "characters": scene_characters,
            "environment": clean_environment(raw_scene.get("environment")),
            "summary": summary,
            "summaryGenerated": summary_generated,
            "summarySource": "derived" if summary_generated else "model",
            "blocks": blocks,
        }
        raw_props = raw_scene.get("props") or raw_scene.get("propsState") or raw_scene.get("items")
        if isinstance(raw_props, (list, dict)):
            # Keep only evidence-oriented item state; do not synthesize an
            # item's location or ownership when the provider did not observe it.
            scene["props"] = raw_props if isinstance(raw_props, list) else [raw_props]
        for key, value in (
            ("goal", goal),
            ("obstacle", obstacle),
            ("result", result),
            ("continuityIn", continuity_in),
            ("continuityOut", continuity_out),
            ("hook", hook),
        ):
            if value:
                scene[key] = value
        scene_start = number(raw_scene.get("startSec"))
        scene_end = number(raw_scene.get("endSec"))
        if scene_start is not None:
            scene["startSec"] = scene_start
        if scene_end is not None:
            scene["endSec"] = scene_end
        scenes.append(scene)
    if not scenes:
        raise ArkError("方舟返回的剧本没有可用场景")
    used_characters: list[str] = []
    for scene in scenes:
        for character in scene.get("characters", []):
            if character not in used_characters:
                used_characters.append(character)
        for block in scene.get("blocks", []):
            speaker = block.get("speaker")
            if block.get("type") in {"dialogue", "vo"} and speaker and speaker not in {"未知说话人", "未知男声", "未知女声", "旁白", "OS", "内心独白"} and speaker not in used_characters:
                used_characters.append(speaker)
    used_profiles = [profile for profile in character_profiles if profile["name"] in used_characters]
    profile_names = {str(profile.get("name") or "") for profile in used_profiles}
    for name in used_characters:
        if name not in profile_names:
            used_profiles.append({
                "id": f"character_{len(used_profiles) + 1:03d}",
                "name": name,
                "aliases": [],
                "appearance": "",
                "clothing": "",
                "firstAppearance": "首登外观/服装待核对",
            })
    return {
        "version": str(script.get("version") or "1.0"),
        "title": str(script.get("title") or title),
        "eventChain": event_chain,
        "settingRules": setting_rules,
        "characters": used_characters or names(script.get("characters")),
        "characterProfiles": used_profiles,
        "scenes": scenes,
    }

def script_to_markdown(task: dict[str, Any]) -> str:
    script = task.get("result") or {}
    lines = [f"# {script.get('title') or task['title']}", ""]

    def quote_dialogue(text: Any) -> str:
        value = str(text or "").strip()
        if len(value) >= 2 and value[0] in {'“', '"', '「'} and value[-1] in {'”', '"', '」'}:
            return value
        return f"“{value}”"

    def parenthetical(value: Any) -> str:
        text = normalize_text(value)
        if not text:
            return ""
        text = text.strip("（）() ")
        return f"（{text}）" if text else ""

    for scene in script.get("scenes", []):
        lines.extend([scene.get("heading", "未标注场景"), ""])
        cast = [str(name).strip() for name in (scene.get("characters") or []) if str(name).strip()]
        if cast:
            lines.append(f"出场人物：{'、'.join(dict.fromkeys(cast))}")
            lines.append("")
        for block in scene.get("blocks", []):
            block_type = block.get("type")
            if block_type == "dialogue":
                warning = "【需核对】" if block.get("uncertain") else ""
                acting = parenthetical(block.get("performance"))
                lines.append(f"{block.get('speaker', '人物')}{warning}{acting}：{quote_dialogue(block.get('text', ''))}")
            elif block_type in {"vo", "os"}:
                inferred = "（推断）" if block.get("inferred") else ""
                speaker = block.get("speaker") or "旁白"
                vo_kind = block.get("voKind")
                if vo_kind == "os" or block.get("isInnerMonologue") or block_type == "os":
                    label = "OS" if speaker in {"旁白", "未知说话人", "OS", "内心独白"} else f"{speaker} OS"
                else:
                    label = "VO" if speaker in {"旁白", "未知说话人", "OS"} else f"{speaker} VO"
                lines.append(f"{label}{inferred}：{quote_dialogue(block.get('text', ''))}")
            elif block_type == "sound":
                category = "环境音" if block.get("category") == "ambience" else "音效"
                lines.append(f"【{category}：{block.get('text', '')}】")
            elif block_type == "emotion":
                lines.append(f"【情绪：{block.get('text', '')}】")
            elif block_type == "screen_text":
                lines.append(f"【字幕：{block.get('text', '')}】")
            elif block_type == "transition":
                transition_type = block.get("transitionType") or "转场"
                lines.append(f"【{transition_type}：{block.get('text', '')}】")
            else:
                lines.append(f"▲ {block.get('text', '')}")
        lines.append("")
    return "\n".join(lines)


def script_quality(script: dict[str, Any], provider: str) -> dict[str, Any]:
    """Expose deterministic P0/P1 acceptance signals with reviewable labels."""
    scenes = [scene for scene in (script.get("scenes") or []) if isinstance(scene, dict)]
    dialogue = [
        block
        for scene in scenes
        for block in (scene.get("blocks") or [])
        if isinstance(block, dict) and block.get("type") in {"dialogue", "vo"}
    ]
    action_blocks = [
        block
        for scene in scenes
        for block in (scene.get("blocks") or [])
        if isinstance(block, dict) and block.get("type") == "action"
    ]
    punctuation_ok = [bool(_SENTENCE_END_RE.search(str(block.get("text") or ""))) for block in dialogue]
    uncertain = sum(1 for block in dialogue if block.get("uncertain"))
    known_characters = {
        str(name).strip()
        for name in (script.get("characters") or [])
        if str(name).strip()
    }
    known_characters.update(
        str(profile.get("name") or "").strip()
        for profile in (script.get("characterProfiles") or [])
        if isinstance(profile, dict) and str(profile.get("name") or "").strip()
    )
    issues: list[dict[str, Any]] = []

    # Conservative evidence/continuity probes. These never invent facts; they
    # only flag structured output that cannot prove the acceptance standard.
    continuity_entities = re.compile(r"(?:手机|电话|钥匙|证据|文件|照片|钱|刀|枪|礼物|药|门|车|包|杯子|食物)")
    temporal_markers = re.compile(r"(?:当天|第二天|次日|昨晚|今晚|早上|上午|中午|下午|晚上|夜里|回忆|闪回|现在|后来|之前|之后)")
    spatial_markers = re.compile(r"(?:室内|室外|门内|门外|楼上|楼下|车内|车外|厨房|客厅|卧室|医院|学校|街上)")

    def text_of(value: Any) -> str:
        return str(value or "").strip()

    def add_issue(tag: str, severity: str, description: str, scene_index: int | None = None, block_index: int | None = None) -> None:
        issue: dict[str, Any] = {"tag": tag, "severity": severity, "description": description}
        if scene_index is not None:
            issue["scene"] = scene_index
        if block_index is not None:
            issue["block"] = block_index
        issues.append(issue)

    event_chain = script.get("eventChain") or []
    event_chain_warnings = 0
    event_roles = {
        str(item.get("role") or "").strip().lower()
        for item in event_chain
        if isinstance(item, dict)
    }
    if not event_chain:
        event_chain_warnings += 1
        add_issue("漏关键剧情", "P0", "未生成可验收的起因—冲突—转折—结果事件链，需人工核对关键剧情是否完整。")
    else:
        if not {"cause", "conflict", "turn"}.issubset(event_roles):
            event_chain_warnings += 1
            add_issue("漏关键剧情", "P0", "事件链没有起因、冲突或转折节点，无法确认剧情因果是否成立。")
        if "result" not in event_roles:
            event_chain_warnings += 1
            add_issue("场次衔接", "P1", "事件链没有结果节点，需核对结尾是否真正推动了下一步。")

        if "hook" not in event_roles:
            event_chain_warnings += 1
            add_issue("集尾无钩子", "P1", "事件链没有钩子节点，需核对集尾是否留下新危机、反转或待解决问题。")

    action_subject_warnings = 0
    action_detail_warnings = 0
    dialogue_integrity_warnings = 0
    dialogue_mixed_speaker_warnings = 0
    vo_classification_warnings = 0
    scene_task_warnings = 0
    continuity_warnings = 0
    action_structure_warnings = 0
    reaction_warnings = 0
    summary_generated_warnings = 0
    prop_continuity_warnings = 0
    temporal_warnings = 0
    spatial_warnings = 0
    evidence_warnings = 0
    sound_transition_warnings = 0
    emotion_warnings = 0

    for scene_index, scene in enumerate(scenes, start=1):
        heading = str(scene.get("heading") or "")
        location = str(scene.get("location") or "").strip()
        if not _SCENE_HEADING_RE.match(heading):
            add_issue("格式错误", "P2", f"第 {scene_index} 场缺少统一的‘编号 时段 内外 地点’场次头。", scene_index)
        if not location or location == "待补充":
            add_issue("空间跳跃", "P1", f"第 {scene_index} 场没有明确地点，无法确认空间关系。", scene_index)
        for field, label in (("goal", "目标"), ("obstacle", "阻力"), ("result", "结果")):
            if not str(scene.get(field) or "").strip():
                scene_task_warnings += 1
                add_issue("场次任务", "P1", f"第 {scene_index} 场缺少{label}，场次可能退化为流水账。", scene_index)
        if not str(scene.get("summary") or "").strip():
            add_issue("场次衔接", "P1", f"第 {scene_index} 场缺少剧情衔接说明。", scene_index)
        if scene.get("summaryGenerated") or scene.get("summarySource") == "derived":
            summary_generated_warnings += 1
            add_issue("场次衔接", "P1", f"第 {scene_index} 场的剧情衔接由系统补全，需核对起因、结果和下一步动机。", scene_index)
        if scene_index > 1 and not str(scene.get("continuityIn") or "").strip():
            continuity_warnings += 1
            add_issue("空间跳跃", "P1", f"第 {scene_index} 场没有说明上一场结果如何带入。", scene_index)
        if scene_index < len(scenes) and not str(scene.get("continuityOut") or "").strip():
            continuity_warnings += 1
            add_issue("场次衔接", "P1", f"第 {scene_index} 场没有说明结果如何推动下一场。", scene_index)

        scene_blocks = [block for block in (scene.get("blocks") or []) if isinstance(block, dict)]
        scene_text = " ".join(text_of(block.get("text")) for block in scene_blocks)
        scene_time = text_of(scene.get("timeOfDay"))
        if temporal_markers.search(scene_text) and scene_time in {"", "未知", "不明"}:
            temporal_warnings += 1
            add_issue("时间错乱", "P1", f"第 {scene_index} 场出现时间锚点，但场次头未明确时间，需回看视频核对先后顺序。", scene_index)
        if spatial_markers.search(scene_text) and (not location or location in {"待补充", "未知"}):
            spatial_warnings += 1
            add_issue("空间跳跃", "P1", f"第 {scene_index} 场正文包含空间变化，但场次地点未明确，需补齐空间关系。", scene_index)
        if continuity_entities.search(scene_text) and not scene.get("props") and not scene.get("continuityOut"):
            prop_continuity_warnings += 1
            add_issue("道具断裂", "P1", f"第 {scene_index} 场出现关键道具，但没有道具状态或场尾连续性记录。", scene_index)
        for block_index, block in enumerate(scene_blocks, start=1):
            block_type = block.get("type")
            text = str(block.get("text") or "").strip()
            emotion_present = any(
                str(block.get(key) or "").strip()
                for key in ("emotion", "emotionType", "emotionTrigger", "emotionChange", "emotionTarget", "emotionEvidence", "speechTone")
            ) or block.get("emotionIntensity") is not None
            if emotion_present:
                if not str(block.get("emotionEvidence") or "").strip():
                    emotion_warnings += 1
                    add_issue("情绪无证据", "P1", f"第 {scene_index} 场第 {block_index} 个块有情绪判断但缺少画面或声音证据。", scene_index, block_index)
                if block.get("emotionIntensity") is not None and not str(block.get("emotionTrigger") or "").strip():
                    emotion_warnings += 1
                    add_issue("情绪链不完整", "P1", f"第 {scene_index} 场第 {block_index} 个块有情绪强度但缺少触发原因。", scene_index, block_index)
            if block_type in {"dialogue", "vo"} and text and not _SENTENCE_END_RE.search(text):
                add_issue("格式错误", "P2", f"第 {scene_index} 场第 {block_index} 条台词或声音缺少句末标点。", scene_index, block_index)
            if block_type == "dialogue":
                speaker = str(block.get("speaker") or "未知说话人").strip()
                if speaker in {"", "未知说话人"} or block.get("uncertain"):
                    add_issue("台词归属错", "P0", f"第 {scene_index} 场第 {block_index} 句台词说话人需要核对。", scene_index, block_index)
                if block.get("dialogueIssues"):
                    dialogue_integrity_warnings += 1
                    if any(item in {"mixed_speakers", "split_mixed_speakers"} for item in block.get("dialogueIssues", [])):
                        dialogue_mixed_speaker_warnings += 1
                    add_issue("漏台词", "P0", f"第 {scene_index} 场第 {block_index} 句对白存在完整性问题：{','.join(block['dialogueIssues'])}。", scene_index, block_index)
            elif block_type == "vo" and block.get("voKind") in {"unknown", ""}:
                vo_classification_warnings += 1
                add_issue("OS/VO混淆", "P1", f"第 {scene_index} 场第 {block_index} 条声音来源不明确。", scene_index, block_index)
            elif block_type == "action":
                has_explicit_subject = text.startswith("未知人物") or text.startswith("未知说话人") or any(
                    text.startswith(name) for name in known_characters
                )
                if known_characters and not has_explicit_subject:
                    action_subject_warnings += 1
                    add_issue("可拍摄性", "P0", f"第 {scene_index} 场第 {block_index} 个动作缺少明确人物主体。", scene_index, block_index)
                missing_parts = [
                    label
                    for key, label in (("object", "对象"), ("result", "结果"))
                    if not str(block.get(key) or "").strip()
                ]
                if missing_parts:
                    action_detail_warnings += 1
                    add_issue(
                        "动作细节",
                        "P1",
                        f"第 {scene_index} 场第 {block_index} 个动作缺少{'、'.join(missing_parts)}，需回看视频补齐可执行信息。",
                        scene_index,
                        block_index,
                    )
                if _ABSTRACT_ACTION_RE.search(text) or not _ACTION_VERB_RE.search(text):
                    action_structure_warnings += 1
                    add_issue("动作概括", "P1", f"第 {scene_index} 场第 {block_index} 个动作可能停留在抽象结论，需补主体、动作、对象和结果。", scene_index, block_index)

                if block.get("inferred") or block.get("evidence") is False:
                    evidence_warnings += 1
                    add_issue("事实臆造", "P0", f"第 {scene_index} 场第 {block_index} 个动作标记为推断，不能当作视频事实输出。", scene_index, block_index)
            elif block_type == "sound":
                if text and block.get("source") not in {None, "", "heard", "听见"}:
                    sound_transition_warnings += 1
                    add_issue("音效缺失", "P1", f"第 {scene_index} 场第 {block_index} 个声音来源未标记为可听证据。", scene_index, block_index)
            elif block_type in {"screen_text", "transition"} and not (block.get("startSec") is not None or block.get("endSec") is not None):
                sound_transition_warnings += 1
                add_issue("格式错误", "P2", f"第 {scene_index} 场第 {block_index} 个字幕/转场缺少时间定位。", scene_index, block_index)

        # A sharp line normally needs a visible or audible response before the
        # next beat. Flag only high-impact dialogue with no nearby reaction so
        # ordinary two-person exchanges do not become false positives.
        for block_index, block in enumerate(scene_blocks):
            if block.get("type") != "dialogue" or not _HIGH_IMPACT_DIALOGUE_RE.search(str(block.get("text") or "")):
                continue
            following = scene_blocks[block_index + 1 : block_index + 3]
            if not following or not any(
                item.get("type") == "emotion"
                or (item.get("type") == "action" and _REACTION_ACTION_RE.search(str(item.get("text") or "")))
                for item in following
            ):
                reaction_warnings += 1
                add_issue("反应缺失", "P1", f"第 {scene_index} 场第 {block_index + 1} 句高冲突台词后缺少可见反应或情绪转折。", scene_index, block_index + 1)

        previous_intensity: int | None = None
        for block_index, block in enumerate(scene_blocks):
            intensity = block.get("emotionIntensity")
            if not isinstance(intensity, int):
                continue
            if previous_intensity is not None and abs(intensity - previous_intensity) >= 2 and not str(block.get("emotionChange") or "").strip():
                emotion_warnings += 1
                add_issue("情绪变化丢失", "P1", f"第 {scene_index} 场第 {block_index + 1} 个块情绪强度跨级变化，但没有记录变化节点。", scene_index, block_index + 1)
            previous_intensity = intensity

    profile_by_name = {
        str(profile.get("name") or "").strip(): profile
        for profile in (script.get("characterProfiles") or [])
        if isinstance(profile, dict) and str(profile.get("name") or "").strip()
    }
    character_introduction_warnings = sum(
        1
        for name in known_characters
        if not any(
            (value := str(profile_by_name.get(name, {}).get(field) or "").strip())
            and "待核对" not in value
            for field in ("firstAppearance", "appearance", "clothing")
        )
    )
    if character_introduction_warnings:
        add_issue("人物信息", "P1", f"有 {character_introduction_warnings} 个人物缺少首次出现时的外观或服装证据。")

    if scenes and not str(scenes[-1].get("hook") or "").strip():
        add_issue("结尾无钩子", "P1", "最后一场没有明确的新危机、反转、悬念或强情绪落点。", len(scenes))

    coverage = round(100 * sum(punctuation_ok) / len(punctuation_ok)) if punctuation_ok else 0
    confidence = round(100 * (1 - uncertain / len(dialogue))) if dialogue else 0
    issue_tags = list(dict.fromkeys(str(item["tag"]) for item in issues))
    severity_counts = {
        level: sum(1 for item in issues if item.get("severity") == level)
        for level in ("P0", "P1", "P2")
    }
    return {
        "dialogueCoverage": coverage,
        "speakerConfidence": max(0, confidence),
        "warnings": len(issues),
        "sceneCount": len(scenes),
        "dialogueCount": len(dialogue),
        "actionCount": len(action_blocks),
        "actionSubjectWarnings": action_subject_warnings,
        "actionDetailWarnings": action_detail_warnings,
        "actionStructureWarnings": action_structure_warnings,
        "dialogueIntegrityWarnings": dialogue_integrity_warnings,
        "dialogueMixedSpeakerWarnings": dialogue_mixed_speaker_warnings,
        "voClassificationWarnings": vo_classification_warnings,
        "characterIntroductionWarnings": character_introduction_warnings,
        "sceneTaskWarnings": scene_task_warnings,
        "continuityWarnings": continuity_warnings,
        "eventChainWarnings": event_chain_warnings,
        "reactionWarnings": reaction_warnings,
        "summaryGeneratedWarnings": summary_generated_warnings,
        "propContinuityWarnings": prop_continuity_warnings,
        "temporalWarnings": temporal_warnings,
        "spatialWarnings": spatial_warnings,
        "evidenceWarnings": evidence_warnings,
        "soundTransitionWarnings": sound_transition_warnings,
        "emotionWarnings": emotion_warnings,
        "severityCounts": severity_counts,
        "issueTags": issue_tags,
        "issues": issues[:100],
        "provider": provider,
    }


def quality_gate(quality: dict[str, Any]) -> tuple[bool, list[dict[str, Any]]]:
    """Return whether a script is safe to expose as a downloadable result.

    P0/P1 findings are acceptance failures.  P0 findings are fact or structure
    breaks; P1 findings are continuity, action-chain, or classification gaps.
    Both can reproduce the old defects in a delivered screenplay, so callers
    must keep the result in review instead of silently shipping a warning.
    """
    issues = [
        issue
        for issue in (quality.get("issues") or [])
        if isinstance(issue, dict) and str(issue.get("severity") or "").upper() in {"P0", "P1"}
    ]
    return not issues, issues
