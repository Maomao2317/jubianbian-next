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


class AdaptationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(gc.collect)
        db_patch = patch.object(auth_store, "DB_PATH", Path(self.tmp.name) / "project.sqlite3")
        db_patch.start()
        self.addCleanup(db_patch.stop)
        auth = patch.object(routes, "current_user", return_value={"id": "owner"})
        self.current_user = auth.start()
        self.addCleanup(auth.stop)
        auth_store.init_db()
        routes.init_adaptations()

    def create(self, **source):
        return routes.create_project(None, routes.ProjectInput(
            goal="改成适合分镜绘制的古装漫剧", episode_count=2, **source))

    def generate(self, project, kind, raw, **kwargs):
        job = routes.JobInput(kind=kind, revision=project["revision"], **kwargs)
        background = BackgroundTasks()
        with patch.object(ad, "model_choice", return_value=("ark", "test-model")), patch.object(
            ad, "generate", side_effect=lambda _, data, contract, choice: (
                contract.model_validate(raw), {"model": choice[1], "promptVersion": ad.PROMPT_VERSION}
            )
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
        project = self.generate(project, "preview", {
            "source_id": "S2", "cast": CAST, "scene": SCENE,
            "decisions": ["保留账册作为线索"], "questions": [],
        }, source_id="S2")
        return self.generate(project, "plan", PLAN)

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
        project = self.generate(project, "episode", DRAFT)
        self.assertIn(f"沈青：{LINE}", routes.download(project_id, None).body.decode())
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

    def test_provider_schema_keeps_fields_named_like_schema_annotations(self):
        schema = ad._ark_schema(ad.Plan.model_json_schema())
        self.assertIn("title", schema["properties"])
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        self.assertIn("original_name", schema["properties"]["cast"]["items"]["properties"])
        self.assertNotIn("$defs", schema)

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
