"""Customer-facing task views; internal QA and evidence stay in admin views."""
from __future__ import annotations

import math
from typing import Any

from .script import has_screenplay_body


ISSUE_TEXT = {
    "独立音频转写缺失": "部分台词尚未完成核对，建议对照原视频检查对白是否完整。",
    "独立画面文字校验缺失": "部分画面文字尚未完成核对，建议检查字幕、人物姓名及画面中的文字。",
    "台词尾部截断": "结尾部分的台词可能有遗漏，建议检查视频末尾的对白是否完整。",
    "音频对白不一致": "剧本中的部分台词可能与原视频不一致，建议核对是否存在遗漏或措辞变化。",
    "音频对白需核对": "部分台词需要对照原视频核对，注意原话是否完整、准确。",
    "疑似漏台词片段": "这段对白可能有遗漏，建议对照原视频逐句核对。",
    "漏台词": "这处对白可能不完整，建议核对是否漏字、漏句。",
    "画面文字需核对": "部分字幕、人物姓名或画面文字可能有遗漏，建议对照原视频核对。",
    "疑似漏画面文字片段": "这段画面中的字幕或文字可能有遗漏，建议回看原视频核对。",
    "字幕对白遗漏": "这处字幕可能对应一段未写入剧本的对白，请核对原话及说话人物。",
    "人物姓名缺少依据": "人物姓名尚不能确认，建议核对视频中的称呼、姓名字幕或签名。",
    "台词归属错": "这句台词的说话人物需要核对，请确认是谁说的。",
    "对白置信度低": "这句台词尚不能完全确认，建议对照原视频核对具体措辞。",
    "OS/VO混淆": "这处声音的来源需要核对，请确认是人物对白、旁白还是内心独白。",
    "正文时间缺段": "部分视频内容可能未完整写入剧本，建议检查情节衔接及内容是否遗漏。",
    "剧本正文为空": "尚未生成可用的剧本正文，请重新处理。",
    "格式错误": "这处剧本格式需要核对，请检查场次标题、标点及段落。",
    "空间跳跃": "这处场景或地点变化交代可能不完整，建议核对场景衔接。",
    "时间错乱": "这处情节的时间或先后顺序需要核对。",
    "情绪无证据": "这处人物情绪描述需要对照原视频确认。",
    "情绪链不完整": "这处人物情绪变化的原因可能交代不完整，建议核对。",
    "正文混入校对备注": "这段内容可能混入了检查备注，建议核对是否属于原视频内容。",
    "可拍摄性": "这处动作的执行人物不够明确，建议对照画面核对。",
    "动作概括": "这处动作描述可能过于笼统，建议核对人物、动作及结果。",
    "连续动作未拆分": "这处连续动作可能合并过多，建议按原视频顺序核对。",
    "事实臆造": "这处描述尚不能确认出现在原视频中，建议核对后使用。",
    "音效缺失": "这处声音描述需要对照原视频核对。",
    "画面表演缺失": "这段对白附近的动作或表演描述可能不完整，建议对照画面核对。",
    "反应缺失": "这句台词之后的人物反应可能有遗漏，建议核对。",
    "情绪变化丢失": "这处人物情绪的变化过程可能不完整，建议核对。",
    "人物外形缺失": "人物首次出场的外形或服装描述可能不完整，建议核对。",
}

STAGE_MESSAGES = {
    "uploading": "等待视频上传", "queued": "等待处理", "probing": "正在读取视频信息",
    "compression_queued": "等待视频准备", "compressing": "正在准备视频",
    "video_upload": "正在提交视频", "video_upload_retry": "正在重新提交视频",
    "video_read_retry": "正在重新读取视频", "video_preparing": "正在准备视频",
    "evidence_wait": "正在核对台词和画面文字", "transcribing": "正在整理台词",
    "vision": "正在整理人物和画面内容", "repairing": "正在核对并补全内容",
    "reviewing": "正在核对台词和画面内容", "merging": "正在整理剧本内容",
    "exporting": "正在生成下载文件", "done": "处理完成，剧本可下载",
    "review": "内容检查完成，请查看提示并手动确认复核", "failed": "本次处理未完成，请查看任务详情",
}


def user_issue(issue: Any) -> dict[str, str]:
    source = issue if isinstance(issue, dict) else {}
    location = []
    start, end = source.get("startSec"), source.get("endSec")
    valid_time = lambda n: type(n) in (int, float) and math.isfinite(n) and n >= 0
    if valid_time(start):
        def clock(seconds: float) -> str:
            value = int(seconds)
            return f"{value // 60:02d}:{value % 60:02d}"
        location.append(clock(start) + (f"–{clock(end)}" if valid_time(end) and end >= start else " 附近"))
    scene, block = source.get("scene"), source.get("block")
    if type(scene) is int and scene > 0:
        location.append(f"第 {scene} 场" + (f"第 {block} 段" if type(block) is int and block > 0 else ""))
    tag = source.get("tag")
    text = ISSUE_TEXT.get(tag if isinstance(tag, str) else "", "部分内容尚待确认，建议对照原视频检查台词、人物及画面内容。")
    return {"severity": "重点核对" if source.get("severity") == "P0" else "建议核对",
            "description": (" · ".join(location) + "：" if location else "") + text}


def user_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for event in events:
        stage = event.get("stage")
        stage = stage if stage in STAGE_MESSAGES else "reviewing"
        message = STAGE_MESSAGES[stage]
        if event.get("type") == "user_approved":
            message = "已确认查看检查提示，剧本可下载"
        elif event.get("type") in {"admin_recheck_failed_restored", "admin_recheck_restored"}:
            message = "已恢复上次完成的剧本"
        result.append({"type": "stage", "status": event.get("status"),
                       "stage": stage, "message": message,
                       **{key: event.get(key) for key in ("progressPercent", "durationMs", "createdAt")}})
    return result


def user_result(script: Any) -> Any:
    if not isinstance(script, dict):
        return None
    block_fields = {"type", "text", "speaker", "performance", "emotion", "screenType", "voKind", "isInnerMonologue",
                    "category", "transitionType", "suppressInOutput", "startSec", "endSec"}
    return {"title": script.get("title"), "scenes": [
        {"heading": scene.get("heading"), "characters": scene.get("characters", []),
         "blocks": [{key: value for key, value in block.items() if key in block_fields}
                    for block in (scene.get("blocks") or []) if isinstance(block, dict)]}
        for scene in (script.get("scenes") or []) if isinstance(scene, dict)]}


def user_task(task: dict[str, Any]) -> dict[str, Any]:
    fields = {"id", "title", "status", "stage", "fileName", "episodeNumber", "fileSize", "mimeType",
              "durationSec", "estimatedMinutes", "creditsUsed", "progressPercent", "stageProgressPercent",
              "createdAt", "updatedAt", "completedAt", "batchId", "batchTitle", "batchIndex", "batchTotal"}
    result = {key: value for key, value in task.items() if key in fields}
    quality = task.get("quality") if isinstance(task.get("quality"), dict) else {}
    issues = []
    raw_issues = [issue for key in ("blockingIssues", "issues")
                  for issue in (quality.get(key) if isinstance(quality.get(key), list) else [])]
    for issue in raw_issues:
        public = user_issue(issue)
        if public not in issues:
            issues.append(public)
    result["reviewAvailable"] = bool(issues) or task.get("status") == "review"
    result["quality"] = {"issues": issues}
    result["result"] = user_result(task.get("result"))
    if task.get("status") == "done" and task.get("result") is not None:
        result["downloadable"] = has_screenplay_body(task.get("result"))
    if task.get("error"):
        result["error"] = "本次未能生成可用剧本，请重试或联系管理员查看处理详情。"
    if "events" in task:
        result["events"] = user_events(task["events"])
    return result
