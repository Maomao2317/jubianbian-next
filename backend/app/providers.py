"""External recognition providers and their deterministic fallbacks."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request as UrlRequest, urlopen

from .config import (
    ARK_API_KEY,
    ARK_API_KEYS,
    ARK_BASE_URL,
    ARK_FILE_POLL_SECONDS,
    ARK_FILE_POLL_TIMEOUT_SECONDS,
    ARK_MODEL,
    ARK_INPUT_TOKEN_PRICE_RMB_PER_MILLION,
    ARK_OUTPUT_TOKEN_PRICE_RMB_PER_MILLION,
    ARK_VIDEO_FPS,
    DATA_DIR,
    OPENAI_API_KEY,
    OPENAI_BASE_URL,
    OPENAI_TEXT_MODEL,
    OPENAI_TRANSCRIPTION_MODEL,
)
from .errors import ArkError
from .logging_setup import logger
from .media import multipart_body
from .script import normalize_script

# Ark is the preferred video-understanding provider.  OpenAI remains an
# optional transcription/text fallback when the primary provider is unavailable.
def ark_fallback_message(error: ArkError) -> str:
    """Explain a provider downgrade without exposing provider credentials/details."""
    if error.status_code == 429 or error.provider_code.lower() in {"setlimitexceeded", "ratelimitexceeded"}:
        return "方舟模型当前达到推理限额（HTTP 429），本次已切换到备用识别链路。"
    return "方舟识别服务暂时不可用，本次已切换到备用识别链路。"


_ark_pool_lock = threading.Lock()
_ark_pool_index = 0
_ark_cooldowns: dict[str, float] = {}


def ark_account_key() -> str:
    """Pick the next available account; cooldowns are isolated per API key."""
    global _ark_pool_index
    if not ARK_API_KEYS:
        raise ArkError("未配置方舟 API Key，请在 fangzhou.env 中填写 ARK_API_KEY")
    now = time.monotonic()
    with _ark_pool_lock:
        for _ in range(len(ARK_API_KEYS)):
            key = ARK_API_KEYS[_ark_pool_index % len(ARK_API_KEYS)]
            _ark_pool_index = (_ark_pool_index + 1) % len(ARK_API_KEYS)
            if _ark_cooldowns.get(key, 0) <= now:
                return key
        return min(ARK_API_KEYS, key=lambda value: _ark_cooldowns.get(value, 0))


def ark_http(method: str, path: str, data: bytes | None = None, content_type: str = "application/json", *, api_key: str | None = None) -> Any:
    api_key = api_key or ARK_API_KEY
    if not api_key:
        raise ArkError("未配置方舟 API Key，请在 fangzhou.env 中填写 ARK_API_KEY")
    request = UrlRequest(
        f"{ARK_BASE_URL}{path}",
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": content_type,
        },
        method=method,
    )
    try:
        with urlopen(request, timeout=300) as response:
            raw = response.read()
    except HTTPError as exc:
        detail = ""
        provider_code = ""
        try:
            payload = json.loads(exc.read().decode("utf-8", errors="replace"))
            detail = str(payload.get("message") or payload.get("error") or payload.get("detail") or "")
            provider_code = str(payload.get("code") or "") if isinstance(payload, dict) else ""
        except (OSError, ValueError):
            pass
        if exc.code == 429:
            with _ark_pool_lock:
                _ark_cooldowns[api_key] = time.monotonic() + 30
            # Provider 429 bodies may contain account identifiers and internal
            # quota text. Keep the actionable cause without echoing that data.
            detail = "模型当前达到推理限额或已暂停，请在方舟模型激活页调整限额或关闭 Safe Experience Mode"
        suffix = f"：{detail[:300]}" if detail else ""
        raise ArkError(
            f"方舟接口请求失败（HTTP {exc.code}）{suffix}",
            status_code=exc.code,
            provider_code=provider_code,
        ) from exc
    except URLError as exc:
        raise ArkError(f"无法连接方舟接口：{exc.reason}") from exc
    except OSError as exc:
        raise ArkError(f"方舟接口网络错误：{exc}") from exc
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ArkError("方舟接口返回了无法解析的响应") from exc


def ark_upload_video(path: Path, api_key: str | None = None) -> str:
    # The Files API accepts a local video and lets the model reuse it by file_id.
    # The upload is intentionally done in the worker thread so FastAPI stays responsive.
    fields = {
        "purpose": "user_data",
        "preprocess_configs": json.dumps({"video": {"fps": ARK_VIDEO_FPS}}, separators=(",", ":")),
    }
    body, boundary = multipart_body(
        fields,
        "file",
        path.name,
        path.read_bytes(),
        "video/mp4",
    )
    payload = ark_http("POST", "/files", body, f"multipart/form-data; boundary={boundary}", api_key=api_key)
    file_id = None
    if isinstance(payload, dict):
        file_id = payload.get("id") or payload.get("file_id")
    if not file_id:
        raise ArkError("方舟上传成功但没有返回 file_id")
    return str(file_id)


def ark_wait_for_file(file_id: str, api_key: str | None = None) -> None:
    deadline = time.monotonic() + ARK_FILE_POLL_TIMEOUT_SECONDS
    encoded_id = quote(file_id, safe="")
    while True:
        payload = ark_http("GET", f"/files/{encoded_id}", api_key=api_key)
        status = str(payload.get("status") or payload.get("state") or "").lower() if isinstance(payload, dict) else ""
        if not status or status in {"active", "ready", "uploaded", "succeeded", "completed"}:
            return
        if status in {"failed", "error", "expired", "cancelled", "canceled"}:
            message = payload.get("message") if isinstance(payload, dict) else ""
            raise ArkError(f"方舟视频预处理失败：{message or status}")
        if time.monotonic() >= deadline:
            raise ArkError("方舟视频预处理超时，请稍后重试")
        time.sleep(ARK_FILE_POLL_SECONDS)


def ark_prompt(title: str, duration_sec: float) -> str:
    return (
        "你是专业的中文短剧剧本整理助手。请完整核对视频中的声音、对白、字幕和画面动作，把它按原时间顺序整理成《剧拆拆》样式的镜头化剧本。"
        "每个可辨识的连续画面动作使用一个 action block，导出时以‘▲’开头；动作要写成可读的画面文字，不要写摄影术语、拍摄角度或剪辑调度。"
        "只返回一个合法 JSON 对象，不要 Markdown、不要代码围栏、不要解释。"
        "JSON 结构必须是："
        "{version:string,title:string,eventChain:[{id:string,role:'cause'|'conflict'|'turn'|'result'|'hook',summary:string,evidence:string,startSec:number,endSec:number}],settingRules:string[],"
        "characters:string[],characterProfiles:[{id:string,name:string,aliases:string[],appearance:string,clothing:string,firstAppearance:string}],"
        "scenes:[{id:string,heading:string,location:string,timeOfDay:string,interiorExterior:string,segmentType:'main'|'recap'|'trailer'|'title_card'|'credits'|'flashback',"
        "goal:string,obstacle:string,result:string,continuityIn:string,continuityOut:string,hook:string,summary:string,characters:string[],environment:string,props:[{name:string,state:string,evidence:string}],"
        "blocks:[{type:'action',text:string,emotion:string,emotionType:string,emotionIntensity:0|1|2|3|4|5,emotionTrigger:string,emotionChange:string,emotionTarget:string,emotionEvidence:string,performance:string,object:string,result:string,startSec:number,endSec:number}|"
        "{type:'dialogue',speaker:string,rawText:string,finalText:string,performance:string,speechTone:string,emotionType:string,emotionIntensity:0|1|2|3|4|5,emotionTrigger:string,emotionChange:string,emotionTarget:string,emotionEvidence:string,tone:string,volume:string,pause:string,emphasis:string,text:string,confidence:'high'|'medium'|'low',uncertain:boolean,startSec:number,endSec:number}|"
        "{type:'vo',speaker:string,voKind:'os'|'narration'|'memory'|'phone'|'unknown',performance:string,emotion:string,text:string,confidence:'high'|'medium'|'low',uncertain:boolean,inferred:boolean,startSec:number,endSec:number}|"
        "{type:'sound',category:'effect'|'ambience',source:'heard',importance:'plot'|'atmosphere',text:string,startSec:number,endSec:number}|"
        "{type:'screen_text',screenType:'subtitle'|'system'|'title_card'|'other',text:string,startSec:number,endSec:number}|"
        "{type:'transition',transitionType:'flashback'|'return'|'flash'|'other',text:string,startSec:number,endSec:number}|"
        "{type:'emotion',emotionType:string,emotionIntensity:0|1|2|3|4|5,emotionTrigger:string,emotionChange:string,emotionTarget:string,emotionEvidence:string,text:string,startSec:number,endSec:number}]}]}。"
        "必须遵守以下规则："
        "1. heading 必须严格使用‘编号 时段 内外 地点’，例如‘1-1 夜 外 酒店门口’、‘1-2 日 内 酒店房间’；时段只用日、夜、清晨、黄昏或不明，内外只用内、外或不明。"
        "2. 不要输出人物表、剧情衔接、场次任务、目标、阻力、结果、环境说明、道具表或独立情绪段；这些字段如出现在 JSON 结构中一律留空，情绪直接写进▲画面动作或对白括号，正文只保留场次头、每场出场人物、▲画面动作、对白、声音、字幕和必要转场。"
        "3. 只有地点、时间或内外景真正变化才新建场景；同一场内按视频中的连续画面和动作顺序逐条整理，保留《剧拆拆》所需的画面节奏，不要把多个动作概括成一句剧情总结。"
        "4. 连续动作要完整覆盖因果和数量：每一次明确的拳击、推搡、开门、拿取、进出、转身离开都不能漏写；如果是第一拳、第二拳，要按实际顺序明确写出，不能只写‘打了几拳’或只写最后结果，也不能凭空增加动作。动作必须保留视频里实际听见/看见的动词和对象，不要把‘推’改写成‘撞’、把‘拿’概括成‘处理’。每个 action 必须包含规范人物名、动作、对象和结果，必要时补充可执行的表演提示；不要让句子以‘狠狠拽住’、‘随后离开’、‘故意看向’这类无主语短语开头；看不清时写‘未知人物’，不要猜。"
        "5. 指尖发白、眼里有血丝、眼神一闪、呼吸变化等只作为动作段中的补充，不得单独成为一个镜头/block；只有它直接改变人物决定或剧情结果时才保留。"
        "6. 严禁写‘特写、近景、正反打、镜头切到、推近、拉远、俯拍、仰拍、画面给到’等拍摄或剪辑指令。后期剪辑处理不进入剧本正文。"
        "7. environment 必须留空；画面中的地点、人物关系和动作直接写进 action block，不另设环境段落。"
        "8. 人物第一次出现时，必须在 characterProfiles 的 firstAppearance、appearance 或 clothing 中补齐视频实际可见的识别信息；只写能帮助表演的外观/服装，不要猜年龄、身份或剧情之外的颜色饰品。看不清就留空并标记需要核对。"
        "9. dialogue 必须一人一句、完整保留原话，不能改写成剧情概括，不能省略、合并、调换顺序或补写听不清的内容。台词文字是不可改写字段：只允许恢复中文标点和清理空白，不能替换同义词。每个 dialogue block 只能对应一个 speaker；发现说话人切换就拆成多个 block，仍逐字保留。先根据语气和语法恢复准确中文标点：逗号分隔分句，句末使用‘。’、‘？’、‘！’或‘……’，不要输出无标点的长句，也不要连续重复标点。听不清的人名、称谓和代词保留‘[听不清]’，confidence 写 low、uncertain 写 true。"
        "10. 情绪必须绑定可观察证据：先写画面动作/声音，再写 emotionType、emotionIntensity、emotionTrigger、emotionChange、emotionTarget、emotionEvidence；看不清时标 emotion_uncertain，不得编造心理。对白的 speechTone 只有视频确实能判断时才填写。关键转折可单独使用 emotion block，但不能只写场次总结。"
        "11. 人物或旁白的画外音、内心声、回忆声、电话另一端声音必须使用 type=vo，并在 speaker 写对应人物名或‘旁白’，不能混成普通 dialogue，也不能漏标。voKind 必须分别写 os（明确是人物内心独白）、narration（旁白/画外音）、memory（回忆声）或 phone（电话另一端）；没有明确证据时写 unknown，不得把普通旁白猜成 OS。若声音来源无法确认，speaker 写‘未知说话人’并标 uncertain。没有声音依据时不能臆造 VO。"
        "12. 正式扒剧本不输出背景音乐；sound 只保留原片确实听到且对剧情有作用的动作音效或环境声。必须逐条识别所有画面字幕，包括人物姓名字幕、对白字幕、作品/AI免责声明、系统提示、标题字样和转场字幕，全部使用 screen_text 单独标记并保持出现顺序，不能漏掉、合并或改写；闪回、回到现实和闪白等使用 transition。"
        "13. characterProfiles 中为每个人建立唯一 id、规范 name 和 aliases；正文所有 speaker 和 characters 必须使用同一个规范 name，不能混用‘男主’、‘江川’等称呼。"
        "14. recap、trailer、title_card 或 credits 不作为新剧情场景，但其中实际出现的字幕必须保留为 screen_text；不要因其属于片头、片尾或包装而丢弃字幕。"
        "15. startSec 和 endSec 填写画面或声音的大致秒数并保持顺序；时间只用于回看定位，不要据此拆成镜头。无法判断时才填 0。每个关键行为都要能回溯到 eventChain 或视频时间证据；不要为没有证据的动机、设定、道具状态或心理活动补写事实。"
        "没有把握的内容使用‘不明’、‘未知说话人’或‘[听不清]’，不要编造视频之外的事实；优先保证剧情因果、台词准确、动作动词保真和人物一致。"
        f"视频标题：{title}；视频时长：{duration_sec:.1f} 秒。"
    )


def ark_response_text(payload: Any) -> str:
    if isinstance(payload, dict) and isinstance(payload.get("output_text"), str):
        return payload["output_text"].strip()
    parts: list[str] = []
    if isinstance(payload, dict):
        output = payload.get("output") or []
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    continue
                content = item.get("content") or []
                if isinstance(content, list):
                    for chunk in content:
                        if isinstance(chunk, dict) and isinstance(chunk.get("text"), str):
                            parts.append(chunk["text"])
                elif isinstance(content, str):
                    parts.append(content)
    if parts:
        return "\n".join(parts).strip()

    # Keep compatibility with minor Responses API response-shape changes.
    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"output_text", "text"} and isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, (dict, list)):
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(payload)
    return "\n".join(parts).strip()

def ark_usage(payload: Any) -> dict[str, int | float | None]:
    usage = payload.get("usage") if isinstance(payload, dict) else {}
    usage = usage if isinstance(usage, dict) else {}

    def number(*keys: str) -> int | None:
        for key in keys:
            value = usage.get(key)
            if isinstance(value, (int, float)):
                return int(value)
        return None

    input_tokens = number("input_tokens", "prompt_tokens")
    output_tokens = number("output_tokens", "completion_tokens")
    total_tokens = number("total_tokens")
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = input_tokens + output_tokens
    # Some Ark endpoints expose a billed amount alongside usage. Accept the
    # common shapes without coupling the app to one response version.
    cost: float | None = None
    for source in (usage, payload if isinstance(payload, dict) else {}):
        for key in ("api_cost_rmb", "cost_rmb", "total_cost", "cost"):
            value = source.get(key)
            if isinstance(value, (int, float)):
                cost = float(value)
                break
        if cost is not None:
            break
    if cost is None and (input_tokens is not None or output_tokens is not None):
        cost = (
            (input_tokens or 0) * ARK_INPUT_TOKEN_PRICE_RMB_PER_MILLION
            + (output_tokens or 0) * ARK_OUTPUT_TOKEN_PRICE_RMB_PER_MILLION
        ) / 1_000_000
    return {"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": total_tokens, "api_cost_rmb": round(cost or 0, 6)}


def ark_recognize(path: Path, title: str, duration_sec: float) -> tuple[dict[str, Any], dict[str, int | None]]:
    api_key = ark_account_key()
    started = time.perf_counter()
    logger.info("provider_start provider=ark operation=video_recognize file=%s duration_sec=%.1f", path.name, duration_sec)
    upload_started = time.perf_counter()
    file_id = ark_upload_video(path, api_key)
    logger.info("provider_step provider=ark operation=upload file=%s duration_ms=%.1f", path.name, (time.perf_counter() - upload_started) * 1000)
    wait_started = time.perf_counter()
    ark_wait_for_file(file_id, api_key)
    logger.info("provider_step provider=ark operation=file_ready file=%s duration_ms=%.1f", path.name, (time.perf_counter() - wait_started) * 1000)
    response_started = time.perf_counter()
    payload = ark_http(
        "POST",
        "/responses",
        json.dumps({
            "model": ARK_MODEL,
            "input": [{
                "role": "user",
                "content": [
                    {"type": "input_video", "file_id": file_id},
                    {"type": "input_text", "text": ark_prompt(title, duration_sec)},
                ],
            }],
        }, ensure_ascii=False).encode("utf-8"),
        api_key=api_key,
    )
    logger.info("provider_step provider=ark operation=response file=%s duration_ms=%.1f", path.name, (time.perf_counter() - response_started) * 1000)
    text = ark_response_text(payload)
    if not text:
        raise ArkError("方舟没有返回剧本文本")
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    if not cleaned.startswith("{"):
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            cleaned = cleaned[start:end + 1]
    try:
        raw_script = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ArkError("方舟返回的剧本不是合法 JSON") from exc
    script = normalize_script(raw_script, title)
    logger.info(
        "provider_done provider=ark operation=video_recognize file=%s duration_ms=%.1f scenes=%s",
        path.name,
        (time.perf_counter() - started) * 1000,
        len(script.get("scenes") or []),
    )
    return script, ark_usage(payload)


def openai_transcribe(path: Path) -> str:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not OPENAI_API_KEY:
        return ""
    audio_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", dir=DATA_DIR, delete=False) as handle:
            audio_path = Path(handle.name)
        subprocess.run(
            [ffmpeg, "-y", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k", str(audio_path)],
            capture_output=True,
            timeout=180,
            check=True,
        )
        body, boundary = multipart_body(
            {"model": OPENAI_TRANSCRIPTION_MODEL, "language": "zh"},
            "file",
            "audio.mp3",
            audio_path.read_bytes(),
            "audio/mpeg",
        )
        request = UrlRequest(
            f"{OPENAI_BASE_URL}/audio/transcriptions",
            data=body,
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        with urlopen(request, timeout=180) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return str(payload.get("text") or "").strip()
    finally:
        if audio_path:
            audio_path.unlink(missing_ok=True)


def openai_script(title: str, duration_sec: float, transcript: str) -> dict[str, Any] | None:
    if not OPENAI_API_KEY or not transcript:
        return None
    prompt = (
        "请把下面这段短视频转写整理为《剧拆拆》样式的中文短剧剧本。只返回 JSON，不要 Markdown。"
        "按画面顺序保留连续动作，每个可辨识画面用一个 action block；不要写人物表、剧情衔接、场次任务、目标阻力结果、环境说明、独立情绪段或摄影剪辑指令。"
        "JSON 必须包含 version、title、eventChain、settingRules、characters、characterProfiles、scenes；eventChain 记录起因、冲突、转折、结果和钩子，characterProfiles 内包含 id、name、aliases、appearance、clothing、firstAppearance；scenes 内包含 id、heading、location、timeOfDay、interiorExterior、goal、obstacle、result、continuityIn、continuityOut、hook、summary、characters、environment、blocks。"
        "summary、goal、obstacle、result、continuityIn、continuityOut、hook、environment 均留空；每场只保留场次头、出场人物和正文 blocks。"
        "blocks 可使用 action、dialogue、vo、sound、screen_text、transition、emotion；动作要完整写出每一次明确的拳击、推搡、进出和取放，不能漏掉第一步，也不要把指尖发白、血丝等微表情单独拆成块。每个 action 必须明确人物名、视频中实际动词、动作对象和结果，不能省略动作主语或把动作概括成泛化词。"
        "dialogue 必须完整保留转写原话，一人一句，不省略、合并、调换顺序或改写，只恢复中文标点和空白；每个 block 只能有一个 speaker。人物/旁白画外音、内心声、电话声必须使用 vo，speaker 写对应人物或旁白，voKind 区分 os、narration、memory、phone、unknown，不能漏标或把旁白猜成 OS。语气、音量、停顿、重音只有能从音频判断时才填写。"
        "sound 只描述原片中确实听到且有用的动作音效或环境声，不输出背景音乐；必须逐条保留所有可见字幕、人物姓名字幕、免责声明和系统提示，使用 screen_text，不能漏字幕；闪回和回到现实使用 transition。"
        "16. props 必须记录视频中实际出现的关键道具及其状态/证据；跨场景的手机、证物、钱、钥匙等必须在 continuityIn/continuityOut 或 props 中保持可追踪。时间锚点、室内外和门内外变化必须写入场次头或衔接字段，不能让正文自行猜测。"
        f"视频标题：{title}\n视频时长：{duration_sec:.1f} 秒\n转写：{transcript}"
    )
    payload = json.dumps({
        "model": OPENAI_TEXT_MODEL,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": "你是严谨的中文短视频编剧与台词整理助手。"},
            {"role": "user", "content": prompt},
        ],
    }).encode("utf-8")
    request = UrlRequest(
        f"{OPENAI_BASE_URL}/chat/completions",
        data=payload,
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=180) as response:
        result = json.loads(response.read().decode("utf-8"))
    content = result["choices"][0]["message"]["content"]
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.IGNORECASE)
    script = json.loads(content)
    if not isinstance(script, dict) or not isinstance(script.get("scenes"), list):
        return None
    script.setdefault("version", "1.0")
    script.setdefault("title", title)
    return script
