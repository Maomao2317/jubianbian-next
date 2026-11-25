"""Authenticated staging workspace for planning and writing adaptations."""

from __future__ import annotations

import io
import json
import sqlite3
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile
from contextlib import closing
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response
from pydantic import Field

from . import adaptation as ad
from .auth_store import current_user, db, now_iso
from .config import JBB_ENVIRONMENT
from .logging_setup import logger
from .script import screenplay_output_scenes, script_to_markdown
from .task_store import row_to_task
from .user_view import user_task

router = APIRouter(prefix="/api/adaptations")
ENABLED = JBB_ENVIRONMENT in {"staging", "development", "dev", "local"}
LEASE_SECONDS = 45 * 60
MAX_FILE_BYTES = 2 * 1024 * 1024
PROJECT_POINTS = 16
EPISODE_POINTS = 16
REWRITE_POINTS = 8


class ProjectInput(ad.Contract):
    title: str = Field(default="", max_length=100)
    goal: str = Field(default="", max_length=1000)
    keep: str = Field(default="", max_length=1200)
    episode_count: int = Field(ge=1, le=ad.MAX_EPISODES)
    task_id: str = Field(default="", max_length=100)
    source_text: str = Field(default="", max_length=ad.MAX_SOURCE_CHARS)


class JobInput(ad.Contract):
    kind: Literal["analyze", "preview", "plan", "episode", "impact", "review_all"]
    revision: int
    notes: str = Field(default="", max_length=1000)
    keep_notes: str = Field(default="", max_length=1200)
    source_id: str = Field(default="", max_length=20)
    direction_id: str = Field(default="", max_length=20)
    keep_option_ids: list[str] = Field(default_factory=list, max_length=12)
    episode: int = Field(default=1, ge=1, le=ad.MAX_EPISODES)


class PlanInput(ad.Contract):
    revision: int
    plan: ad.Plan
    locks: list[ad.LockLine] = Field(max_length=40)


class SourceInput(ad.Contract):
    revision: int
    source_id: str = Field(max_length=20)
    text: str = Field(min_length=1, max_length=12000)


class DraftInput(ad.Contract):
    revision: int
    draft: ad.Draft


class OutputLock(ad.Contract):
    revision: int
    locked: bool


def init_adaptations():
    with closing(db()) as conn, conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS adaptations (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL, title TEXT NOT NULL,
                state_json TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
                job_id TEXT, job_kind TEXT, job_started REAL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS adaptations_owner ON adaptations(user_id, updated_at);
            CREATE TABLE IF NOT EXISTS adaptation_attempts (
                user_id TEXT NOT NULL, started REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS adaptation_attempts_time ON adaptation_attempts(started);
            CREATE TABLE IF NOT EXISTS adaptation_jobs (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, project_title TEXT NOT NULL,
                user_id TEXT NOT NULL, kind TEXT NOT NULL, billing_kind TEXT NOT NULL,
                episode INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL,
                credits_reserved INTEGER NOT NULL DEFAULT 0,
                credits_charged INTEGER NOT NULL DEFAULT 0,
                credits_refunded INTEGER NOT NULL DEFAULT 0,
                provider TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
                prompt_version TEXT NOT NULL DEFAULT '', input_tokens INTEGER,
                output_tokens INTEGER, api_cost_rmb REAL, cost_source TEXT NOT NULL DEFAULT 'unpriced',
                calls_json TEXT NOT NULL DEFAULT '[]', error TEXT NOT NULL DEFAULT '',
                started_at TEXT NOT NULL, finished_at TEXT
            );
            CREATE INDEX IF NOT EXISTS adaptation_jobs_time ON adaptation_jobs(started_at DESC);
            CREATE INDEX IF NOT EXISTS adaptation_jobs_owner ON adaptation_jobs(user_id, started_at DESC);
            CREATE INDEX IF NOT EXISTS adaptation_jobs_project ON adaptation_jobs(project_id, started_at DESC);
            CREATE TABLE IF NOT EXISTS adaptation_episode_versions (
                project_id TEXT NOT NULL, episode INTEGER NOT NULL, version INTEGER NOT NULL,
                screenplay TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (project_id, episode, version)
            );
        """)
        _recover_expired_jobs(conn, time.time())


def admin_adaptations(
    request: Request,
    keyword: str = "",
    status: str = "all",
    kind: str = "all",
    user_id: str = "",
    date_from: str = "",
    date_to: str = "",
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict:
    user = current_user(request)
    if user["role"] != "admin":
        _fail("无管理员权限", 403)
    if status not in {"all", "running", "success", "failed"}:
        _fail("不支持的改编任务状态", 400)
    if kind != "all" and kind not in {"analyze", "preview", "plan", "episode", "impact", "review_all"}:
        _fail("不支持的改编任务分类", 400)

    clauses: list[str] = []
    params: list[object] = []
    if status != "all":
        clauses.append("j.status=?")
        params.append(status)
    if kind != "all":
        clauses.append("j.kind=?")
        params.append(kind)
    if user_id:
        clauses.append("j.user_id=?")
        params.append(user_id)
    if keyword.strip():
        value = f"%{keyword.strip()}%"
        clauses.append("(j.project_title LIKE ? OR u.name LIKE ? OR u.email LIKE ?)")
        params.extend((value, value, value))
    if date_from:
        clauses.append("j.started_at>=?")
        params.append(f"{date_from}T00:00:00")
    if date_to:
        clauses.append("substr(j.started_at,1,10)<=?")
        params.append(date_to)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""

    with closing(db()) as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM adaptation_jobs j LEFT JOIN users u ON u.id=j.user_id" + where,
            params,
        ).fetchone()[0]
        rows = conn.execute(
            """SELECT j.*, u.name AS user_name, u.email AS user_email
               FROM adaptation_jobs j LEFT JOIN users u ON u.id=j.user_id""" + where
            + " ORDER BY j.started_at DESC, j.id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        groups = conn.execute(
            """SELECT j.kind, j.status, COUNT(*) AS jobs,
                      COALESCE(SUM(j.input_tokens),0) AS input_tokens,
                      COALESCE(SUM(j.output_tokens),0) AS output_tokens,
                      COALESCE(SUM(j.credits_charged),0) AS credits_charged,
                      COALESCE(SUM(j.credits_refunded),0) AS credits_refunded,
                      SUM(j.api_cost_rmb) AS api_cost_rmb,
                      SUM(CASE WHEN j.api_cost_rmb IS NULL THEN 1 ELSE 0 END) AS unpriced_jobs
               FROM adaptation_jobs j LEFT JOIN users u ON u.id=j.user_id""" + where
            + " GROUP BY j.kind, j.status ORDER BY j.kind, j.status",
            params,
        ).fetchall()

    items = []
    for row in rows:
        item = dict(row)
        try:
            item["calls"] = json.loads(item.pop("calls_json") or "[]")
        except (TypeError, ValueError):
            item["calls"] = []
        items.append(item)
    return {"total": total, "items": items, "categories": [dict(row) for row in groups]}


def _fail(message: str, status: int = 422):
    raise HTTPException(status_code=status, detail=message)


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _job_label(kind: str) -> str:
    return {
        "analyze": "原稿分析与改编建议",
        "preview": "代表场次试改",
        "plan": "分集大纲",
        "episode": "分集正文与审稿",
        "impact": "大纲影响分析",
        "review_all": "整剧一致性审稿",
    }.get(kind, "剧本改编")


def _adjust_credits(conn, user_id: str, amount: int, entry_type: str, reason: str) -> int:
    user = conn.execute("SELECT credits FROM users WHERE id=?", (user_id,)).fetchone()
    if not user:
        _fail("用户账户不存在，无法结算积分", 409)
    balance = int(user["credits"] or 0) + amount
    if amount < 0:
        changed = conn.execute(
            "UPDATE users SET credits=credits+? WHERE id=? AND credits>=?",
            (amount, user_id, -amount),
        ).rowcount
        if changed != 1:
            _fail("积分不足，请先充值后再生成", 402)
    else:
        conn.execute("UPDATE users SET credits=credits+? WHERE id=?", (amount, user_id))
    conn.execute(
        """INSERT INTO credit_ledger(user_id, amount, balance_after, entry_type, reason, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (user_id, amount, balance, entry_type, reason[:200], now_iso()),
    )
    return balance


def _recover_expired_jobs(conn: sqlite3.Connection, now: float) -> None:
    expired = conn.execute(
        "SELECT * FROM adaptations WHERE job_id IS NOT NULL AND job_started<?",
        (now - LEASE_SECONDS,),
    ).fetchall()
    for project in expired:
        job = conn.execute("SELECT * FROM adaptation_jobs WHERE id=?", (project["job_id"],)).fetchone()
        if job and job["status"] == "running":
            refund = max(0, int(job["credits_reserved"] or 0) - int(job["credits_refunded"] or 0))
            if refund:
                _adjust_credits(conn, job["user_id"], refund, "refund",
                                f"剧本改编失败退回 · {job['project_title']} · {_job_label(job['kind'])}")
            conn.execute(
                """UPDATE adaptation_jobs SET status='failed', credits_refunded=?, error=?, finished_at=?
                   WHERE id=? AND status='running'""",
                (int(job["credits_refunded"] or 0) + refund, "任务超时中断，已退回预扣积分。", now_iso(), job["id"]),
            )
        state = json.loads(project["state_json"])
        state.update(error="上次生成中断，预扣积分已退回。", errorKind=project["job_kind"])
        conn.execute(
            """UPDATE adaptations SET job_id=NULL, job_kind=NULL, job_started=NULL, state_json=?
               WHERE id=? AND job_id=?""",
            (_dump(state), project["id"], project["job_id"]),
        )


def _reserved_points(state: dict, job: JobInput) -> tuple[int, str]:
    if job.kind == "analyze":
        return (PROJECT_POINTS, "project") if not state.get("setupPaid") else (0, "included")
    if job.kind == "preview" and state.get("previewUsed"):
        return REWRITE_POINTS, "preview_retry"
    if job.kind == "plan" and state.get("planUsed"):
        return REWRITE_POINTS, "plan_retry"
    if job.kind == "episode":
        output = state["outputs"].get(str(job.episode))
        if output is not None:
            return (REWRITE_POINTS, "rewrite") if output.get("version", 1) > 1 else (0, "included_revision")
        if job.episode == 1:
            return 0, "included_episode"
        return EPISODE_POINTS, "episode"
    if job.kind == "review_all" and state.get("reviewAllUsed"):
        return REWRITE_POINTS, "review"
    if job.kind == "impact" and state.get("impactChecked"):
        return REWRITE_POINTS, "impact"
    return 0, "included"


def _get(conn: sqlite3.Connection, project_id: str, owner: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM adaptations WHERE id=? AND user_id=?", (project_id, owner)).fetchone()
    if not row:
        _fail("找不到这个改编项目", 404)
    return row


def _state(row: sqlite3.Row) -> dict:
    return json.loads(row["state_json"])


def _project(row: sqlite3.Row) -> dict:
    state = _state(row)
    if row["job_id"] and row["job_started"] < time.time() - LEASE_SECONDS:
        state.update(error="上次生成中断，可重新提交。", errorKind=row["job_kind"])
        job = None
    else:
        job = row["job_kind"]
    billing = {
        "projectPoints": PROJECT_POINTS,
        "episodePoints": EPISODE_POINTS,
        "rewritePoints": REWRITE_POINTS,
        "setupPaid": bool(state.get("setupPaid")),
    }
    return {**state, "id": row["id"], "title": row["title"], "revision": row["revision"],
            "updatedAt": row["updated_at"], "job": job, "billing": billing}


def _idle(row: sqlite3.Row, revision: int | None = None):
    if row["job_id"] and row["job_started"] >= time.time() - LEASE_SECONDS:
        _fail("正在生成，请等本次处理结束", 409)
    if revision is not None and row["revision"] != revision:
        _fail("项目已更新，请刷新后再编辑", 409)


def _save(conn, row, state):
    conn.execute(
        """UPDATE adaptations SET state_json=?,revision=revision+1,updated_at=?,
           job_id=NULL,job_kind=NULL,job_started=NULL WHERE id=?""",
        (_dump(state), now_iso(), row["id"]),
    )


def _task_source(task_id: str, owner: str):
    with closing(db()) as conn, conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=? AND user_id=?", (task_id, owner)).fetchone()
        if not row:
            _fail("找不到这份平台剧本", 404)
        if row["batch_id"]:
            batch = conn.execute(
                "SELECT * FROM tasks WHERE user_id=? AND batch_id=? ORDER BY batch_index, created_at",
                (owner, row["batch_id"]),
            ).fetchall()
            rows = batch if all(item["status"] in {"done", "review"} for item in batch) else [row]
        else:
            rows = [row]
    if row["status"] not in {"done", "review"}:
        _fail("请从已完成且有正文的剧本创建改编")
    scenes, warnings = [], []
    if row["batch_id"] and (row["batch_total"] or 1) > 1 and len(rows) == 1:
        warnings.append("当前仅导入已打开的一集。整批完成后可从结果页导入全剧。")
    for item in rows:
        task = row_to_task(item)
        script = task.get("delivery") or task.get("result") or {}
        for issue in user_task(task)["quality"]["issues"]:
            if issue.get("severity") == "重点核对" and len(warnings) < 30:
                warnings.append(f"{task['title']} · {issue['description']}")
        for scene in screenplay_output_scenes(script):
            # Reuse the existing delivery formatter on each scene only.
            text = script_to_markdown({**task, "delivery": {**script, "scenes": [scene]}})
            text = text.partition("\n")[2].strip()
            heading = text.partition("\n")[0]
            raw_heading = scene["heading"]
            if heading != raw_heading:
                text = raw_heading + text[len(heading):]
            scenes.append(ad.SourceScene(
                id=f"S{len(scenes) + 1}", label=f"{task['title']} · {raw_heading}"[:140],
                text=text, task_id=item["id"], start_sec=scene.get("startSec"),
            ))
    if not scenes or sum(len(scene.text) for scene in scenes) > ad.MAX_SOURCE_CHARS:
        _fail("原稿为空或超过 60,000 字符，请导出需要改编的部分后上传。")
    return row["batch_title"] or row["title"], scenes, warnings


def _read_document(raw: bytes, filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext in {".txt", ".md"}:
        encodings = ("utf-16",) if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else ("utf-8-sig", "gb18030")
        for encoding in encodings:
            try:
                return raw.decode(encoding)
            except UnicodeError:
                continue
        raise ad.AdaptationError("无法读取编码，请另存为 UTF-8 文本后上传。")
    if ext != ".docx":
        raise ad.AdaptationError("请上传 DOCX、TXT 或 Markdown。")
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entry = archive.getinfo("word/document.xml")
            if entry.file_size > 4 * MAX_FILE_BYTES or entry.flag_bits & 1:
                raise ad.AdaptationError("Word 正文过大或已加密，请另存选中的集数。")
            xml = archive.read(entry)
        if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
            raise ad.AdaptationError("Word 文档结构不受支持，请另存为 TXT。")
        paragraphs = ET.fromstring(xml).iter(ns + "p")
        return "\n".join("".join(node.text or "" if node.tag == ns + "t" else
                                     "\n" if node.tag == ns + "br" else "\t"
                                     for node in p.iter() if node.tag in {ns + "t", ns + "br", ns + "tab"})
                         for p in paragraphs)
    except (zipfile.BadZipFile, KeyError, ET.ParseError, RuntimeError, NotImplementedError):
        raise ad.AdaptationError("无法读取这个 Word 文档，请另存为 DOCX 或 TXT。") from None


@router.get("/availability")
def availability(request: Request):
    current_user(request)
    try:
        provider, model = ad.model_choice()
        return {"enabled": True, "model": model, "provider": provider}
    except ad.AdaptationError:
        return {"enabled": True, "model": None}


@router.post("/parse")
async def parse_document(request: Request, file: UploadFile = File(...)):
    current_user(request)
    try:
        raw = await file.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            _fail("文件最大 2 MB，请按集分开上传", 413)
        text = _read_document(raw, file.filename or "").strip()
        if len(text) > ad.MAX_SOURCE_CHARS:
            _fail("正文超过 60,000 字符，请拆成几份再上传", 413)
        if not text or "\x00" in text:
            _fail("这个文件没有可读取的正文")
        return {"text": text}
    except ad.AdaptationError as exc:
        _fail(str(exc))
    finally:
        await file.close()


@router.get("")
def list_projects(request: Request):
    owner = current_user(request)["id"]
    with closing(db()) as conn, conn:
        return [dict(row) for row in conn.execute(
            "SELECT id,title,updated_at AS updatedAt FROM adaptations WHERE user_id=? ORDER BY updated_at DESC LIMIT 50",
            (owner,),
        )]


@router.post("", status_code=201)
def create_project(request: Request, body: ProjectInput):
    owner = current_user(request)["id"]
    if bool(body.task_id) == bool(body.source_text):
        _fail("请从平台剧本或上传正文中选择一份原稿")
    try:
        if body.task_id:
            title, scenes, warnings = _task_source(body.task_id, owner)
        else:
            title, scenes, warnings = "上传剧本", ad.split_script(body.source_text), []
    except ad.AdaptationError as exc:
        _fail(str(exc))
    project_id = uuid.uuid4().hex
    state = {"goal": body.goal, "keep": body.keep, "projectGoal": body.goal, "projectKeep": body.keep,
             "episodeCount": body.episode_count,
             "projectTitle": body.title or title,
             "sources": [scene.model_dump() for scene in scenes], "warnings": warnings,
             "outputs": {}, "locks": [], "usage": {}, "reviews": {}, "confirmed": False,
             "setupPaid": False, "previewUsed": False, "planUsed": False,
             "selectedKeepOptionIds": None, "titleFromSource": not bool(body.title.strip())}
    with closing(db()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT COUNT(*) FROM adaptations WHERE user_id=?", (owner,)).fetchone()[0] >= 50:
            _fail("测试账户最多保留 50 个项目，请先删除不用的项目。", 409)
        conn.execute("INSERT INTO adaptations(id,user_id,title,state_json,updated_at) VALUES(?,?,?,?,?)",
                     (project_id, owner, body.title or title, _dump(state), now_iso()))
        return _project(_get(conn, project_id, owner))


@router.get("/{project_id}")
def get_project(project_id: str, request: Request):
    owner = current_user(request)["id"]
    with closing(db()) as conn, conn:
        return _project(_get(conn, project_id, owner))


@router.get("/{project_id}/progress")
def get_progress(project_id: str, request: Request):
    owner = current_user(request)["id"]
    with closing(db()) as conn:
        row = _get(conn, project_id, owner)
        running = row["job_id"] and row["job_started"] >= time.time() - LEASE_SECONDS
        return {"job": row["job_kind"] if running else None}


@router.delete("/{project_id}", status_code=204)
def delete_project(project_id: str, request: Request):
    owner = current_user(request)["id"]
    with closing(db()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _get(conn, project_id, owner)
        _idle(row)
        conn.execute("DELETE FROM adaptation_episode_versions WHERE project_id=?", (project_id,))
        conn.execute("DELETE FROM adaptations WHERE id=? AND user_id=?", (project_id, owner))
    return Response(status_code=204)


@router.patch("/{project_id}/source")
def update_source(project_id: str, request: Request, body: SourceInput):
    owner = current_user(request)["id"]
    with closing(db()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _get(conn, project_id, owner)
        _idle(row, body.revision)
        state = _state(row)
        if state.get("analysis"):
            _fail("原稿分析后不能再改原文，请新建项目导入修订稿。", 409)
        if state.get("plan") or state["outputs"]:
            _fail("大纲已生成。请在大纲中吸收改动，或新建项目修正原稿。", 409)
        source = next((item for item in state["sources"] if item["id"] == body.source_id), None)
        if not source:
            _fail("找不到该原稿场次", 404)
        if sum(len(item["text"]) for item in state["sources"] if item is not source) + len(body.text) > ad.MAX_SOURCE_CHARS:
            _fail("原稿超过 60,000 字符")
        source.setdefault("originalText", source["text"])
        source["text"] = body.text
        state.pop("preview", None)
        state.pop("error", None)
        _save(conn, row, state)
        return _project(_get(conn, project_id, owner))


def _job_context(state: dict, job: JobInput):
    # Keep raw archival text and UI-only origin metadata out of provider input.
    sources = [ad.SourceScene.model_validate({key: value for key, value in source.items() if key != "originalText"})
               for source in state["sources"]]
    source_data = [{"id": item.id, "label": item.label, "text": item.text} for item in sources]
    context = {"goal": state["goal"], "keep": state["keep"]}
    if job.kind == "analyze":
        if state.get("analysis"):
            raise ad.AdaptationError("原稿已经分析完成，可继续选择方向。")
        return {"title": state.get("projectTitle", ""), "target_episodes": state["episodeCount"],
                "goal": state.get("projectGoal", state.get("goal", "")),
                "keep": state.get("projectKeep", state.get("keep", "")),
                "sources": source_data}, ad.Analysis, sources

    if job.kind == "preview":
        if not state.get("analysis") or not state.get("selectedDirectionId"):
            raise ad.AdaptationError("请先分析原稿并选择一个改编方向。")
        source = next((item for item in source_data if item["id"] == job.source_id), None)
        if not source:
            raise ad.AdaptationError("请先选一场原稿。")
        direction = next((item for item in state["analysis"]["directions"]
                          if item["id"] == state["selectedDirectionId"]), None)
        if not direction:
            raise ad.AdaptationError("所选改编方向已失效，请重新选择。")
        return context | {"analysis": state["analysis"], "direction": direction,
                          "source": source}, ad.Preview, sources

    if job.kind == "plan":
        if not state.get("analysis") or not state.get("preview") or state.get("confirmed"):
            raise ad.AdaptationError("请先生成并审阅一场试改。")
        return context | {"target_episodes": state["episodeCount"], "sources": source_data,
                          "analysis": state["analysis"], "direction": next(
                              item for item in state["analysis"]["directions"]
                              if item["id"] == state["selectedDirectionId"]
                          ), "reviewed_preview": state["preview"],
                          "direction_notes": job.notes}, ad.Plan, sources

    if job.kind == "impact":
        if not state.get("planBeforeEdit"):
            raise ad.AdaptationError("请先保存一处大纲改动。")
        return {"before": state["planBeforeEdit"], "after": state["plan"]}, ad.ImpactReport, sources

    if job.kind == "review_all":
        if not state.get("confirmed"):
            raise ad.AdaptationError("请先确认分集大纲。")
        expected = {str(number) for number in range(1, state["episodeCount"] + 1)}
        if set(state["outputs"]) != expected or any(
            state["outputs"][number].get("stale", True) for number in expected
        ):
            raise ad.AdaptationError("请先生成并复核全部分集，再进行整剧一致性检查。")
        episodes = [{"number": int(number), "draft": state["outputs"][number]["draft"],
                     "continuity": state["outputs"][number]["draft"]["continuity"],
                     "review": state.get("reviews", {}).get(number, {})}
                    for number in sorted(expected, key=int)]
        return {"analysis": state.get("analysis"), "plan": state["plan"],
                "episodes": episodes, "locks": state["locks"]}, ad.ReviewReport, sources

    if not state.get("confirmed"):
        raise ad.AdaptationError("请先确认分集大纲。")
    plan = ad.Plan.model_validate(state["plan"])
    if job.episode > len(plan.episodes):
        raise ad.AdaptationError("该集不在已确认大纲中。")
    current = state["outputs"].get(str(job.episode), {})
    if current.get("locked"):
        raise ad.AdaptationError("本集已锁定，请先解锁。")
    if job.episode > 1:
        if any(state["outputs"].get(str(n), {}).get("stale", True)
               for n in range(1, job.episode)):
            raise ad.AdaptationError("请先生成或复核上一集，再续写本集。")
    refs = set(plan.episodes[job.episode - 1].source_ids)
    history = [{"number": number, "continuity": state["outputs"][str(number)]["draft"]["continuity"]}
               for number in range(1, job.episode)]
    context.update(plan=state["plan"], episode=job.episode, previous=history,
                   source_scenes=[item for item in source_data if item["id"] in refs],
                   locked_lines=[item for item in state["locks"] if item["episode"] == job.episode])
    if current:
        context.update(current_draft=current["draft"], revision_notes=job.notes.strip())
    return context, ad.Draft, sources


def _usage_summary(usage: dict, calls: list[dict]) -> dict:
    input_tokens = [item.get("inputTokens") for item in calls
                    if isinstance(item.get("inputTokens"), (int, float))]
    output_tokens = [item.get("outputTokens") for item in calls
                     if isinstance(item.get("outputTokens"), (int, float))]
    costs = [item.get("apiCostRmb") for item in calls
             if isinstance(item.get("apiCostRmb"), (int, float))]
    sources = {item.get("costSource") for item in calls if item.get("apiCostRmb") is not None}
    cost_source = "unpriced" if not costs else "estimated" if any(
        "estimate" in str(value) for value in sources
    ) else "provider" if len(sources) == 1 else "mixed"
    summary = dict(usage)
    summary.update(
        inputTokens=sum(input_tokens) if input_tokens else usage.get("inputTokens"),
        outputTokens=sum(output_tokens) if output_tokens else usage.get("outputTokens"),
        apiCostRmb=round(sum(costs), 6) if costs else None,
        costSource=cost_source,
        calls=calls,
    )
    return summary


def _run_job(project_id, owner, job_id, job, context, contract, sources, model):
    calls: list[dict] = []
    recorder = ad.usage_recorder.set(lambda kind, usage: calls.append({"kind": kind, **usage}))
    try:
        if job.kind == "preview":
            result, usage = ad.generate_preview(context, model)
        else:
            result, usage = ad.generate(job.kind, context, contract, model)
        review = None
        if job.kind == "analyze":
            ad.validate_analysis(result, sources)
        elif job.kind == "episode":
            with closing(db()) as conn:
                state_for_review = _state(_get(conn, project_id, owner))
            plan_for_review = ad.Plan.model_validate(state_for_review["plan"])
            locks_for_review = [ad.LockLine.model_validate(item) for item in state_for_review["locks"]]
            try:
                ad.validate_draft(result, plan_for_review, locks_for_review, job.episode)
            except ad.AdaptationError as problem:
                result, _ = ad.generate("episode_repair", context | {
                    "draft_to_repair": result.model_dump(), "validation_problem": str(problem),
                }, ad.Draft, model)
                ad.validate_draft(result, plan_for_review, locks_for_review, job.episode)
            review_context = {
                "analysis": state_for_review.get("analysis"), "plan": state_for_review["plan"],
                "episode": job.episode, "draft": result.model_dump(),
                "source_scenes": context["source_scenes"],
                "locked_lines": context["locked_lines"],
                "keep": context["keep"], "revision_notes": context.get("revision_notes", ""),
            }
            review, _ = ad.generate("review", review_context, ad.ReviewReport, model)

        with closing(db()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = _get(conn, project_id, owner)
            if row["job_id"] != job_id:
                return
            state = _state(row)
            if job.kind == "analyze":
                state["analysis"] = result.model_dump()
                state["setupPaid"] = True
                if state.get("titleFromSource"):
                    title = result.title.strip()
                    conn.execute("UPDATE adaptations SET title=? WHERE id=?", (title, project_id))
                    state["projectTitle"] = title
                    state["titleFromSource"] = False
            elif job.kind == "preview":
                state["preview"] = result.model_dump()
                state["previewUsed"] = True
                state.pop("plan", None)
            elif job.kind == "plan":
                ad.validate_plan(result, sources, state["episodeCount"])
                if any(not any(character.id == previous["id"] and character.original_name == previous["original_name"]
                                   and character.name == previous["name"] for character in result.cast)
                       for previous in state["preview"]["cast"]):
                    raise ad.AdaptationError("大纲没有沿用试改的人物编号和姓名，请重试。")
                state["plan"] = result.model_dump()
                state["planUsed"] = True
            elif job.kind == "episode":
                if any(issue.episode != job.episode for issue in review.issues):
                    raise ad.AdaptationError("本集审稿返回了其他集数的提示，请重试。")
                plan = ad.Plan.model_validate(state["plan"])
                locks = [ad.LockLine.model_validate(item) for item in state["locks"]]
                ad.validate_draft(result, plan, locks, job.episode)
                episode_key = str(job.episode)
                previous = state["outputs"].get(episode_key)
                version = 1
                if previous:
                    previous_version = previous.get("version", 1)
                    previous_draft = ad.Draft.model_validate(previous["draft"])
                    conn.execute(
                        """INSERT INTO adaptation_episode_versions(project_id, episode, version, screenplay, notes)
                           VALUES (?, ?, ?, ?, ?)""",
                        (project_id, job.episode, previous_version,
                         ad.render_scenes(previous_draft.scenes, plan.cast, job.episode),
                         previous.get("revisionNotes", "")),
                    )
                    version = previous_version + 1
                state["outputs"][episode_key] = {
                    "draft": result.model_dump(), "locked": False, "stale": False,
                    "version": version, "revisionNotes": context.get("revision_notes", ""),
                }
                state.pop("reviewAll", None)
                state.setdefault("reviews", {})[str(job.episode)] = review.model_dump()
                for key, output in state["outputs"].items():
                    if int(key) > job.episode:
                        output["stale"] = True
                        state["reviews"].pop(key, None)
            elif job.kind == "impact":
                numbers = [item.number for item in result.affected]
                if len(set(numbers)) != len(numbers) or any(n < 1 or n > state["episodeCount"] for n in numbers):
                    raise ad.AdaptationError("影响清单返回了无效集数，请重试。")
                state["impact"] = result.model_dump()["affected"]
                state["impactChecked"] = True
            elif job.kind == "review_all":
                if any(issue.episode > state["episodeCount"] for issue in result.issues):
                    raise ad.AdaptationError("整剧审稿返回了不存在的集数，请重试。")
                state["reviewAll"] = result.model_dump()
                state["reviewAllUsed"] = True

            usage = _usage_summary(usage, calls)
            usage_key = str(job.episode) if job.kind == "episode" else job.kind
            state["usage"][usage_key] = usage
            state.pop("error", None)
            state.pop("errorKind", None)
            _save(conn, row, state)
            conn.execute(
                """UPDATE adaptation_jobs SET status='success', credits_charged=credits_reserved,
                   provider=?, model=?, prompt_version=?, input_tokens=?, output_tokens=?,
                   api_cost_rmb=?, cost_source=?, calls_json=?, finished_at=? WHERE id=?""",
                (calls[0].get("provider", "") if calls else "",
                 calls[0].get("model", "") if calls else "",
                 ad.PROMPT_VERSION, usage.get("inputTokens"), usage.get("outputTokens"),
                 usage.get("apiCostRmb"), usage.get("costSource", "unpriced"),
                 _dump(calls), now_iso(), job_id),
            )
    except Exception as exc:
        # Provider messages can echo source text. Persist only vetted product errors.
        error = str(exc) if isinstance(exc, ad.AdaptationError) else "本次改编生成失败，请重试。"
        if not isinstance(exc, ad.AdaptationError):
            logger.error("adaptation_failed project=%s kind=%s error_type=%s", project_id, job.kind, type(exc).__name__)
        with closing(db()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM adaptations WHERE id=? AND user_id=? AND job_id=?",
                               (project_id, owner, job_id)).fetchone()
            job_row = conn.execute("SELECT * FROM adaptation_jobs WHERE id=?", (job_id,)).fetchone()
            if row and job_row and job_row["status"] == "running":
                state = _state(row)
                refund = max(0, int(job_row["credits_reserved"] or 0) - int(job_row["credits_refunded"] or 0))
                if refund:
                    _adjust_credits(conn, owner, refund, "refund",
                                    f"劇本改編失敗退回 · {job_row['project_title']} · {_job_label(job.kind)}")
                state.update(error=error, errorKind=job.kind)
                if job.kind == "analyze":
                    state["setupPaid"] = False
                _save(conn, row, state)
                usage = _usage_summary({}, calls)
                conn.execute(
                    """UPDATE adaptation_jobs SET status='failed', credits_refunded=?, provider=?, model=?,
                       prompt_version=?, input_tokens=?, output_tokens=?, api_cost_rmb=?, cost_source=?,
                       calls_json=?, error=?, finished_at=? WHERE id=?""",
                    (int(job_row["credits_refunded"] or 0) + refund,
                     calls[0].get("provider", "") if calls else "",
                     calls[0].get("model", "") if calls else "",
                     ad.PROMPT_VERSION, usage.get("inputTokens"), usage.get("outputTokens"),
                     usage.get("apiCostRmb"), usage.get("costSource", "unpriced"),
                     _dump(calls), error[:400], now_iso(), job_id),
                )
    finally:
        ad.usage_recorder.reset(recorder)


@router.post("/{project_id}/jobs", status_code=202)
def start_job(project_id: str, request: Request, body: JobInput, background: BackgroundTasks):
    owner = current_user(request)["id"]
    with closing(db()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        _recover_expired_jobs(conn, time.time())
        row = _get(conn, project_id, owner)
        _idle(row, body.revision)
        try:
            state = _state(row)
            if body.kind == "preview":
                if state.get("confirmed"):
                    raise ad.AdaptationError("大纲已确认。请在新项目中重新试改。")
                if not state.get("analysis"):
                    raise ad.AdaptationError("请先分析原稿并选择一个改编方向。")
                analysis = ad.Analysis.model_validate(state.get("analysis"))
                direction = next((item for item in analysis.directions if item.id == body.direction_id), None)
                if not direction:
                    raise ad.AdaptationError("请选择一个改编方向。")
                if len(set(body.keep_option_ids)) != len(body.keep_option_ids):
                    raise ad.AdaptationError("保留项重复，请重新选择。")
                options = {item.id: item for item in analysis.keep_options}
                if any(option_id not in options for option_id in body.keep_option_ids):
                    raise ad.AdaptationError("保留项已更新，请刷新后重新选择。")
                state["selectedDirectionId"] = direction.id
                state["selectedKeepOptionIds"] = body.keep_option_ids
                project_goal = state.setdefault("projectGoal", state.get("goal", "")).strip()
                project_keep = state.setdefault("projectKeep", state.get("keep", "")).strip()
                goal = "\n".join(part for part in [direction.goal, project_goal, body.notes.strip()] if part)
                keep = "\n".join(
                    [options[option_id].instruction for option_id in body.keep_option_ids]
                    + [project_keep, body.keep_notes.strip()]
                )
                if len(goal) > 1000 or len(keep) > 1200:
                    raise ad.AdaptationError("补充要求太长，请精简后再试。")
                state["goal"], state["keep"] = goal, keep
                if not body.source_id:
                    body = body.model_copy(update={"source_id": analysis.preview_source_id})
            context, contract, sources = _job_context(state, body)
            model = ad.model_choice()
        except ad.AdaptationError as exc:
            _fail(str(exc))
        now = time.time()
        if conn.execute("SELECT COUNT(*) FROM adaptations WHERE job_started>?", (now - LEASE_SECONDS,)).fetchone()[0] >= 2:
            _fail("生成通道繁忙，请稍后提交。", 429)
        if conn.execute("SELECT 1 FROM adaptations WHERE user_id=? AND job_started>?",
                        (owner, now - LEASE_SECONDS)).fetchone():
            _fail("你已有一项改编在生成，请稍后提交。", 409)
        conn.execute("DELETE FROM adaptation_attempts WHERE started<?", (now - 86400,))
        if conn.execute("SELECT COUNT(*) FROM adaptation_attempts").fetchone()[0] >= 150:
            _fail("测试环境已达当天的生成上限，请明天再试。", 429)
        if conn.execute("SELECT COUNT(*) FROM adaptation_attempts WHERE user_id=?", (owner,)).fetchone()[0] >= 40:
            _fail("已达测试环境每日生成上限，请明天再试。", 429)
        job_id = uuid.uuid4().hex
        points, billing_kind = _reserved_points(state, body)
        if points:
            charge_label = {
                "project": "原稿分析（含第1集）",
                "preview_retry": "试改重做",
                "plan_retry": "分集大纲重生成",
                "episode": f"第{body.episode}集生成",
                "rewrite": f"第{body.episode}集重写",
                "review": "整剧一致性复核",
                "impact": "大纲影响分析",
            }.get(billing_kind, _job_label(body.kind))
            _adjust_credits(conn, owner, -points, "consume",
                            f"剧本改编 · {row['title']} · {charge_label}")
        state.pop("error", None)
        state.pop("errorKind", None)
        conn.execute("INSERT INTO adaptation_attempts VALUES(?,?)", (owner, now))
        conn.execute(
            """INSERT INTO adaptation_jobs
               (id, project_id, project_title, user_id, kind, billing_kind, episode,
                status, credits_reserved, started_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?)""",
            (job_id, project_id, row["title"], owner, body.kind, billing_kind,
             body.episode if body.kind == "episode" else 0, points, now_iso()),
        )
        conn.execute("UPDATE adaptations SET job_id=?,job_kind=?,job_started=?,state_json=?,updated_at=? WHERE id=?",
                     (job_id, body.kind, now, _dump(state), now_iso(), project_id))
        background.add_task(_run_job, project_id, owner, job_id, body, context, contract, sources, model)
        return _project(_get(conn, project_id, owner))


@router.put("/{project_id}/plan")
def save_plan(project_id: str, request: Request, body: PlanInput):
    owner = current_user(request)["id"]
    with closing(db()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _get(conn, project_id, owner)
        _idle(row, body.revision)
        state = _state(row)
        if not state.get("plan"):
            _fail("请先生成一份大纲")
        sources = [ad.SourceScene.model_validate({key: value for key, value in item.items() if key != "originalText"})
                   for item in state["sources"]]
        try:
            ad.validate_plan(body.plan, sources, state["episodeCount"])
            ad.validate_locks(body.locks, body.plan, sources)
        except ad.AdaptationError as exc:
            _fail(str(exc))
        plan = body.plan.model_dump()
        locks = [lock.model_dump() for lock in body.locks]
        changed = plan != state["plan"] or locks != state["locks"]
        if changed and state["outputs"]:
            keep_ids = {block["speaker_id"] for output in state["outputs"].values()
                        for scene in output["draft"]["scenes"] for block in scene["blocks"] if block["speaker_id"]}
            if not keep_ids <= {item.id for item in body.plan.cast}:
                _fail("已生成正文使用了被移除的人物，请把人物编号保留在人物表中。")
            old_names = {c["id"]: c["name"] for c in state["plan"]["cast"]}
            new_names = {c["id"]: c["name"] for c in plan["cast"]}
            if any(output["locked"] and any(
                old_names.get(block["speaker_id"]) != new_names.get(block["speaker_id"])
                for scene in output["draft"]["scenes"] for block in scene["blocks"]
            ) for output in state["outputs"].values()):
                _fail("人物表调整会改变已锁定正文，请先解锁相关正文。", 409)
            old_plan = state["plan"]
            affected = 1 if any(old_plan[key] != plan[key] for key in ("premise", "cast")) else next(
                (old["number"] for old, new in zip(old_plan["episodes"], plan["episodes"]) if old != new),
                len(plan["episodes"]) + 1,
            )
            if locks != state["locks"]:
                affected = min(affected, *(item["episode"] for item in locks + state["locks"]))
            for number, output in state["outputs"].items():
                if int(number) >= affected:
                    output["stale"] = True
                    state.get("reviews", {}).pop(number, None)
            state.pop("reviewAll", None)
        if plan != state["plan"]:
            state["planBeforeEdit"] = state["plan"]
            state.pop("impact", None)
        state.update(plan=plan, locks=locks, confirmed=True)
        state.pop("error", None)
        _save(conn, row, state)
        return _project(_get(conn, project_id, owner))


@router.put("/{project_id}/episodes/{episode}")
def save_episode(project_id: str, episode: int, request: Request, body: DraftInput):
    owner = current_user(request)["id"]
    with closing(db()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _get(conn, project_id, owner)
        _idle(row, body.revision)
        state = _state(row)
        output = state["outputs"].get(str(episode))
        if not output:
            _fail("本集还没有正文", 404)
        if output["locked"]:
            _fail("本集已锁定，请先解锁", 409)
        if any(state["outputs"].get(str(n), {}).get("stale", True) for n in range(1, episode)):
            _fail("请先核对前面的集数", 409)
        try:
            plan = ad.Plan.model_validate(state["plan"])
            ad.validate_draft(body.draft, plan, [ad.LockLine.model_validate(item) for item in state["locks"]], episode)
        except ad.AdaptationError as exc:
            _fail(str(exc))
        draft = body.draft.model_dump()
        if draft != output["draft"] or output["stale"]:
            output.update(draft=draft, stale=False)
            state.get("reviews", {}).pop(str(episode), None)
            state.pop("reviewAll", None)
            for number, other in state["outputs"].items():
                if int(number) > episode:
                    other["stale"] = True
        state.pop("error", None)
        _save(conn, row, state)
        return _project(_get(conn, project_id, owner))


@router.get("/{project_id}/episodes/{episode}/versions/{version}")
def get_episode_version(project_id: str, episode: int, version: int, request: Request):
    owner = current_user(request)["id"]
    with closing(db()) as conn:
        _get(conn, project_id, owner)
        row = conn.execute(
            """SELECT screenplay, notes FROM adaptation_episode_versions
               WHERE project_id=? AND episode=? AND version=?""",
            (project_id, episode, version),
        ).fetchone()
        if not row:
            _fail("找不到这个正文版本", 404)
        return dict(row)


@router.put("/{project_id}/episodes/{episode}/lock")
def lock_episode(project_id: str, episode: int, request: Request, body: OutputLock):
    owner = current_user(request)["id"]
    with closing(db()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = _get(conn, project_id, owner)
        _idle(row, body.revision)
        state = _state(row)
        output = state["outputs"].get(str(episode))
        if not output:
            _fail("本集还没有正文", 404)
        if body.locked and output["stale"]:
            _fail("本集依据已变更，请复核并重新生成后再锁定。", 409)
        output["locked"] = body.locked
        _save(conn, row, state)
        return _project(_get(conn, project_id, owner))


@router.get("/{project_id}/download")
def download(project_id: str, request: Request):
    owner = current_user(request)["id"]
    with closing(db()) as conn, conn:
        row = _get(conn, project_id, owner)
        state = _state(row)
    if not state["outputs"]:
        _fail("请先生成分集正文", 409)
    if any(item["stale"] for item in state["outputs"].values()):
        _fail("有分集正文需要按最新大纲复核，请更新标出的集数再导出。", 409)
    plan = ad.Plan.model_validate(state["plan"])
    parts = [f"# {plan.title}"]
    for key in sorted(state["outputs"], key=int):
        draft = ad.Draft.model_validate(state["outputs"][key]["draft"])
        parts.append(f"## 第 {key} 集\n\n" + ad.render_scenes(draft.scenes, plan.cast, int(key)))
    name = quote(f"{plan.title}-改编稿.md", safe="")
    return Response("\n\n".join(parts) + "\n", media_type="text/markdown",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{name}", "Cache-Control": "no-store"})
