"""Read persisted staging evidence and logs; never invoke providers or update tasks."""

import collections
import datetime as dt
import hashlib
import json
import re
import sqlite3
import statistics
import sys

from app.config import DATA_DIR, DB_PATH, LOG_DIR

BATCH = "batch-c44f38be351841b084fa9462896fb991"


def decoded(value):
    return json.loads(value or "{}")


def stats(values):
    values = sorted(float(v) for v in values if v is not None)
    if not values:
        return None
    return dict(n=len(values), total=round(sum(values), 2),
                median=round(statistics.median(values), 2),
                p95=round(values[min(len(values)-1, int(len(values)*.95))], 2),
                maximum=round(values[-1], 2))


def emit(label, value):
    print(label, json.dumps(value, ensure_ascii=False, separators=(",", ":")))


c = sqlite3.connect("file:" + str(DB_PATH) + "?mode=ro", uri=True)
c.row_factory = sqlite3.Row
c.execute("PRAGMA query_only=ON")
rows = c.execute("SELECT * FROM tasks WHERE batch_id=? ORDER BY batch_index", (BATCH,)).fetchall()
tasks = {r["id"]: r for r in rows}
qualities = {r["batch_index"]: decoded(r["quality_json"]) for r in rows}
evidence = {r["batch_index"]: decoded(r["evidence_json"]) for r in rows}
scripts = {r["batch_index"]: decoded(r["result_json"]) for r in rows}
events = c.execute("SELECT * FROM task_events WHERE task_id IN (SELECT id FROM tasks WHERE batch_id=?) ORDER BY id", (BATCH,)).fetchall()
section = sys.argv[1] if len(sys.argv) > 1 else "quality"
print(chr(27) + "[2J" + chr(27) + "[H", end="")

if section == "quality":
    from app.content_coverage import content_gaps
    from app.script import has_screenplay_body
    emit("STATUS", collections.Counter(r["status"] for r in rows))
    emit("REVIEW_EP", [r["batch_index"] for r in rows if r["status"] == "review"])
    emit("EMPTY", [n for n, s in scripts.items() if not has_screenplay_body(s)])
    emit("GAPS20", {r["batch_index"]: content_gaps(scripts[r["batch_index"]], r["duration_sec"]) for r in rows if content_gaps(scripts[r["batch_index"]], r["duration_sec"])})
    emit("GAPS8", {r["batch_index"]: content_gaps(scripts[r["batch_index"]], r["duration_sec"], minimum=8) for r in rows if content_gaps(scripts[r["batch_index"]], r["duration_sec"], minimum=8)})
    emit("COVERAGE", {k: stats(q.get(k) for q in qualities.values()) for k in ["transcriptCoverage", "dialogueEvidencePrecision", "ocrCoverage", "dialogueCount", "actionCount", "tailGapSec"]})
    emit("P0_TAGS", collections.Counter(i["tag"] for q in qualities.values() for i in q.get("blockingIssues", [])))
    emit("DONE_WITH_P0_COUNTS", {r["batch_index"]: qualities[r["batch_index"]].get("severityCounts") for r in rows if r["status"] == "done" and qualities[r["batch_index"]].get("severityCounts", {}).get("P0")})
    emit("LOW_COVERAGE", {n: [q.get("transcriptCoverage"), q.get("dialogueEvidencePrecision")] for n,q in qualities.items() if min(q.get("transcriptCoverage") or 100, q.get("dialogueEvidencePrecision") or 100) < 72})
    emit("P0_EP", {n:q.get("severityCounts",{}).get("P0",0) for n,q in qualities.items() if q.get("severityCounts",{}).get("P0",0)})

elif section == "evidence":
    emit("PROVIDERS", collections.Counter((s.get("kind"),s.get("provider"),s.get("status")) for e in evidence.values() for s in e.get("sources",[])).most_common())
    emit("INCOMPLETE", [(n,s.get("kind"),s.get("sampling")) for n,e in evidence.items() for s in e.get("sources",[]) if s.get("sampling",{}).get("complete") is False])
    emit("TIMINGS_SECONDS", {k:stats((e.get("timingsMs",{}).get(k) or 0)/1000 for e in evidence.values()) for k in ["total","audio","ocr","audioQueue","ocrQueue"]})
    ocr = [i for e in evidence.values() for s in e.get("sources",[]) if s.get("kind")=="ocr" for i in s.get("items",[])]
    emit("OCR_ITEMS", dict(total=len(ocr), cjk=sum(bool(re.search("[\u4e00-\u9fff]",i.get("text",""))) for i in ocr), single=sum(len(i.get("text",""))==1 for i in ocr)))
    emit("OCR_TOP",collections.Counter(i.get("text","") for i in ocr).most_common(15))
    emit("OCR1_SAMPLES",[(i.get("startSec"),i.get("region"),i.get("text")) for s in evidence[1].get("sources",[]) if s.get("kind")=="ocr" for i in s.get("items",[])][::20][:11])
    emit("REVIEW",collections.Counter((q.get("automatedReview",{}).get("attemptCount"),q.get("automatedReview",{}).get("accepted"),q.get("automatedReview",{}).get("retainedDraftReason")) for q in qualities.values()).most_common())

elif section == "events":
    emit("RANGE", dict(created=min(r["created_at"] for r in rows), completed=max(r["completed_at"] for r in rows), uploadedFirst=min(e["created_at"] for e in events if e["event_type"]=="uploaded"), uploadedLast=max(e["created_at"] for e in events if e["event_type"]=="uploaded")))
    emit("TASK_SECONDS",stats(e["duration_ms"]/1000 for e in events if e["event_type"] in {"completed","review_required"}))
    for n in [31,43,55,68,90]:
        emit("TIMELINE",[n,[(e["event_type"],e["stage"],e["created_at"][11:19],e["message"][:130]) for e in events if tasks[e["task_id"]]["batch_index"]==n and (e["event_type"] in {"uploaded","failed","retry","completed","review_required"} or e["stage"]=="probing")]])

elif section in {"logs", "errors", "slow"}:
    names = {r["stored_path"].rsplit("/",1)[-1]:r for r in rows}
    ids = set(tasks)
    logs = []
    all_window = []
    for path in sorted(LOG_DIR.glob("app.log*")):
        for line in path.read_text(encoding="utf-8",errors="replace").splitlines():
            if "2026-10-17 08:00:" <= line[:20] < "2026-10-17 08:20:":
                all_window.append(line)
                if any(x in line for x in ids) or any(x in line for x in names):
                    logs.append(line)
    logs.sort()
    window_starts = [re.search(r"task_start task_id=(\S+)", l).group(1)
                     for l in all_window if "task_start task_id=" in l]
    # Downloaded videos all log as source.mp4. Only attribute that window when
    # every task start is proven to belong to this batch.
    foreign = sorted(set(window_starts) - ids)
    emit("FOREIGN_TASKS", foreign)
    if window_starts and not foreign:
        logs = sorted(all_window)
    emit("LOG_RANGE", [len(logs), logs[0][:23] if logs else None, logs[-1][:23] if logs else None])
    if section == "errors":
        for line in logs:
            if any(x in line for x in ["task_retry_auto", "task_failed", "provider_failed", "response_create_retry", "review_budget_retry", "cpu_fallback", "content_repair_failed"]):
                emit("ERROR",line[:650])
    elif section == "slow":
        for line in sorted([l for l in logs if "operation=response " in l],key=lambda l:float(re.search(r"duration_ms=([\d.]+)",l).group(1)),reverse=True)[:8]:emit("SLOW",line)
        emit("LAST_DONE", [l for l in logs if "task_done " in l][-3:])
    else:
        operations = collections.defaultdict(list)
        for line in logs:
            m=re.search(r"(?:operation|kind)=(\w+).*?duration_ms=([\d.]+)",line)
            if m: operations[m.group(1)].append(float(m.group(2))/1000)
        emit("OP_SECONDS",{k:stats(v) for k,v in operations.items()})
        emit("COUNTS",{k:sum(k in line for line in logs) for k in ["provider_review provider", "provider_review_retained_draft", "provider_review_failed", "response_create_retry", "review_budget_retry", "evidence_asr_remote", "evidence_ocr_remote", "evidence_asr_cpu_fallback", "evidence_ocr_cpu_fallback", "kind=all operation=cache_hit", "batch_character_continuity"]})
        admissions=[float(re.search(r"queue_ms=([\d.]+)",l).group(1))/1000 for l in all_window if "operation=generation_admission" in l]
        emit("MODEL_QUEUE",stats(admissions))
        emit("QUOTA_WAIT",stats(float(re.search(r"quota_wait_ms=([\d.]+)",l).group(1))/1000 for l in all_window if "operation=generation_admission" in l))
        emit("LOG_WINDOW",[min(l[:23] for l in all_window),max(l[:23] for l in all_window),len(all_window)])

elif section == "detail":
    n=int(sys.argv[2]); q=qualities[n];s=scripts[n]; e=evidence[n]
    emit("DETAIL",[n,q.get("transcriptCoverage"),q.get("dialogueEvidencePrecision"),q.get("severityCounts")])
    emit("P0",q.get("blockingIssues",[])[:8])
    emit("SPEAKERS",collections.Counter(b.get("speaker") for sc in s.get("scenes",[]) for b in sc.get("blocks",[]) if b.get("type") in {"dialogue","vo"}))
    emit("SPOKEN",[(b.get("startSec"),b.get("endSec"),b.get("speaker"),b.get("text")) for sc in s.get("scenes",[]) for b in sc.get("blocks",[]) if b.get("type") in {"dialogue","vo"}][:15])
    emit("ASR",[(i.get("startSec"),i.get("endSec"),i.get("text")) for so in e.get("sources",[]) if so.get("kind")=="audio" for i in so.get("items",[])][:16])

elif section == "normalize":
    from app.script import normalize_script,script_quality,quality_gate
    changed=[];mismatch=[];hidden=[]
    for r in rows:
        n=r["batch_index"];s=scripts[n];ns=normalize_script(s,r["title"])
        if ns!=s:changed.append(n)
        q=script_quality(ns,r["provider"],{**evidence[n],"durationSec":r["duration_sec"]})
        approved,blocking=quality_gate(q)
        if (r["status"]=="done") != approved:
            mismatch.append([n,r["status"],q.get("severityCounts"),collections.Counter(i["tag"] for i in blocking)])
        if q.get("severityCounts",{}).get("P0") and approved:hidden.append([n,q["severityCounts"]])
    emit("NORMALIZE_CHANGED",changed)
    emit("QA_STATUS_MISMATCH",mismatch)
    emit("HIDDEN_P0",hidden)

elif section == "cache":
    n=int(sys.argv[2]);r=next(r for r in rows if r["batch_index"]==n)
    directory=DATA_DIR/"recognition-cache"/hashlib.sha256(r["id"].encode()).hexdigest()
    emit("CACHE_FILES",[(p.name,p.stat().st_size) for p in directory.glob("*.json")])
    for p in directory.glob("model*.json"):
        d=decoded(p.read_text(encoding="utf-8")).get("data",{})
        emit("CACHE_MODEL",[p.name,d.keys() if False else list(d)])
        for key in ["rawScript","normalizedScript"]:
            s=d.get(key) or {};bl=[b for sc in s.get("scenes",[]) for b in sc.get("blocks",[])]
            emit("DRAFT",[p.name,key,len(bl),collections.Counter(b.get("type") for b in bl)])

c.close()
