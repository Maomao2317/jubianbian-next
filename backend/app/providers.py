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
    ARK_RESPONSE_POLL_SECONDS,
    ARK_RESPONSE_POLL_TIMEOUT_SECONDS,
    ARK_UPLOAD_PROXY_AUDIO_BITRATE,
    ARK_UPLOAD_PROXY_MAX_HEIGHT,
    ARK_UPLOAD_PROXY_MAX_MB,
    ARK_UPLOAD_PROXY_PRESET,
    ARK_UPLOAD_PROXY_TIMEOUT_SECONDS,
    ARK_UPLOAD_PROXY_VIDEO_BITRATE,
    ARK_RESPONSE_TIMEOUT_SECONDS,
    ARK_THINKING_TYPE,
    ARK_UPLOAD_TIMEOUT_SECONDS,
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
from .evidence import evidence_summary
from .logging_setup import logger, safe_error_text
from .media import multipart_body, probe_duration
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


def _ark_http_error(exc: HTTPError, api_key: str) -> ArkError:
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
    return ArkError(
        f"方舟接口请求失败（HTTP {exc.code}）{suffix}",
        status_code=exc.code,
        provider_code=provider_code,
    )


def ark_http(
    method: str,
    path: str,
    data: bytes | None = None,
    content_type: str = "application/json",
    *,
    api_key: str | None = None,
    timeout: float | None = None,
) -> Any:
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
        with urlopen(request, timeout=timeout or ARK_RESPONSE_TIMEOUT_SECONDS) as response:
            raw = response.read()
    except HTTPError as exc:
        raise _ark_http_error(exc, api_key) from exc
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


def _ark_poll_response(response_id: str, api_key: str) -> Any:
    """Recover a stored streamed response after the original socket closes."""
    deadline = time.monotonic() + ARK_RESPONSE_POLL_TIMEOUT_SECONDS
    encoded_id = quote(response_id, safe="")
    while True:
        try:
            payload = ark_http(
                "GET",
                f"/responses/{encoded_id}",
                api_key=api_key,
                timeout=min(60.0, ARK_RESPONSE_TIMEOUT_SECONDS),
            )
        except ArkError as exc:
            # Ark documents that querying an in-progress response returns an
            # error. The response ID came from response.created, so these
            # transient states are safe to poll until the bounded deadline.
            if exc.status_code in {400, 404, 409, 425} and time.monotonic() < deadline:
                time.sleep(ARK_RESPONSE_POLL_SECONDS)
                continue
            raise
        status = str(payload.get("status") or "").strip().lower() if isinstance(payload, dict) else ""
        if status == "completed" or (not status and ark_response_text(payload)):
            return payload
        if status in {"failed", "incomplete", "cancelled", "canceled"}:
            detail = payload.get("error") or payload.get("incomplete_details") or status
            raise ArkError(f"方舟识别未完成：{detail}")
        if time.monotonic() >= deadline:
            raise ArkError("方舟识别处理超时，已超过后台等待上限")
        time.sleep(ARK_RESPONSE_POLL_SECONDS)


def ark_stream_response(payload: dict[str, Any], api_key: str) -> Any:
    """Create a stored response over SSE and recover it by ID if disconnected."""
    request_payload = {**payload, "stream": True, "store": True}
    request = UrlRequest(
        f"{ARK_BASE_URL}/responses",
        data=json.dumps(request_payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    response_id = ""
    try:
        with urlopen(request, timeout=ARK_RESPONSE_TIMEOUT_SECONDS) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    event = json.loads(data)
                except ValueError:
                    continue
                response_payload = event.get("response") if isinstance(event, dict) else None
                if not isinstance(response_payload, dict):
                    continue
                response_id = str(response_payload.get("id") or response_id)
                event_type = str(event.get("type") or "")
                status = str(response_payload.get("status") or "").lower()
                if event_type == "response.completed" or status == "completed":
                    return response_payload
                if event_type in {"response.failed", "response.incomplete"} or status in {"failed", "incomplete"}:
                    detail = response_payload.get("error") or response_payload.get("incomplete_details") or status
                    raise ArkError(f"方舟识别未完成：{detail}")
    except HTTPError as exc:
        raise _ark_http_error(exc, api_key) from exc
    except (URLError, OSError) as exc:
        if not response_id:
            raise ArkError(f"方舟接口网络错误：{getattr(exc, 'reason', exc)}") from exc
        logger.warning(
            "provider_step provider=ark operation=response_stream_recover response_id=%s error=%s",
            response_id,
            safe_error_text(exc),
        )
    if not response_id:
        raise ArkError("方舟流式响应结束但没有返回 response_id")
    return _ark_poll_response(response_id, api_key)


def _bitrate_bps(value: str) -> int:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kKmM]?)\s*", str(value or ""))
    if not match:
        return 0
    multiplier = {"": 1, "k": 1000, "m": 1_000_000}[match.group(2).lower()]
    return int(float(match.group(1)) * multiplier)


def _ark_proxy_profile(duration_sec: float) -> tuple[str, int]:
    """Fit the proxy near the configured MB ceiling without crushing short clips."""
    configured_video_bps = _bitrate_bps(ARK_UPLOAD_PROXY_VIDEO_BITRATE) or 400_000
    audio_bps = _bitrate_bps(ARK_UPLOAD_PROXY_AUDIO_BITRATE) or 32_000
    if duration_sec <= 0 or ARK_UPLOAD_PROXY_MAX_MB <= 0:
        return ARK_UPLOAD_PROXY_VIDEO_BITRATE, ARK_UPLOAD_PROXY_MAX_HEIGHT
    target_total_bps = int(ARK_UPLOAD_PROXY_MAX_MB * 1024 * 1024 * 8 * 0.92 / duration_sec)
    target_video_bps = max(96_000, target_total_bps - audio_bps - 16_000)
    video_bps = min(configured_video_bps, target_video_bps)
    max_height = ARK_UPLOAD_PROXY_MAX_HEIGHT
    if video_bps < 180_000:
        max_height = min(max_height, 360)
    elif video_bps < 300_000:
        max_height = min(max_height, 480)
    return f"{max(1, video_bps // 1000)}k", max_height


def _ark_upload_proxy(path: Path, duration_sec: float = 0) -> tuple[Path, Path | None]:
    """Create a temporary smaller MP4 when the source is expensive to upload.

    The original file remains untouched.  If ffmpeg is unavailable or the
    proxy cannot be produced, callers fall back to the source transparently.
    """
    if ARK_UPLOAD_PROXY_MAX_MB <= 0:
        return path, None
    try:
        source_size = path.stat().st_size
    except OSError:
        return path, None
    if source_size <= ARK_UPLOAD_PROXY_MAX_MB * 1024 * 1024:
        return path, None
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        logger.warning("provider_step provider=ark operation=proxy_unavailable file=%s reason=ffmpeg_missing", path.name)
        return path, None

    proxy_dir = Path(tempfile.mkdtemp(prefix="jbb-ark-upload-", dir=DATA_DIR))
    proxy_path = proxy_dir / f"{path.stem}.ark.mp4"
    started = time.perf_counter()
    duration = duration_sec or probe_duration(path)
    video_bitrate, max_height = _ark_proxy_profile(duration)
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-vf",
        f"scale='min({max_height},iw)':-2",
        "-c:v",
        "libx264",
        "-preset",
        ARK_UPLOAD_PROXY_PRESET,
        "-b:v",
        video_bitrate,
        "-maxrate",
        video_bitrate,
        "-bufsize",
        video_bitrate,
        "-c:a",
        "aac",
        "-b:a",
        ARK_UPLOAD_PROXY_AUDIO_BITRATE,
        "-movflags",
        "+faststart",
        "-y",
        str(proxy_path),
    ]
    try:
        subprocess.run(
            command,
            capture_output=True,
            timeout=ARK_UPLOAD_PROXY_TIMEOUT_SECONDS,
            check=True,
        )
        proxy_size = proxy_path.stat().st_size
        if proxy_size <= 0 or proxy_size >= source_size:
            raise OSError("proxy_not_smaller")
        logger.info(
            "provider_step provider=ark operation=proxy file=%s source_bytes=%s proxy_bytes=%s bitrate=%s height=%s duration_ms=%.1f",
            path.name,
            source_size,
            proxy_size,
            video_bitrate,
            max_height,
            (time.perf_counter() - started) * 1000,
        )
        return proxy_path, proxy_dir
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning(
            "provider_step provider=ark operation=proxy_failed file=%s error=%s",
            path.name,
            safe_error_text(exc),
        )
        shutil.rmtree(proxy_dir, ignore_errors=True)
        return path, None


def ark_upload_video(path: Path, api_key: str | None = None, duration_sec: float = 0) -> str:
    # The Files API accepts a local video and lets the model reuse it by file_id.
    # The upload is intentionally done in the worker thread so FastAPI stays responsive.
    upload_path, proxy_dir = _ark_upload_proxy(path, duration_sec)
    try:
        fields = {
            "purpose": "user_data",
            "preprocess_configs": json.dumps({"video": {"fps": ARK_VIDEO_FPS}}, separators=(",", ":")),
        }
        body, boundary = multipart_body(
            fields,
            "file",
            upload_path.name,
            upload_path.read_bytes(),
            "video/mp4",
        )
        payload = ark_http(
            "POST",
            "/files",
            body,
            f"multipart/form-data; boundary={boundary}",
            api_key=api_key,
            timeout=ARK_UPLOAD_TIMEOUT_SECONDS,
        )
        file_id = None
        if isinstance(payload, dict):
            file_id = payload.get("id") or payload.get("file_id")
        if not file_id:
            raise ArkError("方舟上传成功但没有返回 file_id")
        return str(file_id)
    finally:
        if proxy_dir is not None:
            shutil.rmtree(proxy_dir, ignore_errors=True)


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


def ark_prompt(title: str, duration_sec: float, evidence: dict[str, Any] | None = None) -> str:
    evidence_fragment = evidence_summary(evidence)
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
        "3a. 只要画面从院门到厅堂、厅堂到院外、化妆区到拍摄区等明确换地点或换连续空间，就必须拆成新的场次，重新编号并填写该场实际出场人物；heading 必须使用‘1-2 日 内转外 地点/地点’或‘1-3 日 外 地点’这类可读格式，不能把多个地点塞进一个场次。时间或内外景发生连续转换时，timeOfDay/interiorExterior 可写‘傍晚转夜’、‘内转外’、‘外转内’。"
        "3b. 出场人物只列本场画面中出现或实际说话的人，不要把全片人物表、剧组全体、路人和未入画工作人员复制到每一场；只有动作或对白确实出现时才列入。"
        "4. 连续动作要完整覆盖因果和数量：每一次明确的拳击、推搡、开门、拿取、进出、转身离开都不能漏写；如果是第一拳、第二拳，要按实际顺序明确写出，不能只写‘打了几拳’或只写最后结果，也不能凭空增加动作。动作必须保留视频里实际听见/看见的动词和对象，不要把‘推’改写成‘撞’、把‘拿’概括成‘处理’。每个 action 必须包含规范人物名、动作、对象和结果，必要时补充可执行的表演提示；不要让句子以‘狠狠拽住’、‘随后离开’、‘故意看向’这类无主语短语开头；看不清时写‘未知人物’，不要猜。"
        "4b. 每条▲画面动作控制在主体+动词+对象+结果的一到两句，按连续动作拆开，避免把整段剧情、环境说明和多个人物的多轮行为塞进一个长句；不要重复同一人物的外形和同一场景陈设。"
        "4a. 人物第一次在正文 action 中出现时，必须把视频中清楚可见的外形、发型、服装或年龄外观直接写在人物名前，不能只写人物姓名、把信息留在 characterProfiles 里；例如‘梳双丸子头、穿红碎花袄的小女孩希希趴在米缸口……’、‘穿深色工装的王铁柱用渔网……’。后续动作中若外形仍清晰且有助于区分人物，也保留关键识别词。只写视频实际看见的内容，不要猜测。"
        "5. 指尖发白、眼里有血丝、眼神一闪、呼吸变化等只作为动作段中的补充，不得单独成为一个镜头/block；只有它直接改变人物决定或剧情结果时才保留。"
        "6. 严禁写‘特写、近景、正反打、镜头切到、推近、拉远、俯拍、仰拍、画面给到’等拍摄或剪辑指令。后期剪辑处理不进入剧本正文。"
        "7. environment 必须留空；画面中的地点、人物关系和动作直接写进 action block，不另设环境段落。"
        "8. 人物第一次出现时，必须在 characterProfiles 的 firstAppearance、appearance 或 clothing 中补齐视频实际可见的识别信息；appearance 和 clothing 要写具体发型、服装、颜色、体貌等可见细节，并与正文第一次 action 中的外形描述保持一致；只写能帮助表演的外观/服装，不要猜年龄、身份或剧情之外的颜色饰品。看不清就留空并标记需要核对。"
        "9. dialogue 必须一人一句、完整保留原话，不能改写成剧情概括，不能省略、合并、调换顺序或补写听不清的内容。台词文字是不可改写字段：只允许恢复中文标点和清理空白，不能替换同义词。每个 dialogue block 只能对应一个 speaker；发现说话人切换就拆成多个 block，仍逐字保留。先根据语气和语法恢复准确中文标点：逗号分隔分句，句末使用‘。’、‘？’、‘！’或‘……’，不要输出无标点的长句，也不要连续重复标点。听不清的人名、称谓和代词保留‘[听不清]’，confidence 写 low、uncertain 写 true。"
        "9a. 对白括号只保留一到两个简短、可执行的情绪/状态词，例如‘震惊’、‘急切呼喊’、‘语气坚定’；不要输出音高、音量、停顿、重音、语气分析、同义重复或完整表演说明。不要把两个人的台词合并到一个 speaker 下，下一位说话人必须新建 dialogue block。"
        "10. 情绪必须绑定可观察证据：先写画面动作/声音，再写 emotionType、emotionIntensity、emotionTrigger、emotionChange、emotionTarget、emotionEvidence；看不清时标 emotion_uncertain，不得编造心理。对白的 speechTone 只有视频确实能判断时才填写。关键转折可单独使用 emotion block，但不能只写场次总结。"
        "11. 人物或旁白的画外音、内心声、回忆声、电话另一端声音必须使用 type=vo，并在 speaker 写对应人物名或‘旁白’，不能混成普通 dialogue，也不能漏标。voKind 必须分别写 os（明确是人物内心独白）、narration（旁白/画外音）、memory（回忆声）或 phone（电话另一端）；没有明确证据时写 unknown，不得把普通旁白猜成 OS。若声音来源无法确认，speaker 写‘未知说话人’并标 uncertain。没有声音依据时不能臆造 VO。"
        "11a. 画面中能看到人物开口说话的内容一律是 dialogue，不能只用 screen_text 替代；如果画面同时确实显示这句对白字幕，除 dialogue 外再保留一条 screen_text。screen_text 还记录画面上额外出现的姓名条、标题、免责声明、系统提示或非对白文字。人物在画外说话才是 VO；人物自己的内心独白才是 OS，不能因为句子像旁白就把普通对白标成 VO/OS。"
        "12. 正式扒剧本不输出背景音乐；sound 只保留原片确实听到且对剧情有作用的动作音效或环境声。必须逐条识别所有画面字幕，包括人物姓名字幕、对白字幕、作品/AI免责声明、系统提示、标题字样和转场字幕，全部使用 screen_text 单独标记并保持出现顺序，不能漏掉、合并或改写；闪回、回到现实和闪白等使用 transition。"
        "13. characterProfiles 中为每个人建立唯一 id、规范 name 和 aliases；正文所有 speaker 和 characters 必须使用同一个规范 name，不能混用‘男主’、‘江川’等称呼。"
        "14. recap、trailer、title_card 或 credits 不作为新剧情场景，但其中实际出现的字幕必须保留为 screen_text；不要因其属于片头、片尾或包装而丢弃字幕。"
        "15. startSec 和 endSec 填写画面或声音的大致秒数并保持顺序；时间只用于回看定位，不要据此拆成镜头。无法判断时才填 0。每个关键行为都要能回溯到 eventChain 或视频时间证据；不要为没有证据的动机、设定、道具状态或心理活动补写事实。"
        "没有把握的内容使用‘不明’、‘未知说话人’或‘[听不清]’，不要编造视频之外的事实；优先保证剧情因果、台词准确、动作动词保真和人物一致。"
        "16. 本提示中的希希、王铁柱、米缸、河流和服装只是格式示例，绝不能套用到其他视频；先根据当前视频真实判断题材、时代、地域、人物关系和叙事语气。无论都市、家庭、校园、职场、古装、悬疑、动作、喜剧、纪录或其他类型，都只记录当前视频实际出现的画面、声音、字幕和动作，不因题材不同而省略关键事件，也不凭类型套路补写剧情。"
        "17. 输出前做一次正文纯净度检查：参考文档中的校对备注、缺漏标注和操作指令（例如‘这里不需要展示’、‘缺少某人台词’、‘少某人说话’、‘这几句是旁白’、‘画面应该是2-2’、‘这一大段不需要’、‘待补充’）不是视频内容，严禁写进 action、dialogue、vo 或 screen_text；只有视频真实出现的文字才可写入 screen_text。"
        "18. 同一 action 不得把多个角色的多轮动作拼成一条长总结；一个 action 只写同一主体的一次连续可见动作，出现‘拿起—递给—接过’、‘开门—走出—关门’或多人先后动作时按先后拆成多个 block，并保留每一步的对象和结果。"
        "19. 字幕与对白双重保真：人物开口内容写 dialogue；若画面同时确实显示对应字幕，另外保留一条同时间段的 screen_text，不得用其中一种替代另一种，也不得把多条字幕合并成剧情概括。"
        "20. 场次编号先按视频/文件中明确的集数分组，再在每集内从1递增（2-1、2-2、3-1、3-2）；批量视频不能把所有场次统一改成第一集或按全局序号覆盖集数。"
        f"视频标题：{title}；视频时长：{duration_sec:.1f} 秒。"
        f"\n{evidence_fragment}"
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


def ark_recognize(
    path: Path,
    title: str,
    duration_sec: float,
    evidence: dict[str, Any] | None = None,
    model: str | None = None,
) -> tuple[dict[str, Any], dict[str, int | None]]:
    api_key = ark_account_key()
    selected_model = (model or ARK_MODEL).strip() or ARK_MODEL
    started = time.perf_counter()
    logger.info("provider_start provider=ark operation=video_recognize file=%s model=%s duration_sec=%.1f", path.name, selected_model, duration_sec)
    upload_started = time.perf_counter()
    file_id = ark_upload_video(path, api_key, duration_sec)
    logger.info("provider_step provider=ark operation=upload file=%s duration_ms=%.1f", path.name, (time.perf_counter() - upload_started) * 1000)
    wait_started = time.perf_counter()
    ark_wait_for_file(file_id, api_key)
    logger.info("provider_step provider=ark operation=file_ready file=%s duration_ms=%.1f", path.name, (time.perf_counter() - wait_started) * 1000)
    response_started = time.perf_counter()
    response_payload: dict[str, Any] = {
            "model": selected_model,
            "input": [{
                "role": "user",
                "content": [
                    {"type": "input_video", "file_id": file_id},
                    {"type": "input_text", "text": ark_prompt(title, duration_sec, evidence)},
                ],
            }],
        }
    if ARK_THINKING_TYPE in {"enabled", "disabled", "auto"}:
        response_payload["thinking"] = {"type": ARK_THINKING_TYPE}
    payload = ark_stream_response(response_payload, api_key)
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
        "provider_done provider=ark operation=video_recognize file=%s model=%s duration_ms=%.1f scenes=%s",
        path.name,
        selected_model,
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


def openai_script(title: str, duration_sec: float, transcript: str, evidence: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if not OPENAI_API_KEY or not transcript:
        return None
    prompt = (
        "请把下面这段短视频转写整理为《剧拆拆》样式的中文短剧剧本。只返回 JSON，不要 Markdown。"
        "按画面顺序保留连续动作，每个可辨识画面用一个 action block；不要写人物表、剧情衔接、场次任务、目标阻力结果、环境说明、独立情绪段或摄影剪辑指令。"
        "JSON 必须包含 version、title、eventChain、settingRules、characters、characterProfiles、scenes；eventChain 记录起因、冲突、转折、结果和钩子，characterProfiles 内包含 id、name、aliases、appearance、clothing、firstAppearance；scenes 内包含 id、heading、location、timeOfDay、interiorExterior、goal、obstacle、result、continuityIn、continuityOut、hook、summary、characters、environment、blocks。"
        "summary、goal、obstacle、result、continuityIn、continuityOut、hook、environment 均留空；每场只保留场次头、出场人物和正文 blocks。"
        "只要画面从一个明确空间切换到另一个空间就拆新场次，重新编号并只列该场实际出现/说话的人；heading 要写清日夜、内外景和地点，允许‘内转外’、‘外转内’、‘傍晚转夜’，不能把多个镜头地点压成一个场次。"
        "blocks 可使用 action、dialogue、vo、sound、screen_text、transition、emotion；动作要完整写出每一次明确的拳击、推搡、进出和取放，不能漏掉第一步，也不要把指尖发白、血丝等微表情单独拆成块。每个 action 必须明确人物名、视频中实际动词、动作对象和结果，不能省略动作主语或把动作概括成泛化词。人物第一次在正文 action 中出现时，要把可见发型、服装、颜色和体貌直接写在人物名前，例如‘梳双丸子头、穿红碎花袄的小女孩希希趴在米缸口……’；不能只把外形放在 characterProfiles。"
        "每条 action 只写一到两句可拍摄的主体、动词、对象和结果，连续动作拆成多个 block，不重复整段环境或同一人物外形。"
        "dialogue 必须完整保留转写原话，一人一句，不省略、合并、调换顺序或改写，只恢复中文标点和空白；每个 block 只能有一个 speaker。画面中人物开口说话必须是 dialogue，不能只写成字幕或 VO；若视频同时有对白字幕，另外保留 screen_text。只有画外音/电话另一端/回忆声使用 vo，人物明确的内心独白才使用 os。对白括号只写一到两个简短情绪词，不写音高、音量、停顿、重音等技术分析。"
        "sound 只描述原片中确实听到且有用的动作音效或环境声，不输出背景音乐；必须逐条保留所有额外画面文字、人物姓名条、免责声明和系统提示，使用 screen_text，不能把人物实际说出的台词误写成 screen_text；闪回和回到现实使用 transition。"
        "16. props 必须记录视频中实际出现的关键道具及其状态/证据；跨场景的手机、证物、钱、钥匙等必须在 continuityIn/continuityOut 或 props 中保持可追踪。时间锚点、室内外和门内外变化必须写入场次头或衔接字段，不能让正文自行猜测。"
        "17. 提示中的人物和场景名称只是格式示例，不能照搬；针对当前视频自适应识别题材、时代、地点、人物关系和叙事语气，覆盖现代、古装、校园、职场、悬疑、动作、喜剧等各种类型，只写视频证据，不套用类型套路或示例剧情。"
        "18. 严禁把参考文档的校对备注或操作指令写进正文，例如‘这里不需要展示’、‘缺少某人台词’、‘少某人说话’、‘画面应该是2-2’、‘这一大段不需要’、‘待补充’；这些不是视频内容。"
        "19. 同一 action 只写一个主体的一次连续可见动作；拿起—递给—接过、开门—走出—关门以及多人先后动作必须按视频顺序拆开，每一步都保留对象和结果，不能压成剧情总结。"
        "20. 人物台词写 dialogue；画面确实存在的对白字幕同时单独保留 screen_text，字幕按出现顺序逐条记录，不能漏写、合并或改写。场次先按明确集数分组，再在每集内从1递增，输出2-1、2-2、3-1、3-2这类编号。"
        f"视频标题：{title}\n视频时长：{duration_sec:.1f} 秒\n转写：{transcript}\n"
        f"{evidence_summary(evidence)}"
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
