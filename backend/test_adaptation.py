import gc
import io
import json
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import BackgroundTasks, HTTPException

from app import adaptation as ad
from app import adaptation_routes as routes
from app import auth_store


SOURCE = """第1集
1-1 夜 内 老商号
沈青：账册不能离开柜台。
沈青按住封条。伙计推门而入。

第2集
2-1 昼 外 街口
沈青拿着账册寻找伙计。伙计远远避开。"""
LINE = "账册不能离开柜台。"
CAST = [{"id": "C1", "original_name": "沈青", "name": "沈青", "role": "在商号管账"}]
SCENE = {"setting": "老商号 夜 内", "blocks": [
    {"speaker_id": "", "text": "沈青按住账册。"}, {"speaker_id": "C1", "text": LINE}]}
PLAN = {"title": "商号旧案", "premise": "查账翻案", "cast": CAST, "episodes": [
    {"number": 1, "beats": "封条被撕", "hook": "账册被藏起", "source_ids": ["S2"]},
    {"number": 2, "beats": "查到伙计", "hook": "查出假账", "source_ids": ["S4"]}]}
DRAFT = {"scenes": [SCENE], "continuity": "下一集寻找伙计"}
LOCK = {"episode": 1, "speaker_id": "C1", "source_id": "S2", "text": LINE}
ANALYSIS = {
    "title": "商号旧案", "summary": "掌柜与账房围绕失踪账册相互试探。",
    "characters": ["沈青：商号管账，掌握封条"],
    "directions": [
        {"id": "D1", "title": "悬疑翻案", "description": "围绕账册线索推进查证。",
         "reason": "原稿已出现账册与封条。", "goal": "改成古装漫剧，突出查证翻案。"},
        {"id": "D2", "title": "人物博弈", "description": "加强沈青与掌柜之间的试探。",
         "reason": "原稿人物已发生冲突。", "goal": "强化沈青与掌柜的对峙。"},
    ],
    "keep_options": [{"id": "K1", "kind": "plot", "label": "账册翻案主线",
                      "instruction": "保留账册查证翻案。", "source_id": "S2", "quote": LINE}],
    "questions": [], "preview_source_id": "S2",
}
REVIEW = {"summary": "与大纲相符。", "issues": []}


class AdaptationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(gc.collect)
        db_patch = patch.object(auth_store, "DB_PATH", Path(self.tmp.name) / "project.sqlite3")
        db_patch.start()
        self.addCleanup(db_patch.stop)
        auth = patch.object(routes, "current_user", return_value={"id": "owner", "role": "user"})
        self.current_user = auth.start()
        self.addCleanup(auth.stop)
        auth_store.init_db()
        with closing(auth_store.db()) as conn, conn:
            conn.execute("""INSERT INTO users(id,email,password_hash,name,credits,created_at)
                          VALUES ('owner','owner@example.test','test','测试用户',100,'now')""")
        routes.init_adaptations()

    def create(self, **source):
        return routes.create_project(None, routes.ProjectInput(
            goal="改成适合分镜绘制的古装漫剧", keep="保留核心因果线索", episode_count=2, **source))

    def generate(self, project, kind, raw, *, repaired=None, **kwargs):
        if kind == "preview":
            kwargs.setdefault("direction_id", "D1")
            kwargs.setdefault("keep_option_ids", ["K1"])
        job = routes.JobInput(kind=kind, revision=project["revision"], **kwargs)
        background = BackgroundTasks()
        outputs = {kind: raw}
        if kind == "episode":
            outputs["review"] = REVIEW
            outputs["episode_repair"] = raw if repaired is None else repaired

        def fake_generate(call_kind, data, contract, choice):
            if call_kind == "analyze":
                self.assertEqual(data["goal"], "改成适合分镜绘制的古装漫剧")
                self.assertEqual(data["keep"], "保留核心因果线索")
            if call_kind == "episode" and str(job.episode) in project["outputs"]:
                self.assertEqual(data["current_draft"], project["outputs"][str(job.episode)]["draft"])
                self.assertEqual(data["revision_notes"], job.notes.strip())
            if ad.usage_recorder.get():
                ad.usage_recorder.get()(call_kind, {
                    "provider": choice[0], "model": choice[1], "promptVersion": ad.PROMPT_VERSION,
                    "inputTokens": 100, "outputTokens": 20, "apiCostRmb": 0.01,
                    "costSource": "public_price_estimate",
                })
            return contract.model_validate(outputs[call_kind]), {
                "model": choice[1], "promptVersion": ad.PROMPT_VERSION,
            }

        with patch.object(ad, "model_choice", return_value=("ark", "test-model")), patch.object(
            ad, "generate", side_effect=fake_generate
        ):
            pending = routes.start_job(project["id"], None, job, background)
            self.assertEqual(pending["job"], kind)
            with self.assertRaises(HTTPException) as exc:
                routes.start_job(project["id"], None, job, BackgroundTasks())
            self.assertEqual(exc.exception.status_code, 409)
            task = background.tasks[0]
            task.func(*task.args, **task.kwargs)
        return routes.get_project(project["id"], None)

    def ready(self):
        project = self.create(source_text=SOURCE)
        project = self.generate(project, "analyze", ANALYSIS)
        project = self.generate(project, "preview", {
            "source_id": "S2", "cast": CAST, "scene": SCENE,
            "decisions": ["保留账册作为线索"], "questions": [],
        }, source_id="S2")
        return self.generate(project, "plan", PLAN)

    def confirm_plan(self, project):
        return routes.save_plan(project["id"], None, routes.PlanInput(
            revision=project["revision"], plan=ad.Plan.model_validate(project["plan"]),
            locks=[ad.LockLine.model_validate(item) for item in project["locks"]]))

    def test_full_flow_quality_lock_and_revision(self):
        project = self.ready()
        project_id = project["id"]
        project = routes.save_plan(project_id, None, routes.PlanInput(
            revision=project["revision"], plan=project["plan"], locks=[LOCK]))
        wrong = {"scenes": [{**SCENE, "blocks": [{"speaker_id": "C1", "text": "略去关键台词"}]}],
                 "continuity": "寻找伙计"}
        project = self.generate(project, "episode", wrong)
        self.assertIn("锁定台词缺失", project["error"])
        self.assertEqual(project["outputs"], {})
        project = self.generate(project, "episode", wrong, repaired=DRAFT)
        self.assertIn(f"沈青：{LINE}", routes.download(project_id, None).body.decode())
        self.assertEqual([call["kind"] for call in project["usage"]["1"]["calls"]],
                         ["episode", "episode_repair", "review"])
        project = self.generate(project, "episode", DRAFT, episode=2)
        project = routes.lock_episode(project_id, 2, None, routes.OutputLock(revision=project["revision"], locked=True))
        with self.assertRaises(HTTPException):
            routes.save_episode(project_id, 2, None, routes.DraftInput(revision=project["revision"], draft=DRAFT))
        with self.assertRaises(HTTPException) as exc:
            routes.save_plan(project_id, None, routes.PlanInput(revision=0, plan=project["plan"], locks=[LOCK]))
        self.assertEqual(exc.exception.status_code, 409)
        changed = json.loads(json.dumps(project["plan"]))
        changed["episodes"][1]["hook"] = "街口出现第二本假账"
        project = routes.save_plan(project_id, None, routes.PlanInput(
            revision=project["revision"], plan=changed, locks=[LOCK]))
        self.assertFalse(project["outputs"]["1"]["stale"])
        self.assertTrue(project["outputs"]["2"]["stale"])
        self.assertTrue(project["outputs"]["2"]["locked"])
        with self.assertRaises(HTTPException):
            routes.download(project_id, None)
        project = self.generate(project, "impact", {"affected": [
            {"number": 2, "reason": "集尾证据变化", "suggestion": "补第二本账册来源"}]})
        self.assertEqual(project["impact"][0]["number"], 2)

    def test_project_charge_covers_first_episode_and_admin_sees_model_stages(self):
        project = self.confirm_plan(self.ready())
        self.assertIn("改成适合分镜绘制的古装漫剧", project["goal"])
        self.assertIn("保留核心因果线索", project["keep"])
        with closing(auth_store.db()) as conn:
            self.assertEqual(conn.execute("SELECT credits FROM users WHERE id='owner'").fetchone()[0], 84)
        project = self.generate(project, "episode", DRAFT)
        with closing(auth_store.db()) as conn:
            self.assertEqual(conn.execute("SELECT credits FROM users WHERE id='owner'").fetchone()[0], 84)
        project = self.generate(project, "episode", DRAFT, episode=2)
        failed = self.generate(project, "episode", {}, episode=2, notes="改一下开场")
        self.assertEqual(failed["outputs"]["2"]["version"], 1)
        with closing(auth_store.db()) as conn:
            self.assertEqual(conn.execute("SELECT credits FROM users WHERE id='owner'").fetchone()[0], 68)
        project = self.generate(failed, "episode", DRAFT, episode=2, notes="缩短开场")
        self.assertEqual(project["outputs"]["2"]["version"], 2)
        self.assertEqual(project["outputs"]["2"]["revisionNotes"], "缩短开场")
        with closing(auth_store.db()) as conn:
            self.assertEqual(conn.execute("SELECT credits FROM users WHERE id='owner'").fetchone()[0], 68)
        project = self.generate(project, "episode", DRAFT, episode=2, notes="增加对峙")
        self.assertEqual(project["outputs"]["2"]["version"], 3)
        archive = routes.get_episode_version(project["id"], 2, 2, None)
        self.assertEqual(archive["notes"], "缩短开场")
        self.assertIn(LINE, archive["screenplay"])
        self.current_user.return_value = {"id": "other", "role": "user"}
        with self.assertRaises(HTTPException) as exc:
            routes.get_episode_version(project["id"], 2, 2, None)
        self.assertEqual(exc.exception.status_code, 404)
        with closing(auth_store.db()) as conn:
            self.assertEqual(conn.execute("SELECT credits FROM users WHERE id='owner'").fetchone()[0], 60)
        self.current_user.return_value = {"id": "owner", "role": "admin"}
        page = routes.admin_adaptations(None, kind="episode", limit=20, offset=0)
        self.assertEqual(page["total"], 5)
        self.assertTrue(any(call["kind"] == "review" for call in page["items"][0]["calls"]))
        self.assertEqual(sum(item["credits_charged"] for item in page["categories"]), 24)
        included = next(item for item in page["items"] if item["billing_kind"] == "included_revision"
                        and item["status"] == "success")
        self.assertEqual(included["credits_charged"], 0)

    def test_failed_analysis_refunds_the_reserved_project_fee(self):
        project = self.create(source_text=SOURCE)
        invalid = {**ANALYSIS, "keep_options": [{**ANALYSIS["keep_options"][0], "quote": "不存在的原文"}]}
        project = self.generate(project, "analyze", invalid)
        self.assertIn("原文依据", project["error"])
        with closing(auth_store.db()) as conn:
            self.assertEqual(conn.execute("SELECT credits FROM users WHERE id='owner'").fetchone()[0], 100)
            job = conn.execute("SELECT status, credits_reserved, credits_refunded FROM adaptation_jobs").fetchone()
        self.assertEqual(tuple(job), ("failed", 16, 16))

    def test_repeated_trial_and_outline_charge_eight_and_failed_retry_refunds(self):
        project = self.ready()
        failed = self.generate(project, "preview", {"source_id": "invalid"}, source_id="S2")
        self.assertIn("失败", failed["error"])
        with closing(auth_store.db()) as conn:
            self.assertEqual(conn.execute("SELECT credits FROM users WHERE id='owner'").fetchone()[0], 84)
            refunded = conn.execute(
                "SELECT billing_kind, status, credits_reserved, credits_refunded "
                "FROM adaptation_jobs WHERE billing_kind='preview_retry'"
            ).fetchone()
        self.assertEqual(tuple(refunded), ("preview_retry", "failed", 8, 8))

        project = self.generate(failed, "preview", {
            "source_id": "S2", "cast": CAST, "scene": SCENE,
            "decisions": ["保留账册作为线索"], "questions": [],
        }, source_id="S2")
        project = self.generate(project, "plan", PLAN)
        with closing(auth_store.db()) as conn:
            self.assertEqual(conn.execute("SELECT credits FROM users WHERE id='owner'").fetchone()[0], 68)
            charges = conn.execute(
                "SELECT billing_kind, credits_charged FROM adaptation_jobs "
                "WHERE billing_kind IN ('preview_retry','plan_retry') AND status='success' "
                "ORDER BY started_at"
            ).fetchall()
        self.assertEqual([tuple(row) for row in charges], [
            ("preview_retry", 8), ("plan_retry", 8),
        ])

    def test_full_script_review_requires_every_current_episode_and_is_classified(self):
        project = self.confirm_plan(self.ready())
        project = self.generate(project, "episode", DRAFT)
        with self.assertRaises(HTTPException):
            routes.start_job(project["id"], None, routes.JobInput(
                kind="review_all", revision=project["revision"]), BackgroundTasks())
        project = self.generate(project, "episode", DRAFT, episode=2)
        project = self.generate(project, "review_all", REVIEW)
        self.assertEqual(project["reviewAll"]["summary"], REVIEW["summary"])
        with closing(auth_store.db()) as conn:
            row = conn.execute("SELECT kind, billing_kind, status, credits_reserved FROM adaptation_jobs WHERE kind='review_all'").fetchone()
        self.assertEqual(tuple(row), ("review_all", "included", "success", 0))

    def test_platform_source_owned_and_quality_visible(self):
        script = {"title": "商号旧案", "scenes": [{"heading": "1-1 夜 内 商号", "startSec": 7.0,
                  "blocks": [{"type": "dialogue", "speaker": "沈青", "text": LINE}]}]}
        with closing(auth_store.db()) as conn, conn:
            conn.execute("""INSERT INTO tasks
                (id,user_id,title,file_name,stored_path,status,stage,created_at,updated_at,result_json,delivery_json,quality_json)
                VALUES ('task','owner','商号','01.mp4','','done','done','now','now',?,?,?)""",
                (json.dumps(script), json.dumps(script), json.dumps({"issues": [{"tag": "漏台词", "severity": "P0"}]})))
        project = self.create(task_id="task")
        self.assertIn(LINE, project["sources"][0]["text"])
        self.assertEqual(project["sources"][0]["start_sec"], 7.0)
        self.assertIn("核对", project["warnings"][0])
        self.current_user.return_value = {"id": "other"}
        self.assertEqual(routes.list_projects(None), [])
        for action in (lambda: self.create(task_id="task"), lambda: routes.get_project(project["id"], None)):
            with self.assertRaises(HTTPException) as exc:
                action()
            self.assertEqual(exc.exception.status_code, 404)

    def test_source_correction_keeps_original_and_invalidates_preview(self):
        project = self.create(source_text=SOURCE)
        text = project["sources"][1]["text"].replace("沈青按住封条", "沈青拿起封条")
        project = routes.update_source(project["id"], None, routes.SourceInput(
            revision=project["revision"], source_id="S2", text=text))
        self.assertIn("按住封条", project["sources"][1]["originalText"])
        self.assertIn("拿起封条", project["sources"][1]["text"])

    def test_unregistered_speaker_and_bad_reference_fail(self):
        plan = ad.Plan.model_validate(PLAN)
        draft = ad.Draft.model_validate({"scenes": [{**SCENE, "blocks": [{"speaker_id": "C99", "text": LINE}]}],
                                         "continuity": "下一集"})
        with self.assertRaises(ad.AdaptationError):
            ad.validate_draft(draft, plan, [], 1)
        plan.episodes[0].source_ids = ["S99"]
        with self.assertRaises(ad.AdaptationError):
            ad.validate_plan(plan, ad.split_script(SOURCE), 2)

    def test_preview_repairs_unregistered_speaker_with_original_dialogue_intact(self):
        new_cast = {"id": "C2", "original_name": "伙计", "name": "伙计", "role": "商号伙计"}
        blocks = SCENE["blocks"] + [{"speaker_id": "C2", "text": "老板回来了。"}]
        raw = {"source_id": "S2", "scene": {**SCENE, "blocks": blocks},
               "cast": CAST, "decisions": ["保留账册"], "questions": []}
        original = ad.Preview.model_validate(raw)
        repair = ad.PreviewRepair.model_validate({
            "cast_additions": [new_cast], "corrections": [{"block": 3, "speaker_id": "C2"}], "questions": [],
        })
        choice = ("ark", "test-model")
        context = {"source": {"id": "S2", "text": SOURCE}}
        with patch.object(ad, "generate", side_effect=[
            (original, {"inputTokens": 40, "outputTokens": 30}),
            (repair, {"inputTokens": 70, "outputTokens": 35}),
        ]) as mocked:
            result, usage = ad.generate_preview(context, choice)
        self.assertEqual(result.scene.blocks, original.scene.blocks)
        self.assertEqual(result.cast[-1].name, "伙计")
        self.assertEqual(mocked.call_args.args[0], "preview_repair")
        self.assertEqual(mocked.call_args.args[1]["unregistered_speaker_blocks"], [3])
        self.assertEqual(usage, {"inputTokens": 110, "outputTokens": 65, "repairAttempts": 1})

        removed = repair.model_copy(update={"corrections": [
            ad.SpeakerCorrection(block=2, speaker_id="C2"),
        ]})
        with patch.object(ad, "generate", side_effect=[(original, {}), (removed, {})]) as mocked:
            with self.assertRaises(ad.AdaptationError):
                ad.generate_preview(context, choice)
        self.assertEqual(mocked.call_count, 2)

    def test_progress_discloses_job_only_to_owner(self):
        project = self.create(source_text=SOURCE)
        job = routes.JobInput(kind="analyze", revision=project["revision"])
        with patch.object(ad, "model_choice", return_value=("ark", "test-model")):
            routes.start_job(project["id"], None, job, BackgroundTasks())
        self.assertEqual(routes.get_progress(project["id"], None), {"job": "analyze"})
        self.current_user.return_value = {"id": "other", "role": "user"}
        with self.assertRaises(HTTPException) as error:
            routes.get_progress(project["id"], None)
        self.assertEqual(error.exception.status_code, 404)

    def test_provider_schema_keeps_fields_named_like_schema_annotations(self):
        schema = ad._ark_schema(ad.Plan.model_json_schema())
        self.assertIn("title", schema["properties"])
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        self.assertIn("original_name", schema["properties"]["cast"]["items"]["properties"])
        self.assertNotIn("$defs", schema)

    def test_provider_schema_constrains_analysis_and_review_labels(self):
        analysis = ad._ark_schema(ad.Analysis.model_json_schema())
        kinds = analysis["properties"]["keep_options"]["items"]["properties"]["kind"]
        self.assertEqual(kinds["enum"], ["character", "plot", "ending", "line"])
        review = ad._ark_schema(ad.ReviewReport.model_json_schema())
        severity = review["properties"]["issues"]["items"]["properties"]["severity"]
        self.assertEqual(severity["enum"], ["error", "note"])

    def test_uploaded_docx_preserves_paragraphs_and_runs(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as doc:
            doc.writestr("word/document.xml", """<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
                <w:body><w:p><w:r><w:t>沈青：</w:t></w:r><w:r><w:t>照原文。</w:t></w:r></w:p>
                <w:p><w:r><w:t>△ 收起账册。</w:t></w:r></w:p></w:body></w:document>""")
        self.assertEqual(routes._read_document(data.getvalue(), "test.docx"), "沈青：照原文。\n△ 收起账册。")
        with self.assertRaises(ad.AdaptationError):
            routes._read_document(b"invalid", "test.docx")


if __name__ == "__main__":
    unittest.main()
