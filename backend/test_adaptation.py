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
        if kind == "analyze":
            kwargs.setdefault("expected_points", project["billing"]["projectPoints"])
        if kind == "episode" and kwargs.get("episode", 1) > 1 and str(kwargs["episode"]) not in project["outputs"]:
            kwargs.setdefault("expected_points", project["billing"]["episodePoints"])
        if kind in {"preview", "plan", "impact", "review_all"} or (
            kind == "episode" and str(kwargs.get("episode", 1)) in project["outputs"]
        ):
            kwargs.setdefault("expected_points", 8)
        job = routes.JobInput(kind=kind, revision=project["revision"], **kwargs)
        background = BackgroundTasks()
        outputs = {kind: raw}
        if kind == "plan":
            outputs["plan"] = {key: value for key, value in raw.items() if key != "cast"}
            outputs["plan"]["additional_cast"] = [
                {key: value for key, value in character.items() if key != "id"} for character in raw["cast"]
            ]
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
        markdown = routes.download(project_id, None)
        plain = routes.download(project_id, None, fmt="txt")
        self.assertTrue(markdown.headers["content-type"].startswith("text/markdown"))
        self.assertTrue(markdown.headers["content-disposition"].endswith(".md"))
        self.assertTrue(plain.headers["content-type"].startswith("text/plain; charset=utf-8"))
        self.assertTrue(plain.headers["content-disposition"].endswith(".txt"))
        self.assertTrue(plain.body.decode().startswith(f"{PLAN['title']}\n\n人物介绍\n"))
        self.assertIn("第 1 集\n", plain.body.decode())
        self.assertIn(f"沈青：{LINE}", plain.body.decode())
        self.assertEqual(markdown.body.decode().replace("# " + PLAN["title"], PLAN["title"], 1)
                         .replace("## 人物介绍", "人物介绍")
                         .replace("## 第 1 集", "第 1 集", 1), plain.body.decode())
        self.assertNotIn("usage", project)
        with patch.object(routes, "current_user", return_value={"id": "owner", "role": "admin"}):
            jobs = routes.admin_adaptations(None, status="success", kind="episode", limit=20, offset=0)
        self.assertEqual([call["kind"] for call in jobs["items"][0]["calls"]],
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
        with self.assertRaises(HTTPException):
            routes.download(project_id, None, fmt="txt")
        project = self.generate(project, "impact", {"affected": [
            {"number": 2, "reason": "集尾证据变化", "suggestion": "补第二本账册来源"}]})
        self.assertEqual(project["impact"][0]["number"], 2)

    def test_project_charge_covers_first_draft_and_admin_sees_paid_ai_actions(self):
        project = self.confirm_plan(self.ready())
        self.assertIn("改成适合分镜绘制的古装漫剧", project["goal"])
        self.assertIn("保留核心因果线索", project["keep"])
        with closing(auth_store.db()) as conn:
            self.assertEqual(conn.execute("SELECT credits FROM users WHERE id='owner'").fetchone()[0], 64)
        project = self.generate(project, "episode", DRAFT)
        with closing(auth_store.db()) as conn:
            self.assertEqual(conn.execute("SELECT credits FROM users WHERE id='owner'").fetchone()[0], 64)
        project = self.generate(project, "episode", DRAFT, episode=2)
        failed = self.generate(project, "episode", {}, episode=2, notes="改一下开场")
        self.assertEqual(failed["outputs"]["2"]["version"], 1)
        with closing(auth_store.db()) as conn:
            self.assertEqual(conn.execute("SELECT credits FROM users WHERE id='owner'").fetchone()[0], 44)
        project = self.generate(failed, "episode", DRAFT, episode=2, notes="缩短开场")
        self.assertEqual(project["outputs"]["2"]["version"], 2)
        self.assertEqual(project["outputs"]["2"]["revisionNotes"], "缩短开场")
        with closing(auth_store.db()) as conn:
            self.assertEqual(conn.execute("SELECT credits FROM users WHERE id='owner'").fetchone()[0], 36)
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
            self.assertEqual(conn.execute("SELECT credits FROM users WHERE id='owner'").fetchone()[0], 28)
        self.current_user.return_value = {"id": "owner", "role": "admin"}
        page = routes.admin_adaptations(None, kind="episode", limit=20, offset=0)
        self.assertEqual(page["total"], 5)
        self.assertTrue(any(call["kind"] == "review" for call in page["items"][0]["calls"]))
        self.assertEqual(sum(item["credits_charged"] for item in page["categories"]), 36)
        rewrites = [item for item in page["items"] if item["billing_kind"] == "rewrite"]
        self.assertCountEqual([(item["credits_charged"], item["credits_refunded"]) for item in rewrites],
                              [(8, 0), (8, 0), (0, 8)])

    def test_failed_analysis_refunds_the_reserved_project_fee(self):
        project = self.create(source_text=SOURCE)
        invalid = {**ANALYSIS, "directions": [ANALYSIS["directions"][0]] * 2}
        project = self.generate(project, "analyze", invalid)
        self.assertIn("改编方向编号重复", project["error"])
        with closing(auth_store.db()) as conn:
            self.assertEqual(conn.execute("SELECT credits FROM users WHERE id='owner'").fetchone()[0], 100)
            job = conn.execute("SELECT status, credits_reserved, credits_refunded FROM adaptation_jobs").fetchone()
        self.assertEqual(tuple(job), ("failed", 20, 20))

    def test_old_quoted_price_does_not_charge_and_paid_setup_still_covers_first_episode(self):
        project = self.create(source_text=SOURCE)
        with patch.object(ad, "model_choice", return_value=("ark", "test-model")):
            for quote in (None, 16):
                with self.subTest(quote=quote), self.assertRaises(HTTPException) as exc:
                    routes.start_job(project["id"], None, routes.JobInput(
                        kind="analyze", revision=project["revision"], expected_points=quote,
                    ), BackgroundTasks())
                self.assertEqual(exc.exception.status_code, 409)
        with closing(auth_store.db()) as conn:
            self.assertEqual(conn.execute("SELECT credits FROM users WHERE id='owner'").fetchone()[0], 100)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM adaptation_jobs").fetchone()[0], 0)
        with patch.object(routes, "PROJECT_POINTS", 16):
            already_paid = self.confirm_plan(self.ready())
        already_paid = self.generate(already_paid, "episode", DRAFT, expected_points=0)
        self.assertIn("1", already_paid["outputs"])
        with closing(auth_store.db()) as conn:
            self.assertEqual(conn.execute("SELECT credits FROM users WHERE id='owner'").fetchone()[0], 68)

    def test_analysis_recovers_formatted_and_misplaced_quotes_and_omits_missing_quotes(self):
        project = self.create(source_text=SOURCE)
        keep = ANALYSIS["keep_options"][0]
        partial = {**ANALYSIS, "preview_source_id": "does-not-exist", "keep_options": [
            {**keep, "source_id": "S4", "quote": "沈青: 账册不能 离开柜台."},
            {**keep, "id": "K2", "quote": "不存在的原文"},
        ]}
        project = self.generate(project, "analyze", partial)
        self.assertNotIn("error", project)
        self.assertEqual(project["analysis"]["preview_source_id"], "S2")
        self.assertEqual(project["analysis"]["keep_options"], [{**keep, "quote": "沈青：" + LINE}])
        self.assertNotIn("usage", project)
        with patch.object(ad, "model_choice", return_value=("ark", "private-model")):
            self.assertEqual(routes.availability(None), {"enabled": True, "ready": True, "projectPoints": 20})

    def test_analysis_with_no_verified_quote_can_still_proceed_to_trial(self):
        project = self.create(source_text=SOURCE)
        keep = ANALYSIS["keep_options"][0]
        partial = {**ANALYSIS, "keep_options": [{**keep, "quote": "不存在的原文"}]}
        project = self.generate(project, "analyze", partial)
        self.assertEqual(project["analysis"]["keep_options"], [])
        self.assertTrue(project["billing"]["setupPaid"])
        project = self.generate(project, "preview", {
            "source_id": "S2", "cast": CAST, "scene": SCENE,
            "decisions": ["保留账册作为线索"], "questions": [],
        }, source_id="S2", keep_option_ids=[])
        self.assertIn("preview", project)

    def test_projects_remain_browsable_without_a_generation_provider(self):
        project = self.create(source_text=SOURCE)
        with patch.object(ad, "model_choice", side_effect=ad.AdaptationError("No model configured")):
            self.assertEqual(routes.availability(None), {"enabled": True, "ready": False, "projectPoints": 20})
            self.assertEqual(routes.get_project(project["id"], None)["id"], project["id"])
            self.assertIn(project["id"], [item["id"] for item in routes.list_projects(None)])

    def test_analysis_does_not_reassign_ambiguous_quotes(self):
        option = {**ANALYSIS["keep_options"][0], "source_id": "other"}
        analysis = ad.Analysis.model_validate({**ANALYSIS, "keep_options": [option]})
        sources = [ad.SourceScene(id=id, label=id, text=LINE) for id in ("S2", "S3")]
        self.assertEqual(ad.ground_analysis(analysis, sources).keep_options, [])

    def test_repeated_trial_and_outline_charge_eight_and_failed_retry_refunds(self):
        project = self.ready()
        failed = self.generate(project, "preview", {"source_id": "invalid"}, source_id="S2")
        self.assertIn("失败", failed["error"])
        with closing(auth_store.db()) as conn:
            self.assertEqual(conn.execute("SELECT credits FROM users WHERE id='owner'").fetchone()[0], 64)
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
            self.assertEqual(conn.execute("SELECT credits FROM users WHERE id='owner'").fetchone()[0], 48)
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
        self.assertEqual(tuple(row), ("review_all", "review", "success", 8))

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
        job = routes.JobInput(kind="analyze", revision=project["revision"], expected_points=20)
        with patch.object(ad, "model_choice", return_value=("ark", "test-model")):
            routes.start_job(project["id"], None, job, BackgroundTasks())
        progress = routes.get_progress(project["id"], None)
        self.assertEqual(progress["job"], "analyze")
        self.assertEqual(progress["progress"]["status"], "running")
        self.assertEqual(progress["progress"]["percent"], 0)
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

    def test_plan_keeps_preview_cast_and_assigns_ids_to_new_characters(self):
        preview_cast = [ad.Character.model_validate({**CAST[0], "id": "C2"})]
        proposal = ad.PlanProposal(
            title=PLAN["title"], premise=PLAN["premise"], episodes=PLAN["episodes"],
            additional_cast=[
                {"original_name": "沈青", "name": "沈清", "role": "与试改冲突"},
                {"original_name": "迟巧", "name": "迟巧", "role": "商号伙计"},
            ],
        )
        plan = ad.plan_from_preview(proposal, preview_cast)
        self.assertEqual(plan.cast[0], preview_cast[0])
        self.assertEqual([(item.id, item.name) for item in plan.cast], [("C2", "沈青"), ("C1", "迟巧")])

    def test_uploaded_docx_preserves_paragraphs_and_runs(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as doc:
            doc.writestr("word/document.xml", """<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
                <w:body><w:p><w:r><w:t>沈青：</w:t></w:r><w:r><w:t>照原文。</w:t></w:r></w:p>
                <w:p><w:r><w:t>△ 收起账册。</w:t></w:r></w:p></w:body></w:document>""")
        self.assertEqual(routes._read_document(data.getvalue(), "test.docx"), "沈青：照原文。\n△ 收起账册。")
        with self.assertRaises(ad.AdaptationError):
            routes._read_document(b"invalid", "test.docx")

    def test_staging_generation_continues_past_old_daily_limits(self):
        with closing(auth_store.db()) as conn, conn:
            conn.executemany("INSERT INTO adaptation_attempts VALUES (?,?)",
                             [("owner", routes.time.time())] * 151)
        project = self.generate(self.create(source_text=SOURCE), "analyze", ANALYSIS)
        self.assertIn("analysis", project)

    def test_episode_admin_view_combines_rewrites_and_keeps_latest_status(self):
        project = self.confirm_plan(self.ready())
        project = self.generate(project, "episode", DRAFT)
        project = self.generate(project, "episode", DRAFT, notes="加强冲突")
        self.current_user.return_value = {"id": "owner", "role": "admin"}
        result = routes.admin_adaptations(None, view="episodes", limit=20, offset=0)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["episode"], 1)
        self.assertEqual(result["items"][0]["credits_charged"], 8)
        self.assertAlmostEqual(result["items"][0]["api_cost_rmb"], 0.04)
        self.assertEqual(result["items"][0]["status"], "success")
        with closing(auth_store.db()) as conn, conn:
            conn.execute("""INSERT INTO adaptation_jobs
                (id,project_id,project_title,user_id,kind,billing_kind,episode,status,error,started_at,api_cost_rmb)
                VALUES ('latest',?,'商号旧案','owner','episode','rewrite',1,'failed','内容不完整','9999',0.03)""",
                (project["id"],))
        result = routes.admin_adaptations(None, view="episodes", status="failed", limit=20, offset=0)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["credits_charged"], 8)
        self.assertAlmostEqual(result["items"][0]["api_cost_rmb"], 0.07)
        self.assertEqual(result["items"][0]["error"], "内容不完整")
        self.assertEqual(routes.admin_adaptations(None, view="episodes", status="success", limit=20, offset=0)["total"], 0)

    def test_character_introductions_compatible_with_old_projects_and_exports(self):
        project = self.confirm_plan(self.ready())
        self.assertEqual(project["plan"]["cast"][0]["biography"], "")
        project["plan"]["cast"][0].update(biography="精于查账，追查账册下落。",
                                          relationships="与掌柜相互试探。", motivation="守住真实账目。")
        project = routes.save_plan(project["id"], None, routes.PlanInput(
            revision=project["revision"], plan=project["plan"], locks=project["locks"]))
        project = self.generate(project, "episode", DRAFT)
        for fmt in ("md", "txt"):
            content = routes.download(project["id"], None, fmt).body.decode()
            self.assertIn("人物介绍", content)
            self.assertIn("精于查账", content)
            self.assertIn("守住真实账目", content)

    def test_admin_user_detail_is_authorized_paginated_and_excludes_secrets(self):
        with self.assertRaises(HTTPException) as error:
            routes.admin_user_detail(None, "owner", 0, 0, 0)
        self.assertEqual(error.exception.status_code, 403)
        self.current_user.return_value = {"id": "owner", "role": "admin"}
        with closing(auth_store.db()) as conn, conn:
            conn.executemany("""INSERT INTO credit_ledger
                (user_id,amount,balance_after,entry_type,reason,created_at)
                VALUES ('owner',1,100,'recharge','test','now')""", [()] * 25)
        detail = routes.admin_user_detail(None, "owner", 20, 0, 0)
        self.assertNotIn("password_hash", detail["user"])
        self.assertEqual(detail["ledger"]["total"], 25)
        self.assertEqual(len(detail["ledger"]["items"]), 5)
        self.assertEqual(detail["totals"]["recharged"], 25)
        with self.assertRaises(HTTPException) as error:
            routes.admin_user_detail(None, "unknown", 0, 0, 0)
        self.assertEqual(error.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
