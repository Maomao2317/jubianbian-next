"""Run a small real-model staging smoke using only original synthetic text."""

import json
from contextlib import closing
from unittest.mock import patch

from fastapi import BackgroundTasks

from app import adaptation as ad
from app import adaptation_routes as routes
from app import config
from app.auth_store import db, init_db

SOURCE = """第1集
1-1 夜 内 账房
△ 沈青在关门前翻开账册，发现货款少了十两。
沈青：账册不能离开柜台。
迟巧：掌柜刚才来过。他拿走的是另一本。
△ 门外的铜铃响了。沈青看见掌柜的影子停在门外。

第2集
2-1 昼 外 商号后街
△ 沈青和迟巧约在后门，迟巧拿来一枚系红线的印章。
沈青：这是谁的印？
迟巧：印是掌柜的，红线是我昨夜系的。
△ 沈青把印章上的缺口和账册朱印上的白点对在一起。

第3集
3-1 昼 内 商号大堂
△ 沈青请掌柜在新纸上盖章。
沈青：这缺一角的印，盖过那份假账。
掌柜：我早就把这枚印丢了。
迟巧：那你为什么刚才还在找红线？
△ 掌柜合上印匣。商号东家取出真正的进货单。"""
LINE = "账册不能离开柜台。"


def main():
    if config.JBB_ENVIRONMENT != "staging" or str(config.DATA_DIR) != "/tmp/jbb-adaptation-smoke":
        raise SystemExit("Smoke requires the disposable staging test database.")
    init_db()
    routes.init_adaptations()
    with closing(db()) as connection, connection:
        connection.execute(
            """INSERT INTO users(id,email,password_hash,name,credits,created_at)
               VALUES ('isolated-smoke','adaptation-smoke@example.test','test','改编冒烟测试',9999,'now')"""
        )
    def run(project, kind, **fields):
        background = BackgroundTasks()
        routes.start_job(project["id"], None, routes.JobInput(
            kind=kind, revision=project["revision"], **fields
        ), background)
        task = background.tasks[0]
        task.func(*task.args, **task.kwargs)
        project = routes.get_project(project["id"], None)
        if project.get("error"):
            raise SystemExit(f"{kind}: {project['error']}")
        return project

    provider, model = ad.model_choice()
    with patch.object(routes, "current_user", return_value={"id": "isolated-smoke", "role": "admin"}):
        project = routes.create_project(None, routes.ProjectInput(
            title="账房对印", goal="改成古装漫剧，把关键冲突设计成画得出来的动作。",
            keep="保留沈青、迟巧的姓名和身份，账册、缺角印章、红线及翻案的因果。第一集原样保留沈青的台词“账册不能离开柜台。”。",
            episode_count=3, source_text=SOURCE,
        ))
        project = run(project, "analyze")
        source_texts = {item["id"]: item["text"] for item in project["sources"]}
        if any(item["quote"] not in source_texts[item["source_id"]] for item in project["analysis"]["keep_options"]):
            raise SystemExit("preflight: an original quote could not be verified")
        if "usage" in project or set(routes.availability(None)) != {"enabled", "ready"}:
            raise SystemExit("preflight: provider or cost details are exposed in the project")
        source = next(item for item in project["sources"] if LINE in item["text"])
        direction = project["analysis"]["directions"][0]
        project = run(project, "preview", source_id=source["id"], direction_id=direction["id"],
                      keep_option_ids=[item["id"] for item in project["analysis"]["keep_options"]])
        project = run(project, "plan", notes="第一集保留沈青及其锁定台词。")
        plan = project["plan"]
        character = next((item for item in plan["cast"] if item["name"] == "沈青"), None)
        if not character:
            raise SystemExit("preflight: the model did not preserve the requested lead character name")
        project = routes.save_plan(project["id"], None, routes.PlanInput(
            revision=project["revision"], plan=plan,
            locks=[ad.LockLine(episode=1, speaker_id=character["id"], source_id=source["id"], text=LINE)]))
        for number in range(1, 4):
            project = run(project, "episode", episode=number)
        notes = "缩短最后一场的问答，保留当众辨认印章的动作。"
        project = run(project, "episode", episode=3, notes=notes)
        archive = routes.get_episode_version(project["id"], 3, 1, None)
        if project["outputs"]["3"]["revisionNotes"] != notes or not archive["screenplay"]:
            raise SystemExit("preflight: the requested revision or its previous version was not saved")
        project = run(project, "review_all")
        if not project.get("reviewAll"):
            raise SystemExit("preflight: the whole-script review is missing")
        if any(str(number) not in project["reviews"] for number in range(1, 4)):
            raise SystemExit("preflight: at least one generated episode is missing its review")
        exported = routes.download(project["id"], None).body.decode()
        if not all(f"## 第 {number} 集" in exported for number in range(1, 4)) or "沈青：" + LINE not in exported:
            raise SystemExit("preflight: generated export did not pass the episode and exact dialogue checks")
        admin = routes.admin_adaptations(None, kind="all", limit=20, offset=0)
        charged = sum(item["credits_charged"] for item in admin["items"])
        if charged != 48 or not any(item["billing_kind"] == "included_revision" and item["status"] == "success"
                                    for item in admin["items"]):
            raise SystemExit("preflight: adaptation charges or the included revision were incorrect")
    calls = [call for item in admin["items"] for call in item["calls"]]
    costs = [call["apiCostRmb"] for call in calls if call["apiCostRmb"] is not None]
    print(json.dumps({"verified": True, "provider": provider, "model": model,
                      "episodes": 3, "promptVersion": ad.PROMPT_VERSION,
                      "jobs": admin["total"], "calls": len(calls), "creditsCharged": charged,
                      "pricedCalls": len(costs), "apiCostRmb": round(sum(costs), 6) if costs else None,
                      "inputTokens": sum(item["input_tokens"] or 0 for item in admin["items"]),
                      "outputTokens": sum(item["output_tokens"] or 0 for item in admin["items"])}))


if __name__ == "__main__":
    main()
