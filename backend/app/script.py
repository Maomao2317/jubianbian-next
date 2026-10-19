"""Script normalization, quality checks, and export formatting."""

from __future__ import annotations

from difflib import SequenceMatcher
import math
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
# Models occasionally copy review notes from a reference screenplay into the
# returned JSON.  These phrases are instructions to the editor, not visible
# video content, and must never become an action/dialogue block.
_EDITORIAL_ARTIFACT_RE = re.compile(
    r"^(?:"
    r"这里(?:不需要|无需|不用|应该|是|画面|缺少)|"
    r"画面(?:应该|需要|缺少)|"
    r"缺少(?:人物|台词|对白|动作|情绪|描述|字幕|画面|内容)|"
    r"少[^，。；：:]{0,16}(?:说话|台词|对白|动作|描述|情绪|字幕)|"
    r"(?:这(?:里|一段|一大段|两句|三句|几句)|本段)(?:不需要|无需|不用|是|为)|"
    r"(?:需要|待)补(?:充|齐)|待核对|此处(?:省略|删除)|"
    r"\d+[：:][^。]{0,20}(?:这里|说话|台词|字幕)"
    r")"
)
_SCENE_HEADING_RE = re.compile(
    r"^\s*\d+-\d+\s+(?:日|夜|清晨|黄昏|傍晚|傍晚转夜|日转夜|不明)\s+"
    r"(?:内|外|内外|内转外|外转内|不明)\s+.+"
)
# Episode markers are intentionally kept independent from the media upload
# helpers.  Normalization also runs on provider responses and on legacy JSON
# that may not have gone through an upload route first.
_EPISODE_MARKER_RE = re.compile(
    r"(?:\u7b2c\s*0*(\d{1,4})\s*(?:\u96c6|\u8bdd|\u671f)|"
    r"(?:^|[\s_.()\[\]\u3010\u3011-])(?:ep|e|episode)\s*0*(\d{1,4})(?=$|[\s_.()\[\]\u3010\u3011-]))",
    re.IGNORECASE,
)
_SCENE_NUMBER_RE = re.compile(r"^\s*(\d{1,4})-(\d{1,5})(?=\s|$)")
_VO_KINDS = {"os", "offscreen", "narration", "memory", "phone", "unknown"}
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
    "offscreen": "offscreen",
    "off_screen": "offscreen",
    "画外对白": "offscreen",
}
_DIALOGUE_LABEL_RE = re.compile(r"(?:^|[。！？…\n])\s*([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9· ]{0,14})\s*[：:]")
_INTERNAL_REVIEW_MARKER_RE = re.compile(r"【(?:需核对|待核对|内部校对|审核提示)[^】]*】")
_EMOTION_TOKEN_RE = re.compile(
    r"(?:紧张|害怕|恐惧|愤怒|生气|焦急|着急|悲伤|难过|惊讶|震惊|疑惑|冷漠|平静|温柔|慌乱|失望|绝望|委屈|欣慰|担忧|激动|疲惫|无奈|警惕|尴尬|沉重|坚定|急切)"
)
_SPEECH_CUE_RE = re.compile(
    r"(?:开口|说道|说着|说|问道|询问|追问|反问|问|回答|答道|回应|喊道|呼喊|喊|叫道|怒斥|嘀咕|念道|低声道|高声道)"
)


def character_label(value: Any) -> str:
    """A boolean lip-sync flag is never a character identity."""
    if not isinstance(value, str):
        return ""
    label = value.strip()
    if label.casefold() in {"true", "false", "none", "null", "yes", "no", "0", "1"}:
        return ""
    return label


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


def strip_internal_review_markers(text: Any) -> str:
    """Remove QA-only badges while preserving the recovered source words."""
    return _INTERNAL_REVIEW_MARKER_RE.sub("", str(text or "")).strip()


def compact_emotion_text(text: Any, *, max_parts: int = 2, max_chars: int = 72) -> str:
    """Deduplicate repeated emotion wording without rewriting factual action.

    This is intentionally conservative: only clauses containing a known
    emotion token participate in de-duplication; concrete actions and visual
    details remain untouched.
    """
    value = normalize_text(strip_internal_review_markers(text)).strip("，。！？；：: ")
    if not value:
        return ""
    parts = [part.strip("，、。！？；：: ") for part in re.split(r"[、；;，,。！？…]+", value) if part.strip()]
    kept: list[str] = []
    seen_tokens: set[str] = set()
    seen_clauses: set[str] = set()
    for part in parts:
        clause_key = re.sub(r"\s+", "", part)
        if clause_key in seen_clauses:
            continue
        tokens = set(_EMOTION_TOKEN_RE.findall(part))
        if tokens and tokens & seen_tokens:
            # A later clause such as “情绪紧张、神情紧张” adds no visible
            # information. Keep the first occurrence only.
            continue
        if len(part) > max_chars:
            part = part[:max_chars].rstrip("，、。！？；：: ")
        kept.append(part)
        seen_clauses.add(clause_key)
        seen_tokens.update(tokens)
        if len(kept) >= max_parts:
            break
    return "、".join(dict.fromkeys(kept))


def compact_character_detail(text: Any, *, max_chars: int = 42) -> str:
    """Keep one short, concrete appearance/clothing identity sentence.

    Provider responses often repeat a noun in slightly different forms (for
    example ``麻花辫的长麻花辫`` or ``碎花衫、碎花翻领衬衫``).  We retain the
    most specific phrase and drop only exact/obvious overlaps.
    """
    value = normalize_text(strip_internal_review_markers(text)).strip("。！？；; ")
    if not value:
        return ""
    value = re.sub(r"^空(?=(?:浅|深|黑|白|红|蓝|绿|灰|紫|黄|棕))", "穿", value)
    value = re.sub(r"(麻花辫)的(?:长|短)?麻花辫", r"长\1", value)
    clauses = [part.strip("，、；：: ") for part in re.split(r"[，、；：:]+", value) if part.strip()]
    selected: list[str] = []
    for clause in clauses:
        # When one clothing phrase contains an earlier, less specific phrase,
        # keep the specific one and remove the shorter duplicate.
        contained = False
        for index, existing in enumerate(selected):
            if clause in existing and len(existing) >= len(clause) + 2:
                contained = True
                break
            if existing in clause and len(clause) >= len(existing) + 2:
                selected[index] = clause
                contained = True
                break
        if not contained:
            selected.append(clause)
    result = "、".join(dict.fromkeys(selected))
    if len(result) > max_chars:
        result = result[:max_chars].rstrip("，、；：: ")
    return result


def evidence_comparison_text(text: Any) -> str:
    """Reduce recovered speech/OCR to comparable characters only.

    Punctuation and whitespace are intentionally ignored because the screenplay
    is allowed to restore Chinese punctuation.  Words, names, digits and their
    order remain untouched, so this cannot make a paraphrase look verbatim.
    """
    value = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", value)


def ordered_script_text(script: dict[str, Any], block_types: set[str]) -> str:
    return "".join(
        evidence_comparison_text(block.get("text"))
        for scene in (script.get("scenes") or [])
        if isinstance(scene, dict)
        for block in (scene.get("blocks") or [])
        if isinstance(block, dict) and block.get("type") in block_types
    )


def time_window_script_text(
    script: dict[str, Any],
    block_types: set[str],
    start_sec: Any,
    end_sec: Any,
    *,
    margin: float = 0.8,
) -> str:
    """Return text from timestamped blocks overlapping one evidence segment."""
    if not isinstance(start_sec, (int, float)) or not isinstance(end_sec, (int, float)):
        return ""
    start = max(0.0, float(start_sec) - margin)
    end = max(start, float(end_sec) + margin)
    values: list[str] = []
    for scene in script.get("scenes") or []:
        if not isinstance(scene, dict):
            continue
        for block in scene.get("blocks") or []:
            if not isinstance(block, dict) or block.get("type") not in block_types:
                continue
            block_start = block.get("startSec")
            block_end = block.get("endSec")
            if not isinstance(block_start, (int, float)) and not isinstance(block_end, (int, float)):
                continue
            left = float(block_start if isinstance(block_start, (int, float)) else block_end)
            right = float(block_end if isinstance(block_end, (int, float)) else block_start)
            if right >= start and left <= end:
                values.append(evidence_comparison_text(block.get("text")))
    return "".join(values)


def matching_character_counts(reference: str, candidate: str) -> tuple[int, int, int]:
    """Return ordered matching characters and both input sizes."""
    if not reference or not candidate:
        return 0, len(reference), len(candidate)
    if reference == candidate:
        return len(reference), len(reference), len(candidate)
    # Keep difflib's popularity heuristic enabled.  Disabling it makes highly
    # repetitive ASR output quadratic (a long ``啊啊啊`` clip can otherwise
    # stall the worker), while normal Chinese speech still gets ordered matches.
    matcher = SequenceMatcher(None, reference, candidate)
    matched = sum(item.size for item in matcher.get_matching_blocks())
    return matched, len(reference), len(candidate)


def subtitle_aligned_evidence(
    script: dict[str, Any],
    evidence: dict[str, Any] | None,
    *,
    update_script: bool = False,
) -> tuple[dict[str, Any] | None, int]:
    """Use subtitles only when the corresponding audio is not intelligible.

    Clear speech is authoritative even when the subtitle uses different words.
    A low-confidence/unclear audio segment may use a confirmed subtitle as its
    wording reference. In both cases the independent evidence remains intact
    for quality checks.
    """
    if not isinstance(evidence, dict):
        return evidence, 0
    sources = evidence.get("sources") or []
    audio = next((s for s in sources if isinstance(s, dict) and s.get("kind") == "audio"
                  and s.get("status") == "available"), None)
    ocr = [item for s in sources if isinstance(s, dict) and s.get("kind") == "ocr"
           and s.get("status") == "available" for item in s.get("items") or []
           if isinstance(item, dict)]
    if audio is None or not ocr:
        return evidence, 0

    def span(item: dict[str, Any]) -> tuple[float, float] | None:
        start, end = item.get("startSec"), item.get("endSec", item.get("startSec"))
        if type(start) not in {int, float} or type(end) not in {int, float}:
            return None
        if not (0 <= start < float("inf") and start <= end < float("inf")):
            return None
        return float(start), float(end)

    def overlaps(left: tuple[float, float], right: tuple[float, float]) -> bool:
        return min(left[1], right[1]) > max(left[0], right[0])

    def audio_is_unclear(item: dict[str, Any], block: dict[str, Any]) -> bool:
        # Block confidence/uncertainty also describes speaker ownership. Only
        # explicit speech metadata or low-confidence ASR can authorize a text
        # replacement; an unknown speaker must not change clearly heard words.
        if any(value.get("audioUnclear") is False
               or str(value.get("speechClarity") or "").casefold() == "clear"
               for value in (item, block)):
            return False
        if item.get("uncertain") is True or any(value.get("audioUnclear") is True for value in (item, block)):
            return True
        for value in (item.get("confidence"), item.get("speechClarity"), block.get("speechClarity")):
            normalized = str(value or "").strip().casefold()
            if normalized in {"low", "unclear", "inaudible", "unintelligible", "noise", "不清晰", "听不清"}:
                return True
        return False

    blocks = [block for scene in script.get("scenes") or [] for block in scene.get("blocks") or []
              if isinstance(block, dict)]
    spoken = [(b, span(b)) for b in blocks if b.get("type") in {"dialogue", "vo"} and span(b)]
    segments = [(i, item, span(item)) for i, item in enumerate(audio.get("items") or [])
                if isinstance(item, dict) and item.get("type") == "transcript_segment" and span(item)]
    observations = [(evidence_comparison_text(item.get("text")), span(item)) for item in ocr if span(item)]
    proposals: dict[int, list[tuple[dict[str, Any], str, str]]] = {}
    for subtitle in blocks:
        if (subtitle.get("type") != "screen_text" or subtitle.get("screenType") != "subtitle"
                or subtitle.get("uncertain") or subtitle.get("confidence") == "low"):
            continue
        window = span(subtitle)
        raw = str(subtitle.get("text") or "").strip()
        text = evidence_comparison_text(raw)
        if not window or not text or not any(t == text and w[0] <= window[1] and w[1] >= window[0]
                                              for t, w in observations):
            continue
        targets = [(b, w) for b, w in spoken if overlaps(w, window)]
        refs = [(i, item, w) for i, item, w in segments if overlaps(w, window)]
        if len(targets) != 1 or len(refs) != 1:
            continue
        block, block_window = targets[0]
        index, item, audio_window = refs[0]
        # ASR may include several subtitle lines. A single partial line must
        # not erase the rest; leave ambiguous alignment to video review.
        if audio_window[0] < window[0] - 0.5 or audio_window[1] > window[1] + 0.5:
            continue
        reference = evidence_comparison_text(item.get("text"))
        candidate = evidence_comparison_text(block.get("text"))
        subtitle_text = evidence_comparison_text(raw)
        if not audio_is_unclear(item, block):
            # A provider that selected a subtitle despite clear speech must be
            # restored to the audible wording. Only replace a whole subtitle
            # line; a partial caption is intentionally left for review.
            if (update_script and block.get("textSource") == "subtitle"
                    and candidate == subtitle_text and reference and reference != subtitle_text):
                replacement = normalize_dialogue_text(item.get("text"))
                block["text"] = replacement
                for key in ("sourceText", "rawText", "finalText"):
                    block[key] = str(item.get("text") or "").strip()
                block["textSource"] = "audio"
            continue
        matched, ref_size, sub_size = matching_character_counts(reference, text)
        similar = bool(ref_size and sub_size and matched / ref_size >= 0.5 and matched / sub_size >= 0.5)
        explicit = block.get("textSource") == "subtitle" and candidate == text
        if not similar and not explicit:
            continue
        # Replace just the matching utterance inside a longer dialogue block.
        # If the model already chose the subtitle, only adjust its ASR reference.
        if update_script and candidate == reference and candidate != text:
            replacement = raw
        elif candidate.count(text) == 1:
            replacement = str(block.get("text") or "")
        elif update_script and reference and candidate.count(reference) == 1:
            original = str(block.get("text") or "")
            positions = [i for i, char in enumerate(original) for _ in evidence_comparison_text(char)]
            offset = candidate.index(reference)
            replacement = original[:positions[offset]] + raw + original[positions[offset + len(reference) - 1] + 1:]
        elif (update_script and block_window[0] >= window[0] - 0.5
              and block_window[1] <= window[1] + 0.5 and candidate
              and matching_character_counts(candidate, text)[0] >= 0.7 * max(len(candidate), len(text))):
            replacement = raw
        else:
            continue
        proposals.setdefault(index, []).append((block, raw, replacement))

    replacements: dict[int, dict[str, Any]] = {}
    for index, choices in proposals.items():
        # Conflicting captions in one audio span are not a safe override.
        if len({evidence_comparison_text(raw) for _, raw, _ in choices}) != 1:
            continue
        block, raw, replacement = choices[0]
        if update_script:
            # Earlier aligned segments may have repaired the same long block.
            # Recompute this span against its current text to retain both edits.
            reference = evidence_comparison_text(audio["items"][index].get("text"))
            current = str(block.get("text") or "")
            candidate = evidence_comparison_text(current)
            subtitle_text = evidence_comparison_text(raw)
            if candidate == reference:
                replacement = raw
            elif candidate.count(reference) == 1 and candidate.count(subtitle_text) != 1:
                positions = [i for i, char in enumerate(current) for _ in evidence_comparison_text(char)]
                offset = candidate.index(reference)
                replacement = current[:positions[offset]] + raw + current[positions[offset + len(reference) - 1] + 1:]
            elif candidate.count(subtitle_text) == 1:
                replacement = current
            replacement = normalize_dialogue_text(replacement)
            block["text"] = replacement
            for key in ("sourceText", "rawText", "finalText"):
                block[key] = replacement
            block["textSource"] = "subtitle"
        item = audio["items"][index]
        if evidence_comparison_text(item.get("text")) != evidence_comparison_text(raw):
            # Word timestamps still describe the audible version. Keeping
            # them as separate references would undo this deliberate choice.
            replacements[index] = {**item, "text": raw, "words": []}
    if not replacements:
        return evidence, 0
    aligned_audio = {**audio, "items": [replacements.get(i, item) for i, item in enumerate(audio["items"])]}
    return {**evidence, "sources": [aligned_audio if s is audio else s for s in sources]}, len(replacements)


def suppress_redundant_subtitles(script: dict[str, Any]) -> None:
    """Keep dialogue captions for QA, displaying their spoken lines only.

    Calculate this after repairs as well as normalization: restored dialogue
    may cover a previously orphaned caption. A caption may span several lines
    or a scene boundary, but never a different episode with reset timestamps.
    """
    spoken_by_episode: dict[str, list[dict[str, Any]]] = {}
    scenes = [scene for scene in script.get("scenes") or [] if isinstance(scene, dict)]

    def episode_key(scene: dict[str, Any]) -> str:
        match = re.match(r"^\s*(\d+)-\d+", str(scene.get("heading") or ""))
        return match.group(1) if match else str(script.get("episodeNumber") or "1")

    def span(block: dict[str, Any]) -> tuple[float, float] | None:
        start, end = block.get("startSec"), block.get("endSec")
        if (type(start) in {int, float} and type(end) in {int, float}
                and 0 <= start <= end < float("inf")):
            return float(start), float(end)
        return None

    def spoken_text(block: dict[str, Any]) -> str:
        text = re.sub(r"[\[【（(](?:听不清|无法辨认|不清晰)[\]】）)]", "", str(block.get("text") or ""))
        return evidence_comparison_text(text)

    for scene in scenes:
        for block in scene.get("blocks") or []:
            if not isinstance(block, dict):
                continue
            block.pop("suppressInOutput", None)
            if block.get("type") in {"dialogue", "vo"} and spoken_text(block):
                spoken_by_episode.setdefault(episode_key(scene), []).append(block)

    for scene in scenes:
        blocks = scene.get("blocks") or []
        for index, subtitle in enumerate(blocks):
            if (not isinstance(subtitle, dict) or subtitle.get("type") != "screen_text"
                    or subtitle.get("screenType") != "subtitle"):
                continue
            text = evidence_comparison_text(subtitle.get("text"))
            if not text:
                continue
            window = span(subtitle)
            if window is None:
                # Legacy untimed records: only an identical adjacent line is
                # safe to hide; repeated text elsewhere proves nothing.
                neighbors = blocks[max(0, index - 1):index] + blocks[index + 1:index + 2]
                if any(block.get("type") in {"dialogue", "vo"} and text == spoken_text(block)
                       for block in neighbors if isinstance(block, dict)):
                    subtitle["suppressInOutput"] = True
                continue
            candidates = [block for block in spoken_by_episode.get(episode_key(scene), [])
                          if span(block) and span(block)[1] >= window[0] - 0.35
                          and span(block)[0] <= window[1] + 0.35]
            candidates.sort(key=lambda block: span(block))
            joined = "".join(spoken_text(block) for block in candidates)
            # Matching wording handles brief caption lag. For shortened or
            # differently worded captions, require substantial time coverage,
            # not a single coincident edge from an unrelated sentence.
            intervals = sorted((max(window[0], span(block)[0]), min(window[1], span(block)[1]))
                               for block in candidates if span(block)[1] > window[0]
                               and span(block)[0] < window[1])
            covered, end = 0.0, window[0]
            for left, right in intervals:
                covered += max(0.0, right - max(left, end))
                end = max(end, right)
            duration = window[1] - window[0]
            if (joined and text in joined) or (duration > 0 and covered / duration >= 0.65):
                subtitle["suppressInOutput"] = True


def restore_short_transcript_segments(
    script: dict[str, Any],
    evidence: dict[str, Any] | None,
    *,
    max_segment_chars: int = 16,
) -> dict[str, int]:
    """Restore short ASR beats that a video model compressed or dropped.

    The repair only touches a single timestamped dialogue/VO block when the
    model text is almost entirely an ordered subset of independent ASR. This
    covers pauses such as ``今天 / 我决定 / 我`` without guessing a speaker.
    Multiple ASR beats mapped to one block are split back into separate lines
    so repeated one-character dialogue is not lost by de-duplication.
    """
    result = {"segments": 0, "blocks": 0}
    if not isinstance(script, dict) or not isinstance(evidence, dict):
        return result
    evidence, _ = subtitle_aligned_evidence(script, evidence)
    audio = next(
        (
            source
            for source in (evidence.get("sources") or [])
            if isinstance(source, dict) and source.get("kind") == "audio" and source.get("status") == "available"
        ),
        None,
    )
    if not isinstance(audio, dict):
        return result
    segments = []
    for item in audio.get("items") or []:
        if not isinstance(item, dict) or item.get("type") != "transcript_segment":
            continue
        comparison = evidence_comparison_text(item.get("text"))
        start = item.get("startSec")
        end = item.get("endSec")
        if not comparison or len(comparison) > max_segment_chars:
            continue
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            continue
        segments.append({**item, "_comparison": comparison})
    if not segments:
        return result

    scenes = [scene for scene in (script.get("scenes") or []) if isinstance(scene, dict)]
    timed_blocks: list[tuple[int, int, dict[str, Any], float, float]] = []
    for scene_index, scene in enumerate(scenes):
        for block_index, block in enumerate(scene.get("blocks") or []):
            if not isinstance(block, dict) or block.get("type") not in {"dialogue", "vo", "os"}:
                continue
            start = block.get("startSec")
            end = block.get("endSec")
            if not isinstance(start, (int, float)) and not isinstance(end, (int, float)):
                continue
            left = float(start if isinstance(start, (int, float)) else end)
            right = float(end if isinstance(end, (int, float)) else start)
            if right < left:
                left, right = right, left
            timed_blocks.append((scene_index, block_index, block, left, right))

    grouped: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for segment in segments:
        left = float(segment["startSec"])
        right = float(segment["endSec"])
        if right < left:
            left, right = right, left
        overlapping = [
            entry for entry in timed_blocks
            if entry[4] >= left - 0.35 and entry[3] <= right + 0.35
        ]
        if len(overlapping) != 1:
            continue
        scene_index, block_index, _block, _start, _end = overlapping[0]
        grouped.setdefault((scene_index, block_index), []).append(segment)

    for (scene_index, block_index), mapped in sorted(grouped.items(), reverse=True):
        blocks = scenes[scene_index].get("blocks") or []
        if block_index >= len(blocks) or not isinstance(blocks[block_index], dict):
            continue
        block = blocks[block_index]
        ordered = sorted(mapped, key=lambda item: (float(item["startSec"]), float(item["endSec"])))
        reference = "".join(str(item.get("_comparison") or "") for item in ordered)
        candidate = evidence_comparison_text(block.get("text"))
        matched, reference_size, candidate_size = matching_character_counts(reference, candidate)
        if not reference_size or not candidate_size:
            continue
        reference_coverage = matched / reference_size
        candidate_precision = matched / candidate_size
        # The provider line must look like an incomplete subset of ASR. When it
        # contains substantial extra wording, keep it and request video review.
        if reference_coverage >= 0.98 or candidate_precision < 0.85:
            continue

        replacements: list[dict[str, Any]] = []
        for item in ordered:
            text = normalize_dialogue_text(strip_internal_review_markers(item.get("text")))
            if not text:
                continue
            replacement = dict(block)
            replacement["text"] = text
            replacement["startSec"] = float(item["startSec"])
            replacement["endSec"] = float(item["endSec"])
            replacement["asrRecovered"] = True
            if replacement.get("type") == "dialogue":
                replacement["rawText"] = str(item.get("text") or "").strip()
                replacement["finalText"] = text
                replacement["sourceText"] = str(item.get("text") or "").strip()
            replacements.append(replacement)
        if not replacements:
            continue
        blocks[block_index : block_index + 1] = replacements
        result["segments"] += len(replacements)
        result["blocks"] += 1
    return result


def restore_transcript_gap_segments(
    script: dict[str, Any],
    evidence: dict[str, Any] | None,
    *,
    max_missing_chars: int = 12,
) -> dict[str, int]:
    """Restore one provable interior gap from a time-aligned ASR segment.

    Boundary recovery fixes dropped opening/closing words, but a provider can
    also omit a short middle phrase (``我决定`` from ``我今天决定``).  This
    pass accepts only a single contiguous deletion from the ASR reference,
    with one unambiguous timed block and no replacement/substitution.  It is
    therefore safe against ASR paraphrases and never assigns a line to a new
    speaker. Short one-word segments remain handled by the stricter repair.
    """
    result = {"segments": 0, "blocks": 0}
    if not isinstance(script, dict) or not isinstance(evidence, dict):
        return result
    evidence, _ = subtitle_aligned_evidence(script, evidence)
    audio = next(
        (
            source
            for source in (evidence.get("sources") or [])
            if isinstance(source, dict) and source.get("kind") == "audio" and source.get("status") == "available"
        ),
        None,
    )
    if not isinstance(audio, dict):
        return result
    scenes = [scene for scene in (script.get("scenes") or []) if isinstance(scene, dict)]
    timed_blocks: list[tuple[int, int, dict[str, Any], float, float]] = []
    for scene_index, scene in enumerate(scenes):
        for block_index, block in enumerate(scene.get("blocks") or []):
            if not isinstance(block, dict) or block.get("type") not in {"dialogue", "vo"}:
                continue
            start = block.get("startSec")
            end = block.get("endSec")
            if not isinstance(start, (int, float)) and not isinstance(end, (int, float)):
                continue
            left = float(start if isinstance(start, (int, float)) else end)
            right = float(end if isinstance(end, (int, float)) else start)
            timed_blocks.append((scene_index, block_index, block, min(left, right), max(left, right)))

    def one_gap(reference: str, candidate: str) -> str | None:
        if len(reference) <= len(candidate) or len(candidate) < 3:
            return None
        opcodes = SequenceMatcher(None, reference, candidate, autojunk=False).get_opcodes()
        gaps: list[str] = []
        for tag, ref_start, ref_end, cand_start, cand_end in opcodes:
            if tag == "equal":
                continue
            if tag == "delete":
                gaps.append(reference[ref_start:ref_end])
                continue
            # A replacement means the model/ASR disagrees on a spoken word;
            # do not silently choose either side.
            return None
        missing = "".join(gaps)
        if len(gaps) != 1 or not missing or len(missing) > max_missing_chars:
            return None
        matched = len(candidate)
        if matched / max(1, len(reference)) < 0.55:
            return None
        return missing

    repaired_blocks: set[tuple[int, int]] = set()
    for item in audio.get("items") or []:
        if not isinstance(item, dict) or item.get("type") != "transcript_segment":
            continue
        raw_text = str(item.get("text") or "").strip()
        reference = evidence_comparison_text(raw_text)
        start = item.get("startSec")
        end = item.get("endSec")
        if not raw_text or len(reference) < 4 or len(reference) > 180:
            continue
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
            continue
        left, right = min(float(start), float(end)), max(float(start), float(end))
        overlapping = [
            entry for entry in timed_blocks
            if entry[4] >= left - 0.35 and entry[3] <= right + 0.35
        ]
        if len(overlapping) != 1:
            continue
        scene_index, block_index, block, _block_start, _block_end = overlapping[0]
        if (scene_index, block_index) in repaired_blocks:
            continue
        candidate = evidence_comparison_text(block.get("text"))
        if one_gap(reference, candidate) is None:
            continue
        recovered_text = normalize_dialogue_text(raw_text)
        if not recovered_text or recovered_text == block.get("text"):
            continue
        block["text"] = recovered_text
        if block.get("type") == "dialogue":
            block["rawText"] = raw_text
            block["finalText"] = recovered_text
            block["sourceText"] = raw_text
        block["evidenceRecovered"] = True
        block["asrRecovered"] = True
        repaired_blocks.add((scene_index, block_index))
        result["segments"] += 1
        result["blocks"] += 1
    return result


def restore_evidence_boundary_segments(
    script: dict[str, Any],
    evidence: dict[str, Any] | None,
    *,
    max_missing_chars: int = 12,
) -> dict[str, int]:
    """Recover a short missing prefix/suffix from time-aligned ASR or subtitles.

    A common failure mode is that the model starts a dialogue a few characters
    late even though the opening word is audible and visible in the subtitle.
    The old short-beat repair intentionally ignored longer ASR segments, so it
    could not correct ``我今天已经决定……`` becoming ``今天已经决定……``.  This
    repair only replaces an existing timed dialogue/VO block when the evidence
    contains the complete model text as one contiguous prefix/suffix match and
    the omitted boundary is small.  It never invents a new speaker or a new
    line. OCR additionally needs a corresponding subtitle independently
    classified by the video model; an OCR substring alone is not authority.
    """
    result = {"segments": 0, "blocks": 0}
    if not isinstance(script, dict) or not isinstance(evidence, dict):
        return result
    evidence, _ = subtitle_aligned_evidence(script, evidence)

    scenes = [scene for scene in (script.get("scenes") or []) if isinstance(scene, dict)]
    timed_blocks: list[tuple[int, int, dict[str, Any], float, float]] = []
    for scene_index, scene in enumerate(scenes):
        for block_index, block in enumerate(scene.get("blocks") or []):
            if not isinstance(block, dict) or block.get("type") not in {"dialogue", "vo"}:
                continue
            start = block.get("startSec")
            end = block.get("endSec")
            if not isinstance(start, (int, float)) and not isinstance(end, (int, float)):
                continue
            left = float(start if isinstance(start, (int, float)) else end)
            right = float(end if isinstance(end, (int, float)) else start)
            if right < left:
                left, right = right, left
            timed_blocks.append((scene_index, block_index, block, left, right))

    def boundary_match(reference: str, candidate: str) -> bool:
        if len(reference) <= len(candidate) or len(candidate) < 2:
            return False
        missing = len(reference) - len(candidate)
        if missing > max_missing_chars or candidate not in reference:
            return False
        offset = reference.find(candidate)
        # Only a boundary omission is safe.  An interior mismatch may be an
        # ASR/OCR error and must remain visible to the quality gate.
        return offset <= max_missing_chars and (len(reference) - offset - len(candidate)) <= max_missing_chars

    for source in evidence.get("sources") or []:
        if not isinstance(source, dict) or source.get("status") != "available":
            continue
        kind = str(source.get("kind") or "").strip().lower()
        if kind not in {"audio", "ocr"}:
            continue
        for item in source.get("items") or []:
            if not isinstance(item, dict):
                continue
            expected_type = "transcript_segment" if kind == "audio" else "screen_text"
            if item.get("type") != expected_type:
                continue
            raw_text = str(item.get("text") or "").strip()
            reference = evidence_comparison_text(raw_text)
            start = item.get("startSec")
            end = item.get("endSec")
            if not raw_text or not reference or len(reference) > 180:
                continue
            if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
                continue
            left = min(float(start), float(end))
            right = max(float(start), float(end))
            margin = 0.75 if kind == "ocr" else 0.35
            overlapping = [
                entry for entry in timed_blocks
                if entry[4] >= left - margin and entry[3] <= right + margin
            ]
            if len(overlapping) != 1:
                continue
            scene_index, block_index, block, _block_start, _block_end = overlapping[0]
            if kind == "ocr":
                # OCR also reads textures, logos and background objects. A
                # matching substring does not prove that its surrounding
                # characters were spoken. Require the video model to have
                # independently identified this *whole* observation as a
                # reliable dialogue subtitle at the same time. Compare the
                # surface text too: comparison_text intentionally drops
                # punctuation, which could otherwise authorize OCR symbols.
                confirmed = any(
                    caption.get("type") == "screen_text"
                    and caption.get("screenType") == "subtitle"
                    and not caption.get("uncertain")
                    and caption.get("confidence") != "low"
                    and normalize_dialogue_text(caption.get("text")) == normalize_dialogue_text(raw_text)
                    and type(caption.get("startSec")) in {int, float}
                    and type(caption.get("endSec")) in {int, float}
                    and min(caption["endSec"], right, _block_end) > max(caption["startSec"], left, _block_start)
                    for scene in scenes for caption in scene.get("blocks") or []
                    if isinstance(caption, dict)
                )
                if not confirmed:
                    continue
            candidate = evidence_comparison_text(block.get("text"))
            if not boundary_match(reference, candidate):
                continue
            recovered_text = normalize_dialogue_text(raw_text)
            if not recovered_text:
                continue
            if recovered_text == block.get("text"):
                continue
            block.setdefault("evidenceRecoveryOriginal", {
                key: block[key] for key in ("text", "rawText", "sourceText", "finalText") if key in block
            })
            block["text"] = recovered_text
            if block.get("type") == "dialogue":
                block["rawText"] = raw_text
                block["finalText"] = recovered_text
                block["sourceText"] = raw_text
            block["evidenceRecovered"] = True
            block["subtitleRecovered"] = kind == "ocr"
            block["asrRecovered"] = kind == "audio"
            result["segments"] += 1
            result["blocks"] += 1
    return result


def episode_from_text(value: Any) -> int | None:
    """Extract an explicit episode number from a title or scene heading."""
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        number = int(text)
        return number if 0 < number <= 9999 else None
    # Uploads are often named simply ``2.mp4``, ``episode_02_final.mp4`` or
    # ``part-003.mov`` rather than using the Chinese “第N集” marker.  Accept an
    # isolated one-to-four digit filename token, while deliberately ignoring
    # long hash-like names and numbers embedded inside words.
    basename = re.split(r"[\\/]", text)[-1]
    stem = re.sub(r"\.[A-Za-z0-9]{1,8}$", "", basename)
    token = re.search(r"(?:^|[_\-.()\s])0*(\d{1,4})(?=$|[_\-.()\s])", stem)
    has_extension = bool(re.search(r"\.[A-Za-z0-9]{1,8}$", basename))
    date_like_name = bool(re.search(r"(?:^|[_\-.])\d{4}[-_.]\d{1,2}(?:[-_.]\d{1,2})?(?:$|[_\-.])", stem))
    if token and (has_extension or len(token.group(1)) <= 3) and not date_like_name:
        number = int(token.group(1))
        return number if 0 < number <= 9999 else None
    match = _EPISODE_MARKER_RE.search(text)
    if match:
        raw = match.group(1) or match.group(2)
        number = int(raw)
        return number if 0 < number <= 9999 else None
    match = _SCENE_NUMBER_RE.match(text)
    if match:
        number = int(match.group(1))
        return number if 0 < number <= 9999 else None
    return None


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


def speech_action_speaker(text: Any, characters: list[str]) -> str | None:
    """Return the named character immediately attached to a speech cue.

    This repairs the common vision-model error where an action explicitly says
    “甲开口/甲问” but the following dialogue is accidentally labelled 乙.  It
    deliberately requires a nearby explicit cue and never guesses from turn
    taking alone.
    """
    value = str(text or "")
    escaped = [re.escape(name) for name in characters if name]
    if not escaped:
        return None
    name_pattern = "(?:" + "|".join(sorted(escaped, key=len, reverse=True)) + ")"
    resolved: str | None = None
    for cue in _SPEECH_CUE_RE.finditer(value):
        sentence_start = max((value.rfind(mark, 0, cue.start()) for mark in "。！？；"), default=-1) + 1
        segment = value[sentence_start : cue.start()]
        # “甲对/向/看着乙说” and “甲转向乙，开口” retain 甲 as
        # the grammatical subject even though 乙 is nearer to the cue.
        directional = re.search(
            rf"({name_pattern}).{{0,4}}(?:对|向|冲着|看着|望着|转向)({name_pattern}).{{0,6}}$",
            segment,
        )
        if directional:
            resolved = directional.group(1)
            continue
        # In “甲听见乙说”, the embedded voice belongs to 乙.
        heard = re.search(rf"({name_pattern}).{{0,3}}(?:听见|听到)({name_pattern}).{{0,2}}$", segment)
        if heard:
            resolved = heard.group(2)
            continue
        attached = [
            (cue.start() - (sentence_start + match.end()), match.group(0))
            for match in re.finditer(name_pattern, value[sentence_start : cue.start()])
            if cue.start() - (sentence_start + match.end()) <= 2
        ]
        if attached:
            resolved = min(attached)[1]
            continue
        first = re.search(name_pattern, segment)
        if first:
            resolved = first.group(0)
    return resolved


def repair_dialogue_speakers(blocks: list[dict[str, Any]], characters: list[str]) -> None:
    """Reconcile dialogue labels with explicit lip-sync/action evidence."""
    previous_action = ""
    for block in blocks:
        if block.get("type") == "action":
            previous_action = str(block.get("text") or "")
            continue
        if block.get("type") != "dialogue":
            if block.get("type") not in {"screen_text", "sound"}:
                previous_action = ""
            continue
        current = character_label(block.get("speaker")) or "未知说话人"
        visible = character_label(block.get("visibleSpeaker"))
        mouth_moving = block.get("mouthMoving")
        # A reaction shot cannot transfer an ongoing utterance to its subject.
        # Conflicting continuity/lip-sync claims go to review instead.
        if block.get("speechContinuity") is True:
            previous_action = ""
            continue
        candidate = visible if visible in characters and mouth_moving is True else speech_action_speaker(previous_action, characters)
        if candidate and candidate != current:
            block["speakerCorrectedFrom"] = current
            block["speaker"] = candidate
            block["speakerCorrection"] = "visible_lip_sync" if candidate == visible else "preceding_speech_action"
            block["confidence"] = "high" if candidate == visible else "medium"
            # The deterministic mismatch has been resolved; unrelated dialogue
            # integrity findings, if any, remain reviewable.
            issues = [item for item in (block.get("dialogueIssues") or []) if item != "speaker_label_mismatch"]
            if issues:
                block["dialogueIssues"] = issues
            else:
                block.pop("dialogueIssues", None)
        previous_action = ""


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


def merge_continuous_scenes(scenes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Consolidate one continuous setting without changing any block content.

    Actions, speech and captions often overlap. Compare the next start with
    the whole accumulated interval, rather than requiring almost disjoint
    intervals. A backwards time reset, unknown setting or explicit transition
    is still a boundary. This also runs after additive repair restores its
    temporary transition placeholders.
    """
    def episode(scene: dict[str, Any]) -> int:
        return int(scene.get("_episodeOrder") or episode_from_text(scene.get("heading")) or 1)

    def interval(scene: dict[str, Any]) -> tuple[float, float] | None:
        start, end = scene.get("startSec"), scene.get("endSec")
        if (type(start) in {int, float} and type(end) in {int, float}
                and math.isfinite(start) and math.isfinite(end) and 0 <= start <= end):
            return start, end
        return None

    def mergeable(left: dict[str, Any], right: dict[str, Any]) -> bool:
        if episode(left) != episode(right) or any(
            left.get(key) != right.get(key)
            for key in ("location", "timeOfDay", "interiorExterior", "segmentType")
        ):
            return False
        same_parent = bool(left.get("continuityGroup") and
                           left.get("continuityGroup") == right.get("continuityGroup"))
        if not same_parent and (left.get("location") in {None, "", "待补充", "未知", "不明", "未标注地点"}
                or left.get("timeOfDay") in {None, "", "不明"}
                or left.get("interiorExterior") in {None, "", "不明"}):
            return False
        if any(block.get("type") == "transition" for scene in (left, right)
               for block in scene.get("blocks") or []):
            return False
        a, b = interval(left), interval(right)
        return bool(a and b and a[0] - 1.0 <= b[0] <= a[1] + 8.0)

    merged: list[dict[str, Any]] = []
    for scene in scenes:
        if merged and mergeable(merged[-1], scene):
            previous = merged[-1]
            previous["blocks"].extend(scene.get("blocks") or [])
            previous["characters"] = list(dict.fromkeys(
                (previous.get("characters") or []) + (scene.get("characters") or [])
            ))
            if isinstance(scene.get("props"), list):
                previous["props"] = list(previous.get("props") or []) + scene["props"]
            previous["startSec"] = min(previous["startSec"], scene["startSec"])
            previous["endSec"] = max(previous["endSec"], scene["endSec"])
            if previous.get("continuityGroup") != scene.get("continuityGroup"):
                previous.pop("continuityGroup", None)
        else:
            merged.append({**scene, "blocks": list(scene.get("blocks") or [])})
    counts: dict[int, int] = {}
    for scene in merged:
        scene_episode = episode(scene)
        counts[scene_episode] = counts.get(scene_episode, 0) + 1
        scene["heading"] = re.sub(r"^\s*\d+-\d+", f"{scene_episode}-{counts[scene_episode]}",
                                  str(scene.get("heading") or ""), count=1)
        scene["blocks"] = order_blocks_without_crossing_sentences(scene["blocks"])
        for key in ("_episodeOrder", "_sceneOrder", "_inputOrder"):
            scene.pop(key, None)
    return merged


def clean_action_text(text: Any) -> str:
    value = normalize_text(text)
    if not value:
        return ""
    value = re.sub(r"^(?:▲\s*)+", "", value).strip()
    # Camera language belongs to the editing plan, not the screenplay. Remove
    # it defensively because models occasionally echo it despite the prompt.
    value = _CAMERA_LANGUAGE_RE.sub("", value)
    value = dedupe_repeated_action_clauses(value)
    value = re.sub(r"\s*([，。；、])\s*", r"\1", value)
    value = re.sub(r"^[，。；、]+", "", value)
    value = re.sub(r"[，；、]{2,}", "，", value)
    # Do not delete a visible detail here.  A close-up reaction can be the only
    # evidence of a reveal or a decision, and earlier cleanup silently lost it.
    # ``compact_action_blocks`` may attach a purely microscopic detail to a
    # neighbouring beat, but it keeps the detail when there is no safe
    # neighbour.  This is intentionally lossless for arbitrary video genres.
    return normalize_text(value.strip(), sentence=True)


def dedupe_repeated_action_clauses(value: Any) -> str:
    """Remove exact repeated visual clauses without rewriting the action.

    Vision responses occasionally paste the same appearance sentence twice in
    one action. This only drops a later *identical* clause (six or more
    characters); different clothing in a flashback or a later time period is
    intentionally preserved.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    pieces = re.split(r"([\uFF0C\uFF1B\u3002\uFF01\uFF1F])", text)
    output: list[str] = []
    seen: set[str] = set()
    index = 0
    while index < len(pieces):
        clause = pieces[index].strip()
        delimiter = pieces[index + 1] if index + 1 < len(pieces) else ""
        key = re.sub(r"\s+", "", clause)
        duplicate = len(key) >= 6 and key in seen
        if not duplicate:
            if len(key) >= 6:
                seen.add(key)
            if clause:
                output.append(clause)
            if delimiter:
                output.append(delimiter)
        index += 2
    return "".join(output).strip("，；。！？ ")


def is_editorial_artifact(text: Any, block_type: str = "") -> bool:
    """Identify reference-document review notes accidentally returned as prose.

    The detector is deliberately narrow and only matches explicit meta wording
    (for example ``这里不需要展示`` or ``缺少某角色台词``).  Ordinary dialogue
    and subtitle text are left intact, even when they contain words such as
    “需要” or “这里”.
    """
    value = normalize_text(text).strip(" 【】")
    if not value or value in {"---", "———", "…"}:
        return value in {"---", "———"}
    if block_type == "screen_text":
        return False
    if _EDITORIAL_ARTIFACT_RE.match(value):
        return True
    # A review note often contains several labels in one line.  Only apply
    # this secondary check to generated action/dialogue/VO fields, never to
    # screen_text where the same words may be the actual overlay subtitle.
    if block_type != "screen_text" and re.search(
        r"(?:这里是|这里画面|这(?:两|三|几)句(?:话|台词)|少.+(?:说话|台词)|缺少.+(?:描述|台词|动作))",
        value,
    ):
        return True
    return False


def compact_performance(value: Any) -> str:
    """Keep dialogue parentheticals short and playable.

    Provider responses sometimes copy an entire prosody analysis into
    ``performance`` (tone, volume, pauses, emphasis, and repeated labels).
    《剧拆拆》 uses only a few useful emotion/action words in parentheses, so
    retain at most two concise clauses and discard technical narration.
    """
    text = normalize_text(value).strip("，、。！？；：: … ")
    if not text:
        return ""
    text = re.sub(r"^(?:语气|神态|情绪|声音|音量|重音|停顿)\s*[：:]\s*", "", text)
    # Providers sometimes separate every adjective with an ideographic comma
    # and repeat the same clause several times (for example
    # ``关切、关切、关切``).  Split on the complete Chinese punctuation set,
    # then compare whitespace-free clauses so punctuation/spacing cannot make
    # a duplicate survive the export.
    parts = [
        part.strip("，、。！？；：: … ")
        for part in re.split(r"[；;，、,。！？…]+", text)
        if part.strip("，、。！？；：: … ")
    ]
    kept: list[str] = []
    seen: set[str] = set()
    for part in parts:
        key = re.sub(r"\s+", "", part)
        if not key or key in seen:
            continue
        if len(part) > 18:
            # Prefer the first compact descriptive clause over a verbose
            # explanation such as "重音落在……、停顿……".
            part = part[:18].rstrip("，。！？；：: ")
        key = re.sub(r"\s+", "", part)
        if part and key not in seen:
            kept.append(part)
            seen.add(key)
        if len(kept) >= 2:
            break
    return "、".join(kept)


def is_micro_action(text: str) -> bool:
    """Return true for a non-causal close-up detail that should not be a beat."""
    major_candidate = text.replace("拳头", "")
    return bool(_MICRO_DETAIL_RE.search(text)) and not _MAJOR_ACTION_RE.search(major_candidate) and len(text) <= 100


def compact_action_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep each visible action beat as its own 剧拆拆-style ``▲`` line.

    Earlier versions collapsed adjacent actions into a prose paragraph. That
    loses the visual rhythm and makes subtitles/actions hard to compare with a
    source video, so normalization now only cleans/filter micro-details.
    """
    compacted: list[dict[str, Any]] = []
    for block in blocks:
        if block.get("type") != "action":
            compacted.append(block)
            continue
        text = clean_action_text(block.get("text"))
        if not text:
            continue
        block["text"] = text
        compacted.append(block)

    # A detail may precede the action it belongs to. Attach it to an adjacent
    # action instead of exposing a standalone “fingertips/eyes” block. If no
    # adjacent story action exists, keep it: dropping visible content makes the
    # transcript impossible to compare with the source video.
    for index, block in list(enumerate(compacted)):
        if block is None or block.get("type") != "action" or not is_micro_action(block.get("text", "")):
            continue
        merged = False
        if index + 1 < len(compacted) and compacted[index + 1] is not None and compacted[index + 1].get("type") == "action":
            next_block = compacted[index + 1]
            joiner = "" if block["text"].endswith(("，", "；", "：", "、", "。", "！", "？", "…")) else "，"
            next_block["text"] = normalize_text(f"{block['text']}{joiner}{next_block['text']}")
            merged = True
        elif index > 0 and compacted[index - 1] is not None and compacted[index - 1].get("type") == "action":
            previous = compacted[index - 1]
            joiner = "" if previous["text"].endswith(("，", "；", "：", "、", "。", "！", "？", "…")) else "，"
            previous["text"] = normalize_text(f"{previous['text']}{joiner}{block['text']}")
            merged = True
        if merged:
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


def add_character_visual_descriptions(blocks: list[dict[str, Any]], profiles: list[dict[str, Any]]) -> None:
    """Carry a character's visible identity into the first matching action.

    Providers are asked to repeat appearance details in the screenplay, but
    older responses often put them only in ``characterProfiles``.  Keep the
    profile as the source of truth and add its concrete details to the first
    action that names the character.  This makes the exported ``▲`` line
    usable on its own without inventing facts or adding a separate character
    table to the screenplay.
    """

    def clean_detail(value: Any) -> str:
        text = compact_character_detail(value).strip("。！？；; ")
        if not text or text in {"不明", "未知"} or re.search(r"待核对|待确认|待补充", text):
            return ""
        return text

    def details_for(profile: dict[str, Any]) -> tuple[str, list[str]]:
        name = str(profile.get("name") or "").strip()
        details = [
            clean_detail(profile.get("appearance")),
            clean_detail(profile.get("clothing")),
        ]
        details = list(dict.fromkeys(item for item in details if item and item != name))
        if not details:
            # Some legacy provider responses put the whole introduction in
            # firstAppearance. Use only the descriptive lead before the name,
            # rather than copying a complete action sentence into another one.
            first = clean_detail(profile.get("firstAppearance"))
            if name and name in first:
                first = first.split(name, 1)[0].rstrip("，、；：: ")
                first = re.sub(r"的$", "", first).strip()
            if first and first != name and len(first) <= 48:
                details = [first]
        return name, details

    character_details = [details_for(profile) for profile in profiles if isinstance(profile, dict)]
    character_details = [(name, details) for name, details in character_details if name]
    if not character_details:
        return

    introduced: set[str] = set()
    for block in blocks:
        if block.get("type") != "action":
            continue
        text = str(block.get("text") or "").strip()
        if not text:
            continue
        for name, details in character_details:
            if name in introduced or name not in text:
                continue
            if not details:
                introduced.add(name)
                continue
            missing = [detail for detail in details if detail not in text]
            if missing:
                descriptor = compact_character_detail("、".join(missing), max_chars=72)
                styled_with_suffix = False
                # Turn “梳双丸子头的小女孩” + “穿红碎花袄” into the
                # natural screenplay phrasing “梳双丸子头、穿红碎花袄的
                # 小女孩希希”.
                if len(missing) >= 2 and re.match(r"^(?:穿|身着|穿着)", missing[1]) and "的" in missing[0]:
                    action_lead, noun = missing[0].rsplit("的", 1)
                    descriptor = f"{action_lead}、{missing[1]}的{noun}"
                    styled_with_suffix = True
                name_position = text.find(name)
                # Keep the wording natural for appearance verbs such as
                # “梳/扎/穿/戴/编”; noun-like details read better directly
                # before the name (for example “小女孩希希”).
                needs_de = bool(re.search(r"(?:梳|扎|穿|戴|编|披|留|身着|穿着|戴着)", descriptor) or "、" in descriptor)
                styled_name = f"{descriptor}{'' if styled_with_suffix else ('的' if needs_de else '')}{name}"
                text = text[:name_position] + styled_name + text[name_position + len(name) :]
                block["text"] = normalize_text(text, sentence=True)
            introduced.add(name)


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
            value = character_label(value)
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

    def optional_bool(value: Any) -> bool | None:
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().lower()
        if normalized in {"1", "true", "yes", "是", "有", "同步"}:
            return True
        if normalized in {"0", "false", "no", "否", "无", "未动", "不同步"}:
            return False
        return None

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
                # ``type`` is a legacy alias for emotionType, but provider
                # blocks also use it for their structural kind (action,
                # dialogue, ...).  Never turn that structural value into an
                # unsupported emotion claim.
                if key == "type" and str(value.get("type") or "").strip().lower() in {
                    "action", "dialogue", "vo", "sound", "screen_text", "subtitle", "caption", "transition", "emotion"
                }:
                    continue
                text = compact_emotion_text(value.get(key), max_parts=2, max_chars=96)
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
            name = character_label(value.get("name")) or character_label(value.get("id"))
            if not name:
                continue
            aliases = names(value.get("aliases"))
            appearance = compact_character_detail(value.get("appearance"))
            clothing = compact_character_detail(value.get("clothing"))
            first_appearance = str(
                value.get("firstAppearance")
                or value.get("introduction")
                or value.get("intro")
                or ""
            ).strip()
            first_appearance = compact_character_detail(first_appearance, max_chars=72)
            if not first_appearance and (appearance or clothing):
                first_appearance = compact_character_detail("；".join(item for item in (appearance, clothing) if item), max_chars=72)
            row = {
                "id": str(value.get("id") or f"character_{index:03d}"),
                "name": name,
                "aliases": aliases,
                "appearance": appearance,
                "clothing": clothing,
                "firstAppearance": first_appearance,
            }
            for key in ("species", "nameEvidence", "identityEvidence"):
                if isinstance(value.get(key), str) and value[key].strip():
                    row[key] = normalize_text(value[key])
            relationships = []
            for relationship in value.get("relationships") or []:
                if not isinstance(relationship, dict):
                    continue
                target = character_label(relationship.get("target"))
                relation = character_label(relationship.get("relation"))
                if target and relation:
                    relationships.append({"target": target, "relation": relation})
            if relationships:
                row["relationships"] = relationships
            result.append(row)
        return result

    character_profiles = profiles(script.get("characterProfiles"))
    canonical_keys = {profile["name"].casefold() for profile in character_profiles}
    alias_claims: dict[str, set[str]] = {}
    for profile in character_profiles:
        canonical_name = profile["name"]
        alias_map[canonical_name.casefold()] = canonical_name
        for alias in profile.get("aliases", []):
            if alias.casefold() not in canonical_keys:
                alias_claims.setdefault(alias.casefold(), set()).add(canonical_name)
    alias_map.update({key: next(iter(owners)) for key, owners in alias_claims.items() if len(owners) == 1})

    def canonicalize_visible_names(text: Any) -> str:
        """Use canonical profile names in visible action prose as well as speakers.

        Alias normalization used to apply only to ``speaker`` and ``characters``.
        When an action used an alias, appearance injection missed the first
        introduction and the screenplay then mixed names across blocks.  Replace
        only multi-character aliases; pronouns and generic labels are too
        ambiguous to rewrite safely.
        """
        value = str(text or "")
        if script.get("characterNameScope") == "labels":
            return value
        # Match canonical names as well as aliases in one pass. A name such
        # as "苏婉晴（小青）" already contains its alias; replacing "小青"
        # inside it again used to grow a new nested name on every review.
        visible_labels = dict(alias_map)
        for label, canonical in alias_map.items():
            visible_labels.setdefault(unicodedata.normalize("NFKC", label).casefold(), canonical)
        # Repair expansion left in older stored results as well. Derive the
        # exact malformed wrapper from a known alias/profile pair, touching
        # action prose only and leaving spoken repetitions unchanged.
        for label, canonical in visible_labels.items():
            canonical_text = unicodedata.normalize("NFKC", canonical)
            canonical_key = canonical_text.casefold()
            if len(label) < 2 or label == canonical_key or canonical_key.count(label) != 1:
                continue
            position = canonical_key.index(label)
            prefix, suffix = canonical_text[:position], canonical_text[position + len(label):]
            if not prefix and not suffix:
                continue
            expanded = re.compile(
                re.escape(prefix) + re.escape(canonical_text) + re.escape(suffix), re.IGNORECASE,
            )
            for _ in range(16):
                repaired = expanded.sub(lambda _: canonical_text, unicodedata.normalize("NFKC", value))
                if repaired == value:
                    break
                value = repaired
        labels = sorted((label for label in visible_labels if len(label) >= 2), key=len, reverse=True)
        if not labels:
            return value
        return re.sub(
            "|".join(re.escape(label) for label in labels),
            lambda match: visible_labels[match.group(0).casefold()],
            value,
            flags=re.IGNORECASE,
        )

    def dedupe_character_names(text: Any) -> str:
        """Collapse adjacent repetitions of known character names.

        Vision responses occasionally return prose such as ``周周周景明``
        or ``周景明周景明`` after a scene was split. These are not source
        words and make the exported screenplay look corrupted. Only known
        profile names are touched, and only when repeats are contiguous;
        ordinary dialogue and legitimate names such as ``杨洋`` remain intact.
        """
        value = str(text or "")
        known = sorted(
            {
                str(name).strip()
                for name in alias_map.values()
                if len(str(name).strip()) >= 2
            },
            key=len,
            reverse=True,
        )
        for name in known:
            value = re.sub(rf"(?:{re.escape(name)}){{2,}}", name, value)
            prefix = name[:1]
            rest = name[1:]
            if rest:
                value = re.sub(rf"(?:{re.escape(prefix)}){{2,}}{re.escape(rest)}", name, value)
        return value

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

    # The upload title is authoritative when it contains an episode marker.
    # Legacy/provider payloads may omit it from the title but still emit
    # headings such as ``2-1``; use that as a safe fallback before defaulting
    # to episode one.
    title_episode = episode_from_text(title) or episode_from_text(script.get("title"))
    episode_number = title_episode
    if episode_number is None:
        raw_episode = script.get("episodeNumber") or script.get("episode")
        try:
            parsed_episode = int(raw_episode)
        except (TypeError, ValueError):
            parsed_episode = 0
        if 0 < parsed_episode <= 9999:
            episode_number = parsed_episode
    if episode_number is None:
        for candidate in script.get("scenes") or []:
            if not isinstance(candidate, dict):
                continue
            episode_number = episode_from_text(candidate.get("heading"))
            if episode_number is not None:
                break
    episode_number = episode_number or 1

    # A batch upload can contain several episodes even when the upload title
    # carries the first episode marker.  Preserve explicit ``N-M`` prefixes in
    # that case; for a single-episode file the title remains authoritative.
    heading_episodes = {
        episode_from_text(item.get("heading"))
        for item in (script.get("scenes") or [])
        if isinstance(item, dict) and episode_from_text(item.get("heading")) is not None
    }
    multi_episode = len(heading_episodes) > 1

    def scene_heading(raw_scene: dict[str, Any], index: int, location: str, scene_episode: int) -> str:
        raw_heading = str(raw_scene.get("heading") or "").strip()
        valid = re.match(
            r"^\s*\d+-\d+\s+(?:日|夜|清晨|黄昏|傍晚|傍晚转夜|日转夜|不明)\s+"
            r"(?:内|外|内外|内转外|外转内|不明)\s+.+",
            raw_heading,
        )
        if valid:
            # Keep the provider's time/place wording, but always make the
            # episode-scene prefix deterministic and scoped to this episode.
            return re.sub(
                r"^\s*\d+-\d+",
                f"{scene_episode}-{index}",
                raw_heading,
                count=1,
            )
        time_of_day = normalize_time_of_day(raw_scene.get("timeOfDay"))
        interior_exterior = normalize_interior_exterior(raw_scene.get("interiorExterior"))
        heading_location = location if location != "待补充" else (raw_heading or "未标注地点")
        return f"{scene_episode}-{index} {time_of_day} {interior_exterior} {heading_location}"

    def normalize_time_of_day(value: Any) -> str:
        text = str(value or "不明").strip()
        return text if text in {"日", "夜", "清晨", "黄昏", "傍晚", "傍晚转夜", "日转夜", "不明"} else "不明"

    def normalize_interior_exterior(value: Any) -> str:
        text = str(value or "不明").strip()
        return text if text in {"内", "外", "内外", "内转外", "外转内", "不明"} else "不明"

    scenes: list[dict[str, Any]] = []
    for index, raw_scene in enumerate(script["scenes"], start=1):
        if not isinstance(raw_scene, dict):
            continue
        raw_segment_type = str(raw_scene.get("segmentType") or "main").strip().lower()
        raw_heading = str(raw_scene.get("heading") or "").strip()
        heading_episode = episode_from_text(raw_heading)
        scene_episode = heading_episode if multi_episode and heading_episode is not None else episode_number
        scene_number_match = _SCENE_NUMBER_RE.match(raw_heading)
        raw_scene_number = int(scene_number_match.group(2)) if scene_number_match else None
        # Segment labels are model judgements, not permission to erase source
        # content. Recaps can contain most of an uploaded episode. Preserve
        # observed action/speech even when the provider labels it non-plot.
        blocks: list[dict[str, Any]] = []
        heading_parts = re.match(
            r"^\s*\d+-\d+\s+(日|夜|清晨|黄昏|傍晚|傍晚转夜|日转夜|不明)\s+"
            r"(内|外|内外|内转外|外转内|不明)\s+(.+)$", raw_heading,
        )
        scene_location = str(raw_scene.get("location") or (heading_parts.group(3) if heading_parts else "待补充")).strip()
        scene_time = raw_scene.get("timeOfDay") or (heading_parts.group(1) if heading_parts else None)
        scene_interior = raw_scene.get("interiorExterior") or (heading_parts.group(2) if heading_parts else None)
        raw_blocks: list[Any] = []
        if raw_scene.get("sound"):
            raw_blocks.extend(
                {"type": "sound", "text": item if isinstance(item, str) else item.get("text", "")}
                for item in (raw_scene["sound"] if isinstance(raw_scene["sound"], list) else [raw_scene["sound"]])
            )
        if raw_scene.get("emotion"):
            # Standalone emotion notes are visual prose in 剧拆拆 output.
            raw_blocks.append({"type": "action", "text": raw_scene["emotion"]})
        raw_blocks.extend(raw_scene.get("blocks") or [])
        for raw_block in raw_blocks:
            if not isinstance(raw_block, dict):
                continue
            text = strip_internal_review_markers(raw_block.get("text") or raw_block.get("description") or "")
            if not text:
                continue
            raw_type = str(raw_block.get("type") or "action").strip().lower()
            editorial_type = "screen_text" if raw_type in {"screen_text", "subtitle", "caption", "system", "title_card"} else raw_type
            if is_editorial_artifact(text, editorial_type):
                continue
            if editorial_type == "screen_text":
                wrapped_text = re.match(r"^【(?:字幕|画面文字|屏幕文字)[：:](.+?)】(?:\s*(.*))?$", text)
                if wrapped_text and (not wrapped_text.group(2) or is_editorial_artifact(wrapped_text.group(2), "action")):
                    text = wrapped_text.group(1).strip()
            overlay_match = re.match(
                r"^【(?:字幕|画面文字|屏幕文字|标题|系统提示)[：:](.+?)】$",
                text,
            )
            if overlay_match and raw_type in {"action", "visual", "text"}:
                raw_type = "screen_text"
                text = overlay_match.group(1).strip()
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
                "emotion": "action",
            }.get(raw_type, raw_type)
            raw_voice_kind = str(raw_block.get("voKind") or raw_block.get("voiceType") or "").strip().casefold()
            voice_source = str(raw_block.get("voiceSource") or "").strip().casefold()
            mouth_moving = optional_bool(raw_block.get("mouthMoving", raw_block.get("lipSync")))
            forced_os = (
                raw_type in {"os", "inner_monologue"}
                or raw_voice_kind in {"os", "inner_monologue", "inner-monologue", "monologue", "thought"}
                or voice_source in {"os", "inner", "internal", "inner_monologue"}
            )
            explicit_voice_kind = _VO_KIND_ALIASES.get(raw_voice_kind, _VO_KIND_ALIASES.get(voice_source))
            continuity = optional_bool(raw_block.get("speechContinuity"))
            if block_type == "dialogue" and (forced_os or explicit_voice_kind):
                block_type = "vo"
            if block_type not in {"action", "dialogue", "vo", "sound", "emotion", "screen_text", "transition"}:
                block_type = "action"
            if block_type == "action":
                text = clean_action_text(dedupe_character_names(canonicalize_visible_names(text)))
            elif block_type == "dialogue":
                # Never merge or paraphrase dialogue while normalizing it.
                # When a provider returns both verbatim and polished fields,
                # the verbatim one is authoritative.  The display field may
                # only restore punctuation; it must never replace the words.
                source_text = (
                    raw_block.get("sourceText")
                    or raw_block.get("verbatimText")
                    or raw_block.get("rawText")
                )
                dialogue_source = source_text or raw_block.get("finalText") or text
                text = normalize_dialogue_text(strip_internal_review_markers(dialogue_source))
            elif block_type == "vo":
                voice_text = (
                    raw_block.get("sourceText")
                    or raw_block.get("verbatimText")
                    or raw_block.get("rawText")
                    or text
                )
                text = normalize_dialogue_text(strip_internal_review_markers(voice_text))
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
                block["emotion"] = compact_emotion_text(raw_block.get("emotion"), max_parts=2, max_chars=72)
                block.update(emotion_fields(raw_block))
                performance_parts = [
                    raw_block.get("performance") or raw_block.get("acting") or raw_block.get("delivery"),
                    raw_block.get("emotion"),
                    raw_block.get("emotionType"),
                    raw_block.get("emotionChange"),
                ]
                performance = compact_performance("、".join(
                    value
                    for value in (
                        compact_emotion_text(item, max_parts=2, max_chars=36)
                        for item in performance_parts
                    )
                    if value
                ))
                if performance:
                    block["performance"] = performance
            if block_type == "dialogue":
                speaker = dedupe_character_names(character_label(raw_block.get("speaker")) or "未知说话人")
                block["speaker"] = alias_map.get(speaker.casefold(), speaker)
                block["rawText"] = str(source_text or dialogue_source).strip()
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
                # Keep technical emotion fields for QA, but also preserve one
                # short playable cue in the screenplay. Earlier versions
                # discarded these fields entirely, making valid emotion
                # evidence disappear from the user's export.
                block["emotion"] = ""
                block["emotionImportant"] = False
                block.update(emotion_fields(raw_block))
                # Keep only the short playable emotion cue. Technical
                # prosody fields (volume/pause/emphasis) belong to QA data,
                # not to the screenplay parenthetical.
                performance_parts = [
                    raw_block.get("performance") or raw_block.get("acting") or raw_block.get("delivery"),
                    raw_block.get("speechTone"),
                    raw_block.get("emotion"),
                    raw_block.get("emotionType"),
                    raw_block.get("emotionChange"),
                ]
                performance = compact_performance("、".join(
                    value for value in (compact_emotion_text(item, max_parts=2, max_chars=36) for item in performance_parts) if value
                ))
                if performance:
                    block["performance"] = performance
            elif block_type == "vo":
                speaker = dedupe_character_names(character_label(raw_block.get("speaker")) or ("旁白" if raw_type in {"narration", "voiceover", "voice_over"} else "未知说话人"))
                speaker = alias_map.get(speaker.casefold(), speaker)
                block["speaker"] = speaker
                block["voKind"] = "os" if forced_os else (explicit_voice_kind or (
                    "unknown" if mouth_moving is False and not raw_voice_kind else
                    normalize_vo_kind(raw_block.get("voKind") or raw_block.get("voiceType"), raw_type, speaker)
                ))
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
                block["screenType"] = str(raw_block.get("screenType") or ("subtitle" if raw_type in {"screen_text", "subtitle", "caption"} else raw_type)).strip()
            elif block_type == "transition":
                block["transitionType"] = str(raw_block.get("transitionType") or raw_type or "other").strip()
            elif block_type == "emotion":
                block.update(emotion_fields(raw_block))
            if block_type in {"dialogue", "vo"}:
                for key in ("evidenceRecovered", "subtitleRecovered", "asrRecovered"):
                    if raw_block.get(key) is True:
                        block[key] = True
                if isinstance(raw_block.get("evidenceRecoveryOriginal"), dict):
                    block["evidenceRecoveryOriginal"] = dict(raw_block["evidenceRecoveryOriginal"])
                if raw_block.get("textSource") in {"subtitle", "audio"}:
                    block["textSource"] = raw_block["textSource"]
                audio_unclear = optional_bool(raw_block.get("audioUnclear"))
                if audio_unclear is not None:
                    block["audioUnclear"] = audio_unclear
                clarity = str(raw_block.get("speechClarity") or "").strip().casefold()
                if clarity in {"clear", "unclear", "inaudible", "unintelligible"}:
                    block["speechClarity"] = clarity
                visible_speaker = dedupe_character_names(character_label(raw_block.get("visibleSpeaker")))
                if visible_speaker:
                    block["visibleSpeaker"] = alias_map.get(visible_speaker.casefold(), visible_speaker)
                if mouth_moving is not None:
                    block["mouthMoving"] = mouth_moving
                if continuity is not None:
                    block["speechContinuity"] = continuity
                if voice_source:
                    block["voiceSource"] = voice_source
                if isinstance(raw_block.get("speakerEvidence"), str) and raw_block["speakerEvidence"].strip():
                    block["speakerEvidence"] = normalize_text(raw_block["speakerEvidence"])
            if block_type == "action":
                action_object = normalize_text(raw_block.get("object") or raw_block.get("target"))
                action_result = normalize_text(raw_block.get("result") or raw_block.get("impact"))
                if action_object:
                    block["object"] = action_object
                if action_result:
                    block["result"] = action_result
                block["confidence"] = str(raw_block.get("confidence") or "medium").strip().lower()
                block["uncertain"] = truthy(raw_block.get("uncertain")) or block["confidence"] == "low"
                block["inferred"] = truthy(raw_block.get("inferred"))
                # Missing object/result fields are exactly the kind of visual
                # gap the quality gate is meant to surface.  Keep the action
                # text, but make the uncertainty visible in the screenplay.
                if not action_object or not action_result:
                    block["uncertain"] = True
            elif block_type in {"screen_text", "transition"}:
                block["confidence"] = str(raw_block.get("confidence") or "medium").strip().lower()
                block["uncertain"] = truthy(raw_block.get("uncertain")) or block["confidence"] == "low"
            if block_type == "dialogue" and "mixed_speakers" in block.get("dialogueIssues", []):
                blocks.extend(split_explicitly_mixed_dialogue(block))
            else:
                blocks.append(block)
        for block in blocks:
            if block.get("type") == "dialogue":
                block["speaker"] = alias_map.get(str(block.get("speaker") or "").casefold(), block.get("speaker"))
                block.pop("_dialogueSource", None)
        blocks = order_blocks_without_crossing_sentences(blocks)
        known_speakers = list(dict.fromkeys(
            names(raw_scene.get("characters"))
            + names(script.get("characters"))
            + [str(profile.get("name") or "").strip() for profile in character_profiles]
        ))
        repair_dialogue_speakers(blocks, [name for name in known_speakers if name])
        blocks = compact_action_blocks(blocks)
        raw_scene_characters = names(raw_scene.get("characters"))
        all_profile_names = [
            str(profile.get("name") or "").strip()
            for profile in character_profiles
            if str(profile.get("name") or "").strip()
        ]
        block_text = " ".join(str(block.get("text") or "") for block in blocks)
        # Do not copy a provider's global cast list into every scene. Keep a
        # character only when the scene's visual/dialogue text actually
        # mentions them; this removes the "导演、全体工作人员、工人..." pileup
        # seen in the comparison sample while retaining action-only roles.
        candidate_characters = list(dict.fromkeys(raw_scene_characters + names(script.get("characters")) + all_profile_names))
        scene_characters = [name for name in candidate_characters if name and name in block_text]
        fallback_characters = scene_characters + names(script.get("characters")) + [
            str(profile.get("name") or "").strip()
            for profile in character_profiles
            if str(profile.get("name") or "").strip()
        ]
        repair_action_subjects(blocks, list(dict.fromkeys(fallback_characters)))
        add_character_visual_descriptions(blocks, character_profiles)
        known_for_scene = [name for name in dict.fromkeys(fallback_characters) if name]
        if known_for_scene:
            for block in blocks:
                if block.get("type") != "action":
                    continue
                text_value = str(block.get("text") or "")
                if not any(name in text_value for name in known_for_scene) and not text_value.startswith("未知人物"):
                    block["uncertain"] = True
        block_text = " ".join(str(block.get("text") or "") for block in blocks)
        for name in candidate_characters:
            if name and name in block_text and name not in scene_characters:
                scene_characters.append(name)
        for block in blocks:
            speaker = str(block.get("speaker") or "").strip()
            if block.get("type") in {"dialogue", "vo"} and speaker and speaker not in {"旁白", "未知说话人", "OS", "内心独白"}:
                if speaker not in scene_characters:
                    scene_characters.append(speaker)
        scene: dict[str, Any] = {
            "id": str(raw_scene.get("id") or f"scene_{index:03d}"),
            "heading": scene_heading(raw_scene, raw_scene_number or index, scene_location, scene_episode),
            "location": scene_location,
            "timeOfDay": normalize_time_of_day(scene_time),
            "interiorExterior": normalize_interior_exterior(scene_interior),
            "segmentType": raw_segment_type,
            "characters": scene_characters,
            # The visible screenplay is a visual transcript.  Do not expose
            # model-written scene goals, summaries, or environment analysis.
            "environment": "",
            "blocks": blocks,
        }
        # Internal ordering keys are removed after sorting.  A scene number from
        # the provider wins over a timestamp because timestamps can reset for
        # each file in a multi-episode upload.
        scene["_episodeOrder"] = scene_episode
        scene["_sceneOrder"] = raw_scene_number
        scene["_inputOrder"] = index
        if isinstance(raw_scene.get("continuityGroup"), str) and raw_scene["continuityGroup"]:
            scene["continuityGroup"] = raw_scene["continuityGroup"]
        raw_props = raw_scene.get("props") or raw_scene.get("propsState") or raw_scene.get("items")
        if isinstance(raw_props, (list, dict)):
            # Keep only evidence-oriented item state; do not synthesize an
            # item's location or ownership when the provider did not observe it.
            scene["props"] = raw_props if isinstance(raw_props, list) else [raw_props]
        scene_start = number(raw_scene.get("startSec"))
        scene_end = number(raw_scene.get("endSec"))
        if scene_start is None:
            scene_start = min((block["startSec"] for block in blocks if "startSec" in block), default=None)
        if scene_end is None:
            scene_end = max((block["endSec"] for block in blocks if "endSec" in block), default=None)
        if scene_start is not None:
            scene["startSec"] = scene_start
        if scene_end is not None:
            scene["endSec"] = scene_end
        if blocks:
            scenes.append(scene)
    if not scenes:
        raise ArkError("未生成可用剧本正文，已保留原始视频和已有版本。", provider_code="empty_screenplay")
    # Sort batches by episode first.  Within an episode retain the provider's
    # explicit scene number when present; otherwise use timestamps, then input
    # order.  The old global timestamp sort could move episode 10 before episode
    # 2 and then renumber every scene as ``1-N``.
    def scene_sort_key(scene: dict[str, Any]) -> tuple[Any, ...]:
        episode = int(scene.get("_episodeOrder") or episode_number)
        scene_no = scene.get("_sceneOrder")
        start = scene.get("startSec")
        return (
            episode,
            0 if scene_no is not None else 1,
            int(scene_no) if scene_no is not None else (start if start is not None else float("inf")),
            start if start is not None else float("inf"),
            int(scene.get("_inputOrder") or 0),
        )

    scenes = sorted(scenes, key=scene_sort_key)
    scenes = merge_continuous_scenes(scenes)
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
    result = {
        "version": str(script.get("version") or "1.0"),
        **({"characterNameScope": "labels"} if script.get("characterNameScope") == "labels" else {}),
        "episodeNumber": episode_number,
        # The task title already carries an episode marker when the source file
        # is numbered. It is authoritative for the screenplay/export title.
        "title": str(title or script.get("title") or "未命名视频"),
        "eventChain": event_chain,
        "settingRules": setting_rules,
        "characters": used_characters or names(script.get("characters")),
        "characterProfiles": used_profiles,
        "scenes": scenes,
    }
    suppress_redundant_subtitles(result)
    return result


_GENERIC_CHARACTER_LABELS = {
    "旁白", "未知说话人", "未知男声", "未知女声", "人物", "男主", "女主",
    "主角", "配角", "路人", "群众", "工作人员", "全体", "os", "vo",
    "内心独白",
}


def _character_key(value: Any) -> str:
    """Return a conservative comparison key for a character label."""
    text = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    text = re.sub(r"[（(].*?[）)]", "", text)
    text = re.sub(r"[\s·•、，,。！？!?：:；;\[\]【】]+", "", text)
    return text


def _is_generic_character(value: Any) -> bool:
    return _character_key(value) in {_character_key(item) for item in _GENERIC_CHARACTER_LABELS}


def _script_character_names(script: dict[str, Any], *, include_generic: bool = False) -> list[str]:
    """Collect visible character labels in stable first-seen order."""
    values: list[str] = []

    def add(value: Any) -> None:
        text = character_label(value)
        if text and (include_generic or not _is_generic_character(text)) and text not in values:
            values.append(text)

    for value in script.get("characters") or []:
        add(value)
    for profile in script.get("characterProfiles") or []:
        if isinstance(profile, dict):
            add(profile.get("name"))
    for scene in script.get("scenes") or []:
        if not isinstance(scene, dict):
            continue
        for value in scene.get("characters") or []:
            add(value)
        for block in scene.get("blocks") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") in {"dialogue", "vo"}:
                add(block.get("speaker"))
                add(block.get("visibleSpeaker"))
    return values


def _replace_cast_labels(script: dict[str, Any], mapping: dict[str, str]) -> None:
    """Rewrite structured identity labels, never source dialogue/action text."""
    def replace(value: Any) -> Any:
        return mapping.get(str(value).strip(), value) if value is not None else value

    script["characterNameScope"] = "labels"
    script["characters"] = list(dict.fromkeys(replace(v) for v in script.get("characters") or []))
    profiles: dict[str, dict[str, Any]] = {}
    for original in script.get("characterProfiles") or []:
        if not isinstance(original, dict):
            continue
        profile = dict(original)
        old = character_label(profile.get("name"))
        profile["name"] = replace(old)
        profile["aliases"] = list(dict.fromkeys(
            list(profile.get("aliases") or []) + ([old] if old != profile["name"] else [])
        ))
        if profile.get("relationships"):
            profile["relationships"] = [
                {**r, "target": replace(r.get("target"))}
                for r in profile["relationships"] if isinstance(r, dict)
            ]
        existing = profiles.get(profile["name"])
        if existing:
            existing["aliases"] = list(dict.fromkeys(existing["aliases"] + profile["aliases"]))
            for field in ("appearance", "clothing", "firstAppearance", "nameEvidence", "identityEvidence"):
                if not existing.get(field) and profile.get(field):
                    existing[field] = profile[field]
        else:
            profiles[profile["name"]] = profile
    script["characterProfiles"] = list(profiles.values())
    for scene in script.get("scenes") or []:
        if not isinstance(scene, dict):
            continue
        scene["characters"] = list(dict.fromkeys(replace(v) for v in scene.get("characters") or []))
        for block in scene.get("blocks") or []:
            if isinstance(block, dict):
                for field in ("speaker", "visibleSpeaker"):
                    if field in block:
                        block[field] = replace(block[field])


def _backfill_evidenced_cast_aliases(scripts: list[dict[str, Any]]) -> int:
    """Later explicit naming evidence may resolve earlier roles/honorifics.

    Do not treat an alias list, shared clothes, or a shared surname as proof.
    Different full names remain distinct; ambiguous aliases remain unresolved.
    """
    owners: dict[str, set[str]] = {}
    supported: set[tuple[str, str]] = set()
    for script in scripts:
        if not isinstance(script, dict):
            continue
        for profile in script.get("characterProfiles") or []:
            if not isinstance(profile, dict):
                continue
            name = character_label(profile.get("name"))
            if (not re.fullmatch(r"[\u4e00-\u9fff]{2,4}", name) or _is_generic_character(name)
                    or re.search(r"先生$|女士$|小姐$|少爷$|夫人$|总裁$", name)):
                continue
            evidence = str(profile.get("nameEvidence") or "")
            for alias in profile.get("aliases") or []:
                alias = character_label(alias)
                if not alias or alias == name:
                    continue
                if alias not in {"男主", "女主", "主角"} and not re.search(r"先生$|女士$|小姐$|少爷$|夫人$|总裁$", alias):
                    continue
                owners.setdefault(alias, set()).add(name)
                if name in evidence and alias in evidence and not re.search(r"待核|待确认|疑似|猜测|暂定|可能", evidence):
                    supported.add((alias, name))
    mapping = {alias: next(iter(names)) for alias, names in owners.items()
               if len(names) == 1 and (alias, next(iter(names))) in supported}
    changes = 0
    for script in scripts:
        if not isinstance(script, dict):
            continue
        local = {label: mapping[label] for label in _script_character_names(script, include_generic=True)
                 if label in mapping}
        if local:
            _replace_cast_labels(script, local)
            changes += len(local)
    return changes


def apply_batch_character_continuity(scripts: list[dict[str, Any]]) -> int:
    """Accumulate each character's first established name in episode order.

    Each video is recognized independently, so a provider may call the same
    person ``江川`` in episode one, ``男主`` in episode two, and ``江先生`` in
    episode three.  This pass is intentionally deterministic and conservative:
    exact aliases and unique shortened names are
    remapped; ambiguous or genuinely new characters are left alone.  It
    mutates the supplied result objects and returns the number of changed
    labels. All block text remains verbatim; only cast/speaker labels use the
    canonical names. Profiles retain identity aliases as internal metadata.
    """
    if not isinstance(scripts, list) or len(scripts) < 2:
        return 0
    backfilled = _backfill_evidenced_cast_aliases(scripts)
    first = scripts[0] if isinstance(scripts[0], dict) else None
    if not first:
        return 0
    canonical_names = _script_character_names(first)

    alias_map: dict[str, str] = {}
    ambiguous_aliases: set[str] = set()
    # A later episode may use only a kinship label ("丈夫") for a person
    # already named in an earlier episode. Require the same named counterpart
    # and the same explicit relationship; a bare role cannot identify anyone.
    relationship_owners: dict[tuple[str, str], set[str]] = {}
    kinship_relations = {
        "丈夫": {"夫妻", "丈夫", "老公"}, "老公": {"夫妻", "丈夫", "老公"},
        "妻子": {"夫妻", "妻子", "老婆"}, "老婆": {"夫妻", "妻子", "老婆"},
        "父亲": {"父亲", "父子", "父女"}, "爸爸": {"父亲", "父子", "父女"},
        "爸": {"父亲", "父子", "父女"}, "爹": {"父亲", "父子", "父女"},
        "母亲": {"母亲", "母子", "母女"}, "妈妈": {"母亲", "母子", "母女"},
        "妈": {"母亲", "母子", "母女"}, "娘": {"母亲", "母子", "母女"},
    }
    relation_suffixes = {
        "父亲": ("父亲", "爸爸", "爸", "爹"),
        "母亲": ("母亲", "妈妈", "妈", "娘"),
        "哥哥": ("哥哥", "哥"), "姐姐": ("姐姐", "姐"),
        "弟弟": ("弟弟", "弟"), "妹妹": ("妹妹", "妹"),
        "妻子": ("妻子", "老婆"), "丈夫": ("丈夫", "老公"),
        "儿子": ("儿子",), "女儿": ("女儿",),
    }
    relation_pattern = re.compile(r"(?:父亲|母亲|爸爸|妈妈|爸|妈|爹|娘|妻子|丈夫|老婆|老公|儿子|女儿|哥哥|姐姐|弟弟|妹妹)$")

    def claim(label: Any, canonical: str) -> None:
        key = _character_key(character_label(label))
        if not key or key in ambiguous_aliases or _is_generic_character(label):
            return
        if key in alias_map and alias_map[key] != canonical:
            alias_map.pop(key)
            ambiguous_aliases.add(key)
        else:
            alias_map[key] = canonical

    def register(script: dict[str, Any]) -> None:
        for name in _script_character_names(script):
            if name not in canonical_names:
                canonical_names.append(name)
            claim(name, name)
        for profile in script.get("characterProfiles") or []:
            if not isinstance(profile, dict):
                continue
            canonical = character_label(profile.get("name"))
            if not canonical:
                continue
            for alias in profile.get("aliases") or []:
                claim(alias, canonical)
            for relationship in profile.get("relationships") or []:
                if not isinstance(relationship, dict):
                    continue
                target = character_label(relationship.get("target"))
                relation = character_label(relationship.get("relation"))
                if target in canonical_names and canonical != target and relation:
                    signature = (_character_key(target), _character_key(relation))
                    relationship_owners.setdefault(signature, set()).add(canonical)
                suffixes = next((values for values in relation_suffixes.values() if relation in values), ())
                if not target or not suffixes:
                    continue
                target_labels = {target}
                if re.fullmatch(r"[\u4e00-\u9fff]{3}", target):
                    target_labels.add(target[1:])
                for target_label in target_labels:
                    for suffix in suffixes:
                        claim(target_label + suffix, canonical)
                        claim(target_label + "的" + suffix, canonical)

    register(first)

    # Appearance labels are useful identity evidence when later episodes
    # replace a named character with a role description (for example
    # ``校服小女孩``). Keep the match conservative: one unique distinctive
    # phrase or two shared concrete descriptors are required.
    identity_evidence: dict[str, list[str]] = {}

    def identity_text(value: Any) -> str:
        text = unicodedata.normalize("NFKC", str(value or "")).casefold()
        # Clothing/role descriptions vary mostly by connective words. Remove
        # those words before comparing, while retaining concrete descriptors.
        return re.sub(r"[\s的地得着穿戴一名个位之与及和、，。！？!?：:；;（）()【】\[\]]+", "", text)

    def meaningful_identity(value: Any) -> bool:
        """Ignore generated placeholders that are shared by every profile."""
        text = identity_text(value)
        if not text:
            return False
        return not any(marker in text for marker in (
            "待核对", "待补充", "待确认", "未知", "不明", "首登外观服装",
            "首登外观", "外观服装待核对",
        ))

    def identity_tokens(value: Any) -> set[str]:
        text = unicodedata.normalize("NFKC", str(value or "")).casefold()
        tokens = set(re.findall(r"[\u4e00-\u9fff]{2,}", text))
        generic = {
            "人物", "角色", "男子", "女人", "男人", "女孩", "男孩", "小女孩", "小男孩",
            "老人", "老妇人", "年轻人", "中年人", "身影", "穿着", "看起来",
        }
        return {token for token in tokens if token not in generic}

    def add_identity_evidence(script: dict[str, Any], mapping: dict[str, str] | None = None) -> None:
        for profile in script.get("characterProfiles") or []:
            if not isinstance(profile, dict):
                continue
            name = character_label(profile.get("name"))
            canonical = (mapping or {}).get(name, name)
            if not canonical:
                continue
            values = [name]
            values.extend(
                value for value in (
                    str(profile.get(key) or "")
                    for key in ("appearance", "clothing", "firstAppearance", "identityEvidence")
                )
                if meaningful_identity(value)
            )
            values.extend(
                str(alias) for alias in profile.get("aliases") or []
                if character_label(alias) and meaningful_identity(alias)
            )
            identity_evidence.setdefault(canonical, []).extend(value for value in values if value)

    add_identity_evidence(first)

    def resolve_by_appearance(candidate: str, profile: dict[str, Any]) -> str | None:
        if not isinstance(profile, dict) or not identity_evidence:
            return None
        # A relationship label may contain a known character name (for
        # example ``周向东父亲``).  Do not let that substring become an
        # appearance match; relationship labels need an explicit counterpart
        # and relation before they can be resolved.
        if relation_pattern.search(_character_key(candidate)):
            return None
        # Appearance can reconcile descriptive role labels, not establish that
        # two different proper names are one person. Clothing alone is shared
        # by many real characters, especially school/work uniforms.
        if not re.search(r"男主|女主|主角|男子|女子|男生|女生|男人|女人|男孩|女孩|老人|老头|老太|妇人|大叔|阿姨|小伙|少年|少女|老师|医生|老板|校服|[红白黑蓝绿黄紫]衣", candidate):
            return None
        candidate_values = [candidate] + [
            str(profile.get(key) or "")
            for key in ("appearance", "clothing", "firstAppearance", "identityEvidence")
            if meaningful_identity(profile.get(key))
        ]
        if not any(len(identity_text(value)) >= 4 for value in candidate_values):
            return None
        candidate_joined = "".join(identity_text(value) for value in candidate_values if value)
        candidate_tokens = set().union(*(identity_tokens(value) for value in candidate_values if value))
        ranked: list[tuple[int, str]] = []
        for canonical, values in identity_evidence.items():
            evidence_joined = "".join(identity_text(value) for value in values if value)
            evidence_tokens = set().union(*(identity_tokens(value) for value in values if value))
            score = 0
            # Removing connective words turns "穿校服的小女孩" and
            # "校服小女孩" into the same distinctive phrase.
            for value in candidate_values:
                compact = identity_text(value)
                if len(compact) >= 5 and (compact in evidence_joined or evidence_joined in compact):
                    score = max(score, 4)
            overlap = candidate_tokens & evidence_tokens
            if len(overlap) >= 2:
                score = max(score, 3)
            elif any(len(token) >= 5 for token in overlap):
                score = max(score, 3)
            if score:
                ranked.append((score, canonical))
        if not ranked:
            return None
        best_score = max(score for score, _ in ranked)
        best = {canonical for score, canonical in ranked if score == best_score}
        return next(iter(best)) if len(best) == 1 and best_score >= 3 else None

    def resolve(candidate: str) -> str | None:
        key = _character_key(candidate)
        if not key or key in ambiguous_aliases:
            return None
        # Two different full names can be real characters, even if they differ
        # by one glyph. Never merge them through fuzzy spelling similarity.
        if candidate in canonical_names:
            return candidate
        exact = alias_map.get(key)
        if exact:
            return exact
        # A relative cannot be identified as the named person merely by
        # removing 哥/姐 or matching the named person's shorter name.
        if relation_pattern.search(key):
            return None
        role_key = key
        for suffix in ("先生", "女士", "小姐", "老师", "医生", "总裁", "少爷", "夫人", "哥哥", "姐姐", "哥", "姐"):
            if role_key.endswith(suffix) and len(role_key) > len(suffix):
                role_key = role_key[: -len(suffix)]
                break
        if role_key != key and role_key in alias_map:
            return alias_map[role_key]
        if _is_generic_character(candidate) and key not in {"男主", "女主", "主角"}:
            return None
        # A shortened form such as ``景明`` is safe only when it is contained
        # in exactly one canonical name.  This also handles honorific suffixes
        # such as ``江先生`` without guessing between two similar characters.
        contained = [name for name in canonical_names if 2 <= len(key) < len(_character_key(name)) and key in _character_key(name)]
        if len(contained) == 1:
            return contained[0]
        return None

    total_changes = backfilled
    for script in scripts[1:]:
        if not isinstance(script, dict):
            continue
        candidates = _script_character_names(script, include_generic=True)
        mapping: dict[str, str] = {}
        for candidate in candidates:
            resolved = resolve(candidate)
            profile = next((p for p in script.get("characterProfiles") or []
                            if isinstance(p, dict) and p.get("name") == candidate), {})
            if not resolved:
                resolved = resolve_by_appearance(candidate, profile)
            # A new full name must not be replaced by an earlier honorific
            # merely because its profile lists that honorific as an alias.
            # The evidence prepass above handles confirmed role -> full name.
            full_name_candidate = bool(re.fullmatch(r"[\u4e00-\u9fff]{3,4}", candidate)) and not re.search(
                r"先生|女士|小姐|少爷|夫人|总裁|男主|女主|主角|男子|女子|男生|女生|男人|女人|男孩|女孩|老人|老头|老太|妇人|大叔|阿姨|小伙|少年|少女|老师|医生|老板|校服|[红白黑蓝绿黄紫]衣", candidate)
            if not resolved and not full_name_candidate:
                matches = {resolve(alias) for alias in profile.get("aliases") or [] if character_label(alias)} - {None}
                if len(matches) == 1:
                    resolved = matches.pop()
            role = _character_key(candidate)
            if not resolved and role in kinship_relations:
                matches = set()
                for relationship in profile.get("relationships") or []:
                    if not isinstance(relationship, dict):
                        continue
                    target = character_label(relationship.get("target"))
                    relation = character_label(relationship.get("relation"))
                    if target not in canonical_names or relation not in kinship_relations[role]:
                        continue
                    matches.update(relationship_owners.get((_character_key(target), _character_key(relation)), ()))
                if len(matches) == 1:
                    resolved = matches.pop()
            if resolved and _character_key(candidate) != _character_key(resolved):
                mapping[candidate] = resolved
        if not mapping:
            add_identity_evidence(script)
            register(script)
            continue
        def replace_label(value: Any) -> Any:
            if value is None:
                return value
            text = str(value).strip()
            return mapping.get(text, text)

        script["characters"] = list(dict.fromkeys(replace_label(value) for value in script.get("characters") or []))
        script["characterNameScope"] = "labels"
        merged_profiles: list[dict[str, Any]] = []
        profile_by_name: dict[str, dict[str, Any]] = {}
        for profile in script.get("characterProfiles") or []:
            if not isinstance(profile, dict):
                continue
            old_name = str(profile.get("name") or "").strip()
            new_name = replace_label(old_name)
            profile = dict(profile)
            profile["name"] = new_name
            profile["aliases"] = list(dict.fromkeys(
                [alias for alias in profile.get("aliases") or [] if character_label(alias)]
                + ([old_name] if old_name and old_name != new_name else [])
            ))
            if profile.get("relationships"):
                profile["relationships"] = [{**relationship, "target": replace_label(relationship.get("target"))}
                                            for relationship in profile["relationships"] if isinstance(relationship, dict)]
            existing = profile_by_name.get(_character_key(new_name))
            if existing:
                existing["aliases"] = list(dict.fromkeys(existing.get("aliases", []) + profile["aliases"]))
                for field in ("appearance", "clothing", "firstAppearance"):
                    if not existing.get(field) and profile.get(field):
                        existing[field] = profile[field]
            else:
                profile_by_name[_character_key(new_name)] = profile
                merged_profiles.append(profile)
        script["characterProfiles"] = merged_profiles
        for scene in script.get("scenes") or []:
            if not isinstance(scene, dict):
                continue
            scene["characters"] = list(dict.fromkeys(replace_label(value) for value in scene.get("characters") or []))
            for block in scene.get("blocks") or []:
                if not isinstance(block, dict):
                    continue
                for key in ("speaker", "visibleSpeaker"):
                    if key in block:
                        block[key] = replace_label(block.get(key))
        total_changes += len(mapping)
        add_identity_evidence(script, mapping)
        register(script)
    return total_changes

def has_screenplay_body(script: Any) -> bool:
    """Count visible dramatic content, independently of timestamps or headings."""
    if not isinstance(script, dict):
        return False
    return any(
        isinstance(block, dict) and not block.get("suppressInOutput")
        and block.get("type") in {"action", "dialogue", "vo", "os"}
        and isinstance(block.get("text"), str) and bool(block["text"].strip())
        for scene in (script.get("scenes") or []) if isinstance(scene, dict)
        for block in (scene.get("blocks") or [])
    )


def screenplay_output_scenes(script: dict[str, Any]) -> list[dict[str, Any]]:
    """Hide empty caption shells only in output, retaining all QA evidence.

    Renumber visible headings per episode without mutating stored scenes or
    their review indices. Title cards and textless transition markers remain.
    """
    scenes = []
    counts: dict[str, int] = {}
    for scene in script.get("scenes") or []:
        if not any(not block.get("suppressInOutput") and (
            str(block.get("text") or "").strip() or block.get("type") == "transition"
        ) for block in scene.get("blocks") or []):
            continue
        heading = str(scene.get("heading") or "未标注场景")
        match = re.match(r"^\s*(\d+)-\d+", heading)
        if match:
            episode = match.group(1)
            counts[episode] = counts.get(episode, 0) + 1
            heading = re.sub(r"^\s*\d+-\d+", f"{episode}-{counts[episode]}", heading, count=1)
        scenes.append({**scene, "heading": heading})
    return scenes


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

    def uncertainty(block: dict[str, Any], block_type: str) -> str:
        """Keep QA uncertainty out of the screenplay body.

        ``script_quality`` and the administrator review payload retain the
        exact issue and time range.  The downloadable screenplay should only
        contain recovered video content; review badges such as ``需核对`` are
        internal workflow metadata, not dialogue or action.
        """
        del block, block_type
        return ""

    for scene in screenplay_output_scenes(script):
        lines.extend([scene.get("heading", "未标注场景"), ""])
        cast = [str(name).strip() for name in (scene.get("characters") or []) if str(name).strip()]
        if cast:
            lines.append(f"出场人物：{'、'.join(dict.fromkeys(cast))}")
            lines.append("")
        for block in scene.get("blocks", []):
            if block.get("suppressInOutput"):
                continue
            block_type = block.get("type")
            if block_type == "dialogue":
                warning = uncertainty(block, "dialogue")
                acting = parenthetical(block.get("performance"))
                lines.append(f"{block.get('speaker', '人物')}{warning}{acting}：{quote_dialogue(block.get('text', ''))}")
            elif block_type in {"vo", "os"}:
                inferred = uncertainty(block, "vo")
                speaker = block.get("speaker") or "旁白"
                vo_kind = block.get("voKind")
                if vo_kind == "os" or block.get("isInnerMonologue") or block_type == "os":
                    label = "OS" if speaker in {"旁白", "未知说话人", "OS", "内心独白"} else f"{speaker}（OS）"
                else:
                    label = "VO" if speaker in {"旁白", "未知说话人", "OS"} else f"{speaker}（VO）"
                lines.append(f"{label}{inferred}：{quote_dialogue(block.get('text', ''))}")
            elif block_type == "sound":
                category = "环境音" if block.get("category") == "ambience" else "音效"
                lines.append(f"{uncertainty(block, 'sound')}【{category}：{block.get('text', '')}】")
            elif block_type == "emotion":
                lines.append(f"【情绪：{block.get('text', '')}】")
            elif block_type == "screen_text":
                lines.append(f"{uncertainty(block, 'screen_text')}【字幕：{block.get('text', '')}】")
            elif block_type == "transition":
                transition_type = {
                    "flashback": "闪回",
                    "return": "闪出",
                    "flash": "闪白",
                }.get(str(block.get("transitionType") or "").strip().lower(), block.get("transitionType") or "转场")
                text = str(block.get("text") or "").strip()
                marker = uncertainty(block, "transition")
                lines.append(f"{marker}【{transition_type}：{text}】" if text else f"{marker}【{transition_type}】")
            else:
                acting = parenthetical(block.get("performance") or block.get("emotion"))
                lines.append(f"{uncertainty(block, 'action')}▲ {block.get('text', '')}{acting}")
        lines.append("")
    return "\n".join(lines)


def script_quality(
    script: dict[str, Any],
    provider: str,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Expose deterministic P0 blockers and P1/P2 reviewable editing signals."""
    evidence, subtitle_overrides = subtitle_aligned_evidence(script, evidence)
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
        character_label(name)
        for name in (script.get("characters") or [])
        if character_label(name)
    }
    known_characters.update(
        character_label(profile.get("name"))
        for profile in (script.get("characterProfiles") or [])
        if isinstance(profile, dict) and character_label(profile.get("name"))
    )
    issues: list[dict[str, Any]] = []

    # Conservative evidence/continuity probes. These never invent facts; they
    # only flag structured output that cannot prove the acceptance standard.
    continuity_entities = re.compile(r"(?:手机|电话|钥匙|证据|文件|照片|钱|刀|枪|礼物|药|门|车|包|杯子|食物)")
    temporal_markers = re.compile(r"(?:当天|第二天|次日|昨晚|今晚|早上|上午|中午|下午|晚上|夜里|回忆|闪回|现在|后来|之前|之后)")
    spatial_markers = re.compile(r"(?:室内|室外|门内|门外|楼上|楼下|车内|车外|厨房|客厅|卧室|医院|学校|街上)")

    def text_of(value: Any) -> str:
        return str(value or "").strip()

    def action_has_subject(text: str) -> bool:
        if text.startswith(("未知人物", "未知说话人")):
            return True
        for name in known_characters:
            position = text.find(name)
            # An appearance lead may precede the name, e.g. “穿红袄的希希
            # 趴在米缸口”. Do not treat a name buried after a sentence break
            # as the action subject.
            if 0 <= position <= 48 and not re.search(r"[。！？]", text[:position]):
                return True
        return False

    def add_issue(tag: str, severity: str, description: str, scene_index: int | None = None, block_index: int | None = None) -> dict[str, Any]:
        issue: dict[str, Any] = {"tag": tag, "severity": severity, "description": description}
        if scene_index is not None:
            issue["scene"] = scene_index
        if block_index is not None:
            issue["block"] = block_index
        issues.append(issue)
        return issue

    from .content_coverage import content_gaps
    if not has_screenplay_body(script):
        add_issue("剧本正文为空", "P0", "没有可输出的画面动作或对白正文，禁止作为完整剧本交付。")
    # normalize_script synthesizes a placeholder profile for otherwise
    # unaccounted-for labels. A proper name with neither naming nor identity
    # evidence must not silently become a verified cast member.
    for profile in script.get("characterProfiles") or []:
        if not isinstance(profile, dict):
            continue
        name = character_label(profile.get("name"))
        if (re.fullmatch(r"[\u4e00-\u9fff]{2,4}", name)
                and not _is_generic_character(name)
                # Relationship, job and appearance labels are valid when the
                # source never names a person; do not demand a name card for
                # e.g. 林先生, 林父 or 黑衣人.
                and not re.search(
                    r"先生|女士|小姐|少爷|夫人|总裁|老师|医生|大夫|老板|保安|护士|司机|助理|秘书|"
                    r"父亲|母亲|爸爸|妈妈|爷爷|奶奶|哥哥|姐姐|弟弟|妹妹|大哥|大姐|大叔|阿姨|"
                    r"男子|女子|男人|女人|男孩|女孩|老人|老头|老太|妇人|少年|少女|"
                    r"[红白黑蓝绿黄紫灰]衣|[父母爸妈爹娘哥姐弟妹爷奶]$", name)
                and str(profile.get("firstAppearance") or "") == "首登外观/服装待核对"
                and not any(str(profile.get(key) or "").strip()
                            for key in ("nameEvidence", "identityEvidence", "appearance", "clothing"))):
            add_issue("人物姓名缺少依据", "P0", f"{name}仅有姓名标签，没有姓名来源或人物身份资料，需回看姓名条、称呼或签名确认，不能猜名。")
    content_duration = (evidence or {}).get("durationSec")
    content_gap_ranges = content_gaps(script, content_duration) if isinstance(content_duration, (int, float)) else []
    for window in content_gap_ranges:
        issue = add_issue("正文时间缺段", "P0", (
            f"{window['startSec']:.1f}—{window['endSec']:.1f}秒没有画面动作或对白正文，"
            "需回看该段原片补全；字幕和场次时间不能代替正文。"
        ))
        issue.update(window)

    # The visible output follows 剧拆拆 and intentionally has no global event
    # chain or scene-task section. Keep those fields optional for compatibility
    # with stored results, but never turn their absence into a warning.
    event_chain_warnings = 0

    action_subject_warnings = 0
    action_detail_warnings = 0
    dialogue_integrity_warnings = 0
    dialogue_mixed_speaker_warnings = 0
    vo_classification_warnings = 0
    scene_task_warnings = 0
    continuity_warnings = 0
    action_structure_warnings = 0
    compound_action_warnings = 0
    editorial_warnings = 0
    reaction_warnings = 0
    summary_generated_warnings = 0
    prop_continuity_warnings = 0
    temporal_warnings = 0
    spatial_warnings = 0
    evidence_warnings = 0
    sound_transition_warnings = 0
    emotion_warnings = 0
    presentation_gaps = 0

    for scene_index, scene in enumerate(scenes, start=1):
        heading = str(scene.get("heading") or "")
        location = str(scene.get("location") or "").strip()
        if not _SCENE_HEADING_RE.match(heading):
            add_issue("格式错误", "P2", f"第 {scene_index} 场缺少统一的‘编号 时段 内外 地点’场次头。", scene_index)
        if not location or location == "待补充":
            add_issue("空间跳跃", "P1", f"第 {scene_index} 场没有明确地点，无法确认空间关系。", scene_index)
        scene_blocks = [block for block in (scene.get("blocks") or []) if isinstance(block, dict)]
        scene_text = " ".join(text_of(block.get("text")) for block in scene_blocks)
        scene_time = text_of(scene.get("timeOfDay"))
        if temporal_markers.search(scene_text) and scene_time in {"", "未知", "不明"}:
            temporal_warnings += 1
            add_issue("时间错乱", "P1", f"第 {scene_index} 场出现时间锚点，但场次头未明确时间，需回看视频核对先后顺序。", scene_index)
        if spatial_markers.search(scene_text) and (not location or location in {"待补充", "未知"}):
            spatial_warnings += 1
            add_issue("空间跳跃", "P1", f"第 {scene_index} 场正文包含空间变化，但场次地点未明确，需补齐空间关系。", scene_index)
        for block_index, block in enumerate(scene_blocks, start=1):
            block_type = block.get("type")
            text = str(block.get("text") or "").strip()
            emotion_present = any(
                str(block.get(key) or "").strip()
                for key in ("emotion", "emotionType", "emotionTrigger", "emotionChange", "emotionTarget")
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
                speaker = character_label(block.get("speaker")) or "未知说话人"
                if speaker in {"", "未知说话人", "未知男声", "未知女声"}:
                    add_issue("台词归属错", "P0", f"第 {scene_index} 场第 {block_index} 句台词说话人需要核对。", scene_index, block_index)
                elif block.get("uncertain"):
                    # Low confidence is not proof that a known speaker is
                    # wrong. Explicit lip-sync/action conflicts below still
                    # block delivery, while this remains an internal hint.
                    add_issue("对白置信度低", "P1", f"第 {scene_index} 场第 {block_index} 句对白置信度较低。", scene_index, block_index)
                visible_speaker = character_label(block.get("visibleSpeaker"))
                if (visible_speaker and visible_speaker != speaker and block.get("mouthMoving") is True
                        and block.get("speechContinuity") is not True):
                    add_issue("台词归属错", "P0", f"第 {scene_index} 场第 {block_index} 句口型人物为{visible_speaker}，但对白标记为{speaker}。", scene_index, block_index)
                elif block.get("mouthMoving") is False and block.get("speechContinuity") is not True:
                    add_issue("OS/VO混淆", "P1", f"第 {scene_index} 场第 {block_index} 句口型证据不足，需核对连续声源或画外对白；不能仅据嘴未动认定内心独白。", scene_index, block_index)
                if block.get("dialogueIssues"):
                    dialogue_integrity_warnings += 1
                    if any(item in {"mixed_speakers", "split_mixed_speakers"} for item in block.get("dialogueIssues", [])):
                        dialogue_mixed_speaker_warnings += 1
                    add_issue("漏台词", "P0", f"第 {scene_index} 场第 {block_index} 句对白存在完整性问题：{','.join(block['dialogueIssues'])}。", scene_index, block_index)
            elif block_type == "vo":
                if not character_label(block.get("speaker")) or block.get("speaker") in {"未知说话人", "未知男声", "未知女声"}:
                    add_issue("台词归属错", "P0", f"第 {scene_index} 场第 {block_index} 条声音说话人需要核对。", scene_index, block_index)
                if block.get("voKind") not in _VO_KINDS:
                    vo_classification_warnings += 1
                    add_issue("OS/VO混淆", "P1", f"第 {scene_index} 场第 {block_index} 条声音来源不明确。", scene_index, block_index)
                elif block.get("voKind") == "unknown":
                    vo_classification_warnings += 1
                    add_issue("OS/VO混淆", "P1", f"第 {scene_index} 场第 {block_index} 条声音来源不明确。", scene_index, block_index)
            elif block_type == "action":
                if is_editorial_artifact(text, "action"):
                    editorial_warnings += 1
                    add_issue("正文混入校对备注", "P0", f"第 {scene_index} 场第 {block_index} 个动作疑似是校对说明，不是视频画面。", scene_index, block_index)
                has_explicit_subject = action_has_subject(text)
                if known_characters and not has_explicit_subject:
                    action_subject_warnings += 1
                    add_issue("可拍摄性", "P1", f"第 {scene_index} 场第 {block_index} 个动作缺少明确人物主体。", scene_index, block_index)
                if _ABSTRACT_ACTION_RE.search(text) or not _ACTION_VERB_RE.search(text):
                    action_structure_warnings += 1
                    add_issue("动作概括", "P1", f"第 {scene_index} 场第 {block_index} 个动作可能停留在抽象结论，需补主体、动作、对象和结果。", scene_index, block_index)
                if len(text) > 180:
                    compound_action_warnings += 1
                    add_issue("连续动作未拆分", "P1", f"第 {scene_index} 场第 {block_index} 个动作包含多个主体或过长，建议按视频顺序拆成独立动作块。", scene_index, block_index)

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

        # A lengthy stretch of timed dialogue with neither visible action nor
        # playable delivery is a missing part of the screenplay, even when
        # every word of the dialogue has been transcribed. Review the video;
        # the heuristic itself must never invent gestures or emotions.
        dry_dialogue: list[tuple[int, dict[str, Any]]] = []

        def flag_dry_dialogue() -> None:
            nonlocal presentation_gaps
            for offset in range(0, len(dry_dialogue), 10):
                chunk = dry_dialogue[offset:offset + 10]
                if len(chunk) < 10 or sum(bool(text_of(block.get("performance"))) for _, block in chunk) > 1:
                    continue
                first, last = chunk[0][1], chunk[-1][1]
                start, end = first.get("startSec"), last.get("endSec")
                if (type(start) not in {int, float} or type(end) not in {int, float}
                        or not 0 <= start < end or end - start < 12):
                    continue
                issue = add_issue(
                    "画面表演缺失", "P0",
                    f"第 {scene_index} 场连续十句台词几乎没有画面动作和表演提示，需回看视频核对可见动作与可听语气。",
                    scene_index, chunk[0][0],
                )
                issue.update(startSec=start, endSec=end)
                presentation_gaps += 1

        for block_index, block in enumerate(scene_blocks, start=1):
            if block.get("type") == "action" or block.get("type") == "transition":
                flag_dry_dialogue()
                dry_dialogue = []
            elif block.get("type") == "dialogue":
                dry_dialogue.append((block_index, block))
        flag_dry_dialogue()

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

        previous_intensities: dict[str, int] = {}
        for block_index, block in enumerate(scene_blocks):
            intensity = block.get("emotionIntensity")
            if type(intensity) is not int:
                continue
            actor = character_label(block.get("speaker"))
            if not actor and block.get("type") == "action":
                actors = [name for name in known_characters if str(block.get("text") or "").startswith(name)]
                actor = actors[0] if len(actors) == 1 else ""
            if not actor:
                continue
            previous_intensity = previous_intensities.get(actor)
            if previous_intensity is not None and abs(intensity - previous_intensity) >= 2 and not str(block.get("emotionChange") or "").strip():
                emotion_warnings += 1
                add_issue("情绪变化丢失", "P1", f"第 {scene_index} 场第 {block_index + 1} 个块情绪强度跨级变化，但没有记录变化节点。", scene_index, block_index + 1)
            previous_intensities[actor] = intensity

    # Appearance profiles are internal metadata, but the first visible action
    # must still carry the identifying details so the exported screenplay can be
    # used without a separate character table.
    character_introduction_warnings = 0
    for profile in (script.get("characterProfiles") or []):
        if not isinstance(profile, dict):
            continue
        name = str(profile.get("name") or "").strip()
        details = [str(profile.get(key) or "").strip() for key in ("appearance", "clothing")]
        details = [detail for detail in details if detail and detail not in {"不明", "未知", "待核对"}]
        if not name or not details:
            continue
        first_action = next(
            (
                block
                for scene in scenes
                for block in (scene.get("blocks") or [])
                if isinstance(block, dict)
                and block.get("type") == "action"
                and name in str(block.get("text") or "")
            ),
            None,
        )
        if first_action is None or not any(detail in str(first_action.get("text") or "") for detail in details):
            character_introduction_warnings += 1
            add_issue("人物外形缺失", "P1", f"人物{name}首次动作没有带出可见发型/服装描述。")

    # Compare the final screenplay against independent evidence after all
    # normalization. This turns dialogue coverage into a measurable signal
    # instead of trusting the same model that produced the screenplay.
    transcript_reference = ""
    ocr_reference = ""
    transcript_segments: list[dict[str, Any]] = []
    ocr_segments: list[dict[str, Any]] = []
    evidence_problem_ranges: list[dict[str, Any]] = []
    evidence_sources: dict[str, dict[str, Any]] = {}
    if isinstance(evidence, dict):
        for source in evidence.get("sources") or []:
            if not isinstance(source, dict):
                continue
            kind = str(source.get("kind") or "")
            if kind:
                evidence_sources[kind] = source
            if source.get("status") != "available":
                continue
            # OCR samples the same subtitle over several adjacent frames. Use
            # each distinct normalized item once so repeated sampling is not
            # mistaken for missing screenplay text.
            source_items: list[str] = []
            seen_items: set[str] = set()
            ocr_observations: dict[tuple[str, str, str], dict[str, Any]] = {}
            for item in source.get("items") or []:
                if not isinstance(item, dict):
                    continue
                item_text = evidence_comparison_text(item.get("text"))
                if not item_text:
                    continue
                # Repeated adjacent ASR lines are real speech and must not be
                # collapsed: omitting the second occurrence is still a leaked
                # line. OCR, in contrast, naturally sees the same subtitle in
                # several frames and is deduplicated for aggregate coverage.
                segment = {
                    "text": item_text,
                    "startSec": item.get("startSec"),
                    "endSec": item.get("endSec"),
                    "region": item.get("region"),
                    "screenType": item.get("screenType"),
                }
                if source.get("kind") == "audio" and item.get("type") == "transcript_segment":
                    transcript_segments.append(segment)
                    for word in item.get("words") or []:
                        if isinstance(word, dict) and evidence_comparison_text(word.get("text")) in {"咋", "啊", "呵", "嗯", "哦", "哎", "唉", "呃", "哈"}:
                            transcript_segments.append({**word, "text": evidence_comparison_text(word["text"])})
                elif source.get("kind") == "ocr":
                    # Adjacent frame observations are one occurrence; the
                    # same subtitle later in the video is checked again.
                    key = (item_text, str(item.get("region") or ""), str(item.get("screenType") or ""))
                    previous = ocr_observations.get(key)
                    start = segment["startSec"]
                    previous_end = previous.get("endSec", previous.get("startSec")) if previous else None
                    if previous_end is None and previous:
                        previous_end = previous.get("startSec")
                    if (previous and isinstance(start, (int, float)) and isinstance(previous_end, (int, float))
                            and 0 <= start - previous_end <= 1.1):
                        previous["endSec"] = segment.get("endSec") if segment.get("endSec") is not None else start
                    else:
                        ocr_segments.append(segment)
                        ocr_observations[key] = segment
                if source.get("kind") != "ocr" or item_text not in seen_items:
                    source_items.append(item_text)
                seen_items.add(item_text)
            source_text = "".join(source_items)
            if source.get("kind") == "audio":
                transcript_reference += source_text
            elif source.get("kind") == "ocr":
                ocr_reference += source_text

    audio_source = evidence_sources.get("audio")
    if audio_source is not None and audio_source.get("status") != "available":
        evidence_warnings += 1
        reason = str(audio_source.get("reason") or "音频转写不可用").strip()
        add_issue(
            "独立音频转写缺失",
            "P1",
            f"无法用独立 ASR 校验是否漏台词或改写台词：{reason}。本结果必须人工复核，不能显示伪造覆盖率。",
        )
    ocr_source = evidence_sources.get("ocr")
    if ocr_source is not None and ocr_source.get("status") != "available":
        evidence_warnings += 1
        reason = str(ocr_source.get("reason") or "OCR 不可用").strip()
        add_issue(
            "独立画面文字校验缺失",
            "P1",
            f"无法用独立 OCR 校验字幕、姓名条和系统文字是否遗漏：{reason}。本结果必须人工复核。",
        )

    # Detect a screenplay that stops well before the last independent speech
    # evidence. Aggregate coverage can look acceptable when the entire tail of
    # a long episode is absent, so this is an independent P0 content check.
    evidence_end_sec = max(
        (float(item.get("endSec")) for item in transcript_segments
         if isinstance(item.get("endSec"), (int, float))),
        default=None,
    )
    script_end_sec = max(
        (float(block.get("endSec")) for block in dialogue
         if isinstance(block.get("endSec"), (int, float))),
        default=None,
    )
    tail_gap_sec: float | None = None
    if evidence_end_sec is not None and script_end_sec is not None and evidence_end_sec > script_end_sec:
        tail_gap_sec = round(evidence_end_sec - script_end_sec, 3)
        if tail_gap_sec >= 8:
            evidence_warnings += 1
            issue = add_issue(
                "台词尾部截断",
                "P0",
                f"独立 ASR 证据持续到 {evidence_end_sec:.1f} 秒，但成稿台词只到 {script_end_sec:.1f} 秒，尾部缺失约 {tail_gap_sec:.1f} 秒，必须复核完整视频。",
            )
            issue.update(startSec=script_end_sec, endSec=evidence_end_sec)

    spoken_candidate = ordered_script_text(script, {"dialogue", "vo"})
    timed_spoken = any(
        isinstance(block.get("startSec"), (int, float))
        for scene in scenes for block in scene.get("blocks", [])
        if block.get("type") in {"dialogue", "vo"}
    )
    transcript_matched, transcript_size, spoken_size = matching_character_counts(
        transcript_reference,
        spoken_candidate,
    )
    transcript_coverage: int | None = None
    dialogue_evidence_precision: int | None = None
    if transcript_size >= 12:
        transcript_coverage = round(100 * transcript_matched / transcript_size)
        dialogue_evidence_precision = round(100 * transcript_matched / spoken_size) if spoken_size else 0
        weakest_alignment = min(transcript_coverage, dialogue_evidence_precision)
        if weakest_alignment < 55:
            evidence_warnings += 1
            add_issue(
                "音频对白不一致",
                "P0",
                f"成稿台词与独立音频转写严重不一致（转写覆盖 {transcript_coverage}%、成稿吻合 {dialogue_evidence_precision}%），可能存在漏台词、改写或补写。",
            )
        elif weakest_alignment < 72:
            evidence_warnings += 1
            add_issue(
                "音频对白需核对",
                "P1",
                f"成稿台词与独立音频转写部分不一致（转写覆盖 {transcript_coverage}%、成稿吻合 {dialogue_evidence_precision}%），需按原视频复核。",
            )

    # Overall coverage can remain deceptively high when one short line is
    # missing from a long clip. Check each time-anchored ASR segment as well so
    # the reviewer can seek directly to the likely omission.
    for segment in transcript_segments:
        segment_text = str(segment.get("text") or "")
        if not segment_text:
            continue
        local_candidate = time_window_script_text(
            script,
            {"dialogue", "vo"},
            segment.get("startSec"),
            segment.get("endSec"),
            margin=1.5,
        )
        # A different occurrence of “啊” elsewhere cannot cover a missing
        # utterance at this timestamp. Only untimed legacy output falls back.
        candidate = local_candidate if timed_spoken and isinstance(segment.get("startSec"), (int, float)) else spoken_candidate
        matched, segment_size, _ = matching_character_counts(segment_text, candidate)
        segment_coverage = round(100 * matched / segment_size) if segment_size else 100
        # Provider timestamps are approximate and often drift across a scene
        # boundary. For a normal-length segment, a verbatim match elsewhere
        # in the ordered screenplay is evidence of timing drift rather than a
        # missing line; keep short one- or two-character beats strict so they
        # are still recovered and checked individually.
        if len(segment_text) >= 4 and segment_text in spoken_candidate:
            continue
        if len(segment_text) >= 6:
            global_matched, global_size, _ = matching_character_counts(segment_text, spoken_candidate)
            global_coverage = round(100 * global_matched / global_size) if global_size else 0
            if global_coverage >= 72:
                continue
        if segment_coverage >= 72:
            continue
        filler = evidence_comparison_text(segment_text) in {"咋", "啊", "呵", "嗯", "哦", "哎", "唉", "呃", "哈"}
        ocr_confirmed = any(
            segment_text == item["text"] and isinstance(item.get("startSec"), (int, float))
            and isinstance(segment.get("startSec"), (int, float))
            and abs(item["startSec"] - segment["startSec"]) <= 0.8
            for item in ocr_segments
        )
        # A one-character dramatic beat such as “我” can change the line and
        # must not be silently delivered as complete. Common hesitation fillers
        # remain advisory when independent ASR cannot align them.
        severity = "P0" if segment_coverage < 45 and (not filler or ocr_confirmed) else "P1"
        start = segment.get("startSec")
        end = segment.get("endSec")
        location = f"{float(start):.1f}-{float(end):.1f} 秒" if isinstance(start, (int, float)) and isinstance(end, (int, float)) else "未定位时段"
        issue = add_issue(
            "疑似漏台词片段",
            severity,
            f"独立 ASR 在 {location} 的语音片段仅有 {segment_coverage}% 出现在成稿中，需逐字回听核对。",
        )
        if ocr_confirmed:
            issue["ocrConfirmed"] = True
        if start is not None:
            issue["startSec"] = start
        if end is not None:
            issue["endSec"] = end
        evidence_warnings += 1
        evidence_problem_ranges.append({
            "kind": "audio",
            "startSec": start,
            "endSec": end,
            "coverage": segment_coverage,
            "severity": severity,
        })

    screen_text_candidate = ordered_script_text(script, {"screen_text"})
    timed_screen_text = any(
        isinstance(block.get("startSec"), (int, float))
        for scene in scenes for block in scene.get("blocks", [])
        if block.get("type") == "screen_text"
    )
    def represented_spoken_subtitle(segment: dict[str, Any]) -> bool:
        # Do not require a second, duplicate screen_text when the exact timed
        # subtitle is already spoken. Names/titles and ambiguous short OCR
        # observations still need their own visual-text check.
        text = segment["text"]
        kind = segment.get("screenType")
        if kind != "subtitle" and (kind or segment.get("region") != "subtitle_band" or len(text) < 4):
            return False
        start, end = segment.get("startSec"), segment.get("endSec")
        end = end if end is not None else start
        if type(start) not in {int, float} or type(end) not in {int, float} or not 0 <= start <= end:
            return False
        audio_supported = any(
            item["text"] == text
            and type(item.get("startSec")) in {int, float} and type(item.get("endSec")) in {int, float}
            and item["startSec"] <= end + 0.35 and item["endSec"] >= start - 0.35
            for item in transcript_segments
        )
        return audio_supported and text in time_window_script_text(script, {"dialogue", "vo"}, start, end, margin=0.35)

    represented_subtitles = [represented_spoken_subtitle(segment) for segment in ocr_segments]
    confirmed_texts = {segment["text"] for segment, supported in zip(ocr_segments, represented_subtitles) if supported}
    if confirmed_texts:
        # Aggregate OCR already counts repeated frame text once. Keep the same
        # rule; each later occurrence is still checked separately below.
        remaining = "".join(dict.fromkeys(segment["text"] for segment in ocr_segments
                                          if segment["text"] not in confirmed_texts))
        ocr_matched, ocr_size, _ = matching_character_counts(remaining, screen_text_candidate)
        confirmed_size = sum(len(text) for text in confirmed_texts)
        ocr_matched += confirmed_size
        ocr_size += confirmed_size
    else:
        ocr_matched, ocr_size, _ = matching_character_counts(ocr_reference, screen_text_candidate)
    ocr_coverage: int | None = None
    if ocr_size >= 4:
        ocr_coverage = round(100 * ocr_matched / ocr_size)
        if ocr_coverage < 45:
            evidence_warnings += 1
            add_issue(
                "画面文字需核对",
                "P1",
                f"成稿仅覆盖独立 OCR 证据的 {ocr_coverage}%，可能漏掉字幕、姓名条或系统文字。",
            )

    for segment, represented in zip(ocr_segments, represented_subtitles):
        if represented:
            continue
        segment_text = str(segment.get("text") or "")
        local_screen_text = time_window_script_text(script, {"screen_text"}, segment.get("startSec"),
                                                   segment.get("endSec") if segment.get("endSec") is not None else segment.get("startSec"), margin=0.5)
        matched, segment_size, _ = matching_character_counts(segment_text, local_screen_text)
        segment_coverage = round(100 * matched / segment_size) if segment_size else 100
        # Untimed legacy output can only support an aggregate text comparison.
        if not timed_screen_text:
            matched, segment_size, _ = matching_character_counts(segment_text, screen_text_candidate)
            segment_coverage = round(100 * matched / segment_size) if segment_size else 100
        if segment_coverage >= 45:
            continue
        start = segment.get("startSec")
        end = segment.get("endSec")
        location = f"{float(start):.1f}-{float(end):.1f} 秒" if isinstance(start, (int, float)) and isinstance(end, (int, float)) else "未定位时段"
        issue = add_issue(
            "疑似漏画面文字片段",
            "P1",
            f"独立 OCR 在 {location} 的文字仅有 {segment_coverage}% 出现在成稿字幕中，需回看画面核对。",
        )
        if start is not None:
            issue["startSec"] = start
        if end is not None:
            issue["endSec"] = end
        evidence_warnings += 1
        evidence_problem_ranges.append({
            "kind": "ocr",
            "startSec": start,
            "endSec": end,
            "coverage": segment_coverage,
            "severity": "P1",
        })

    for scene_index, scene in enumerate(scenes, start=1):
        blocks = scene.get("blocks") or []
        for block_index, block in enumerate(blocks, start=1):
            if block.get("type") != "screen_text" or block.get("screenType") != "subtitle":
                continue
            subtitle = evidence_comparison_text(block.get("text"))
            start, end = block.get("startSec"), block.get("endSec")
            if not subtitle or not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
                continue
            spoken = time_window_script_text(script, {"dialogue", "vo"}, start, end, margin=0.35)
            if subtitle in spoken:
                continue
            audio_support = any(
                isinstance(item.get("startSec"), (int, float)) and isinstance(item.get("endSec"), (int, float))
                and item["startSec"] <= end + 0.35 and item["endSec"] >= start - 0.35
                and subtitle in item["text"] for item in transcript_segments
            )
            speech_support = any(
                item.get("type") == "action" and _SPEECH_CUE_RE.search(str(item.get("text") or ""))
                and isinstance(item.get("startSec"), (int, float)) and isinstance(item.get("endSec"), (int, float))
                and item["startSec"] <= end and item["endSec"] >= start - 0.35
                for item in blocks[:block_index - 1]
            )
            if audio_support or speech_support:
                issue = add_issue("字幕对白遗漏", "P1", f"第 {scene_index} 场第 {block_index} 条字幕有声音或开口动作佐证，但缺少对应对白；需回看确认角色和原话。", scene_index, block_index)
                issue["audioSupported"] = audio_support

    coverage = round(100 * sum(punctuation_ok) / len(punctuation_ok)) if punctuation_ok else 0
    confidence = round(100 * (1 - uncertain / len(dialogue))) if dialogue else 0
    unique_speakers = {
        str(block.get("speaker") or "").strip()
        for block in dialogue
        if str(block.get("speaker") or "").strip() not in {"", "未知说话人", "旁白"}
    }
    screen_text_count = sum(
        1
        for scene in scenes
        for block in (scene.get("blocks") or [])
        if isinstance(block, dict) and block.get("type") == "screen_text"
    )
    transition_count = sum(
        1
        for scene in scenes
        for block in (scene.get("blocks") or [])
        if isinstance(block, dict) and block.get("type") == "transition"
    )
    # This is a routing hint, not a quality score.  It gives the later
    # Lite/Turbo router a deterministic signal without pretending that model
    # confidence is the same thing as source-video truth.
    complexity_score = min(
        100,
        len(scenes) * 4
        + len(action_blocks) * 1
        + len(dialogue) * 2
        + len(unique_speakers) * 4
        + screen_text_count * 3
        + transition_count * 5
        + uncertain * 8,
    )
    complexity_band = "complex" if complexity_score >= 60 else "standard" if complexity_score >= 25 else "simple"
    review_recommendation = (
        "turbo_review"
        if complexity_band == "complex" or uncertain or evidence_problem_ranges or evidence_warnings
        else "lite_only"
    )

    # Give automatic and human reviewers a direct seek range whenever the
    # model supplied block timestamps.  The issue still remains valid when no
    # trustworthy timestamp exists, so never invent one here.
    for issue in issues:
        scene_number = issue.get("scene")
        block_number = issue.get("block")
        if not isinstance(scene_number, int) or not isinstance(block_number, int):
            continue
        if scene_number < 1 or scene_number > len(scenes):
            continue
        blocks = [block for block in (scenes[scene_number - 1].get("blocks") or []) if isinstance(block, dict)]
        if block_number < 1 or block_number > len(blocks):
            continue
        block = blocks[block_number - 1]
        if "startSec" not in issue and block.get("startSec") is not None:
            issue["startSec"] = block.get("startSec")
        if "endSec" not in issue and block.get("endSec") is not None:
            issue["endSec"] = block.get("endSec")

    issue_tags = list(dict.fromkeys(str(item["tag"]) for item in issues))
    severity_counts = {
        level: sum(1 for item in issues if item.get("severity") == level)
        for level in ("P0", "P1", "P2")
    }
    return {
        "dialogueCoverage": coverage,
        "transcriptCoverage": transcript_coverage,
        "subtitlePrioritySegments": subtitle_overrides,
        "dialogueEvidencePrecision": dialogue_evidence_precision,
        "evidenceEndSec": evidence_end_sec,
        "scriptEndSec": script_end_sec,
        "tailGapSec": tail_gap_sec,
        "contentGapRanges": content_gap_ranges,
        "ocrCoverage": ocr_coverage,
        "speakerConfidence": max(0, confidence),
        "warnings": len(issues),
        "sceneCount": len(scenes),
        "dialogueCount": len(dialogue),
        "actionCount": len(action_blocks),
        "performanceCount": sum(bool(text_of(block.get("performance"))) for block in dialogue),
        "presentationGaps": presentation_gaps,
        "complexityScore": complexity_score,
        "complexityBand": complexity_band,
        "reviewRecommendation": review_recommendation,
        "actionSubjectWarnings": action_subject_warnings,
        "actionDetailWarnings": action_detail_warnings,
        "actionStructureWarnings": action_structure_warnings,
        "compoundActionWarnings": compound_action_warnings,
        "editorialWarnings": editorial_warnings,
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
        "evidenceProblemRanges": evidence_problem_ranges,
        "soundTransitionWarnings": sound_transition_warnings,
        "emotionWarnings": emotion_warnings,
        "severityCounts": severity_counts,
        "issueTags": issue_tags,
        "blockingIssues": [issue for issue in issues if issue.get("severity") == "P0"],
        "issues": issues[:100],
        "provider": provider,
    }


def quality_gate(quality: dict[str, Any]) -> tuple[bool, list[dict[str, Any]]]:
    """Return whether a script is safe to expose as a downloadable result.

    Only P0 findings block delivery.  P1 findings are useful automated editing
    hints (action granularity, appearance, continuity and similar heuristics),
    but treating every hint as a manual-review failure sent nearly every
    episode to an administrator.  Content-loss and attribution defects remain
    P0 and still fail closed.
    """
    issues = [
        issue
        for issue in (quality.get("blockingIssues") or []) + (quality.get("issues") or [])
        if isinstance(issue, dict) and str(issue.get("severity") or "").upper() == "P0"
    ]
    unique = []
    for issue in issues:
        if issue not in unique:
            unique.append(issue)
    # Legacy persisted summaries can contain more P0s than their truncated
    # details. Missing details must never turn such a result into an approval.
    count = (quality.get("severityCounts") or {}).get("P0", 0)
    if isinstance(count, int) and count > len(unique):
        unique.append({"severity": "P0", "tag": "阻断问题明细不完整",
                       "description": "质量摘要仍有阻断问题，须重新校验完整正文。"})
    return not unique, unique
