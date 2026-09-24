import json
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import maintain as m


def item(number=1):
    ident = "WWZT-%03d" % number
    return {"id": ident, "path": "knowledge/" + ident + ".md", "question": "问题 %s 怎么解决" % number,
            "aliases": [], "category": "tools", "summary": "可按课程答疑中的方法排查。", "source_label": "课程合编答疑",
            "source_date": None, "verified_at": "2026-09-23", "evidence_type": "course_faq_compilation",
            "public_status": "pending_owner_review", "applicable_when": "使用同一课程配套工具", "limitations": "不保证不同版本适用", "tags": []}


class MaintainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.ws = self.root / "maintenance"
        self.skill = self.root / "skill"
        (self.skill / "knowledge").mkdir(parents=True)
        self.old_content = "# 旧答案\n\n" + "按旧版的课程条件做相应操作并查看实际结果。" * 4
        (self.skill / "knowledge/WWZT-001.md").write_text(self.old_content)
        m.write_json(self.skill / "knowledge/catalog.json", {"schema_version": 1, "items": [item()]})
        m.write_json(self.skill / "version.json", {"version": "0.1.0", "skill": "wenwen-zhentao"})
        (self.skill / "knowledge/INDEX.md").write_text("旧目录")
        self.source = self.root / "source.md"
        self.source.write_text("# Q1\n先核对工具版本和报错文字，再针对当前步骤修正。\n后续需要看实际结果才能确认是否解决。")
        self.source_id = m.manual_local(self.source, self.ws)["source_id"]

    def candidate(self, number=1):
        return {"item": item(number), "content": "# 新答案\n\n" + "先核对工具版本和报错文字，再针对当前步骤修正。" * 3,
                "evidence": {"source_id": self.source_id, "anchor": "Q1", "quote": "先核对工具版本和报错文字，再针对当前步骤修正。"}}

    def stage(self, entries=None):
        inp = self.root / "candidate.json"
        m.write_json(inp, {"items": entries or [self.candidate()]})
        return m.stage(inp, self.skill, self.ws)["batch"]

    def approve(self, batch, ids="all"):
        review = m.review(batch, self.ws)
        return m.approve(batch, review["review_digest"], "测试审核人", ids, True, self.ws)

    def test_local_cache_stable_and_media_deferred(self):
        repeat = m.manual_local(self.source, self.ws)
        self.assertFalse(repeat["changed"])
        self.source.write_text(self.source.read_text() + "\n新内容")
        updated = m.manual_local(self.source, self.ws)
        self.assertEqual(self.source_id, updated["source_id"])
        self.assertTrue(updated["changed"])
        video = self.root / "lesson.mp4"
        video.write_bytes(b"not a transcript")
        result = m.manual_local(video, self.ws)
        self.assertEqual("needs_transcription", result["status"])
        self.assertEqual(2, len(list((self.ws / "snapshots" / self.source_id).glob("*.json"))))

    def test_lark_envelopes_and_redacted_errors(self):
        good = subprocess.CompletedProcess([], 0, 'notice\n' + json.dumps({"ok": True, "data": {"a": 1}}), "")
        with mock.patch.object(m.subprocess, "run", return_value=good):
            self.assertEqual({"a": 1}, m.lark(["docs", "+fetch", "--doc", "abc"]))
        bad = subprocess.CompletedProcess([], 1, "", json.dumps({"ok": False, "error": {"code": 999, "message": "authcode=PRIVATE"}}))
        with mock.patch.object(m.subprocess, "run", return_value=bad):
            with self.assertRaises(m.MaintenanceError) as caught:
                m.lark(["docs", "+fetch", "--doc", "abc"])
        self.assertNotIn("PRIVATE", str(caught.exception))
        invalid = subprocess.CompletedProcess([], 0, "not json", "")
        with mock.patch.object(m.subprocess, "run", return_value=invalid):
            with self.assertRaises(m.EnvelopeError):
                m.lark(["docs", "+fetch", "--doc", "abc"])

    def test_parse_failure_stops_batch_preserving_success(self):
        registry = self.root / "registry.json"
        m.write_json(registry, {"schema_version": 1, "sources": [
            {"id": "doc-a", "kind": "lark_doc", "url": "https://a.feishu.cn/docx/abc", "enabled": True},
            {"id": "doc-b", "kind": "lark_doc", "url": "https://a.feishu.cn/docx/def", "enabled": True},
            {"id": "doc-c", "kind": "lark_doc", "url": "https://a.feishu.cn/docx/ghi", "enabled": True}]})
        with mock.patch.object(m, "lark", side_effect=[{"document": {"content": "完整正文内容", "revision_id": 1}}, m.EnvelopeError("信封错误")]) as cli:
            result = m.sync(registry, self.ws)
        self.assertEqual(2, cli.call_count)
        self.assertTrue(result["stopped"])
        self.assertEqual("ready", m.read_json(self.ws / "state.json")["sources"]["doc-a"]["status"])
        with mock.patch.object(m, "lark", return_value={"document": {"content": "补齐其余课程正文", "revision_id": 1}}) as cli:
            resumed = m.sync(registry, self.ws)
        self.assertEqual(2, cli.call_count)
        self.assertFalse(resumed["stopped"])
        self.assertFalse(any("https://a.feishu.cn/docx/abc" in c.args[0] for c in cli.call_args_list))

    def test_wiki_pagination_and_stable_children(self):
        registry = self.root / "registry.json"
        m.write_json(registry, {"schema_version": 1, "sources": [{"id": "course", "kind": "wiki_space", "space_id": "123", "enabled": True}]})
        def fake(args):
            if args[:2] == ["docs", "+fetch"]:
                return {"document": {"content": "完整的课程正文", "revision_id": 1}}
            if "--page-token" in args:
                return {"nodes": [{"node_token": "node2", "obj_token": "doc2", "obj_type": "docx"}], "has_more": False}
            return {"nodes": [{"node_token": "node1", "obj_token": "doc1", "obj_type": "docx"}], "has_more": True, "page_token": "next"}
        with mock.patch.object(m, "lark", side_effect=fake) as cli:
            result = m.sync(registry, self.ws)
        self.assertEqual(2, result["changed"])
        self.assertTrue(any("--page-token" in call.args[0] for call in cli.call_args_list))

    def test_stage_rejects_forged_quote_bad_path_and_duplicate_question(self):
        forged = self.candidate()
        forged["evidence"]["quote"] = "原材料中完全不存在的所谓原话"
        with self.assertRaises(m.MaintenanceError):
            self.stage([forged])
        bad = self.candidate()
        bad["item"]["path"] = "../escape.md"
        with self.assertRaises((m.MaintenanceError, ValueError)):
            self.stage([bad])
        duplicate = self.candidate(2)
        duplicate["item"]["question"] = item()["question"]
        with self.assertRaises(m.MaintenanceError):
            self.stage([duplicate])

    def test_approval_needs_confirmation_and_review_digest(self):
        batch = self.stage()
        check = m.review(batch, self.ws)
        with self.assertRaises(m.MaintenanceError):
            m.approve(batch, check["review_digest"], "我", "all", False, self.ws)
        with self.assertRaises(m.MaintenanceError):
            m.approve(batch, "0" * 64, "我", "all", True, self.ws)
        with self.assertRaises(m.MaintenanceError):
            m.apply(batch, "0.2.0", self.skill, self.ws)

    def test_candidate_tamper_invalidates_review_and_approval(self):
        batch = self.stage()
        self.approve(batch)
        path = self.ws / "batches" / batch / "candidate.json"
        candidate = m.read_json(path)
        candidate["items"][0]["content"] += "\n后来私自改的内容"
        m.write_json(path, candidate)
        with self.assertRaises(m.MaintenanceError):
            m.apply(batch, "0.2.0", self.skill, self.ws)
        self.assertEqual(self.old_content, (self.skill / "knowledge/WWZT-001.md").read_text())

    def test_review_edit_after_approval_invalidates_apply(self):
        batch = self.stage()
        self.approve(batch)
        path = self.ws / "batches" / batch / "review.json"
        report = m.read_json(path)
        report["changes"][0]["test_status"] = "changed after approval"
        m.write_json(path, report)
        with self.assertRaises(m.MaintenanceError):
            m.apply(batch, "0.2.0", self.skill, self.ws)

    def test_source_tamper_invalidates_candidate(self):
        batch = self.stage()
        snapshot = next((self.ws / "snapshots" / self.source_id).glob("*.json"))
        value = m.read_json(snapshot)
        value["content"] += "篡改原文"
        m.write_json(snapshot, value)
        with self.assertRaises(m.MaintenanceError):
            m.review(batch, self.ws)

    def test_unchanged_sync_cli_is_quiet(self):
        registry = self.root / "registry.json"
        m.write_json(registry, {"schema_version": 1, "sources": [{"id": "source", "kind": "local", "path": str(self.source), "enabled": True}]})
        m.sync(registry, self.ws)
        with mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            code = m.main(["sync", "--registry", str(registry), "--workspace", str(self.ws)])
        self.assertEqual(0, code)
        self.assertEqual("", output.getvalue())

    def test_changed_baseline_blocks_apply(self):
        batch = self.stage()
        self.approve(batch)
        card = self.skill / "knowledge/WWZT-001.md"
        card.write_text(self.old_content + "另一个任务的修改")
        with self.assertRaises(m.MaintenanceError):
            m.apply(batch, "0.2.0", self.skill, self.ws)
        self.assertTrue(card.read_text().endswith("另一个任务的修改"))

    def test_apply_only_selected_ids_and_keeps_metadata(self):
        batch = self.stage([self.candidate(), self.candidate(2)])
        self.approve(batch, "WWZT-002")
        result = m.apply(batch, "0.2.0", self.skill, self.ws)
        self.assertEqual(["WWZT-002"], result["applied"])
        self.assertEqual(self.old_content, (self.skill / "knowledge/WWZT-001.md").read_text())
        catalog = m.read_json(self.skill / "knowledge/catalog.json")
        self.assertEqual("pending_owner_review", catalog["items"][0]["public_status"])
        # local_apply_only 只让知识在本人本机生效，不授予公开许可。
        self.assertEqual("pending_owner_review", catalog["items"][1]["public_status"])
        self.assertEqual("wenwen-zhentao", m.read_json(self.skill / "version.json")["skill"])
        self.assertEqual("0.2.0", m.read_json(self.skill / "version.json")["version"])
        self.assertNotIn("snapshots", [x.name for x in self.skill.iterdir()])

    def test_failed_activation_rolls_back(self):
        batch = self.stage()
        self.approve(batch)
        original_replace = os.replace
        def fail_activation(src, dst):
            if Path(src).name == "new" and Path(dst) == self.skill:
                raise OSError("simulated disk failure")
            return original_replace(src, dst)
        with mock.patch.object(m.os, "replace", side_effect=fail_activation):
            with self.assertRaises(m.MaintenanceError):
                m.apply(batch, "0.2.0", self.skill, self.ws)
        self.assertEqual(self.old_content, (self.skill / "knowledge/WWZT-001.md").read_text())
        self.assertEqual("0.1.0", m.read_json(self.skill / "version.json")["version"])

    def test_copy_time_edit_of_unselected_file_is_preserved(self):
        entry = self.skill / "SKILL.md"
        entry.write_text("入口旧版")
        batch = self.stage()
        self.approve(batch)
        original_copy = m.shutil.copytree
        def edited_after_copy(src, dst, *args, **kwargs):
            result = original_copy(src, dst, *args, **kwargs)
            if Path(src) == self.skill:
                entry.write_text("用户刚刚修改的入口，不可以被旧副本覆盖")
            return result
        with mock.patch.object(m.shutil, "copytree", side_effect=edited_after_copy):
            with self.assertRaises(m.MaintenanceError):
                m.apply(batch, "0.2.0", self.skill, self.ws)
        self.assertEqual("用户刚刚修改的入口，不可以被旧副本覆盖", entry.read_text())
        self.assertEqual("0.1.0", m.read_json(self.skill / "version.json")["version"])

    def test_keyboard_interrupt_between_renames_restores_old_install(self):
        batch = self.stage()
        self.approve(batch)
        original_replace = os.replace
        def interrupted(src, dst):
            if Path(src).name == "new" and Path(dst) == self.skill:
                raise KeyboardInterrupt()
            return original_replace(src, dst)
        with mock.patch.object(m.os, "replace", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                m.apply(batch, "0.2.0", self.skill, self.ws)
        self.assertTrue(self.skill.is_dir())
        self.assertEqual(self.old_content, (self.skill / "knowledge/WWZT-001.md").read_text())
        self.assertFalse(m.journal_path(self.skill).exists())

    def force_exit_during_apply(self, batch, after_switch=False):
        script = """import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import maintain
original = os.replace
def crash(src, dst):
    if Path(src).name == 'new' and Path(dst) == Path(sys.argv[3]):
        if sys.argv[5] == 'after':
            original(src, dst)
        os._exit(73)
    return original(src, dst)
maintain.os.replace = crash
maintain.apply(sys.argv[2], '0.2.0', sys.argv[3], sys.argv[4])
"""
        result = subprocess.run([sys.executable, "-c", script, str(ROOT / "tools"), batch, str(self.skill), str(self.ws), "after" if after_switch else "before"], capture_output=True)
        self.assertEqual(73, result.returncode)
        return result

    def test_forced_exit_recovers_from_persisted_journal(self):
        batch = self.stage()
        self.approve(batch)
        self.force_exit_during_apply(batch)
        self.assertFalse(self.skill.exists())
        self.assertTrue(m.journal_path(self.skill).is_file())
        # 子进程已退出并被 wait/reap，按共享锁契约显式清理该已死进程残留锁。
        (self.skill.parent / ("." + self.skill.name + ".update.lock")).unlink()
        recovered = m.recover(self.skill, self.ws)
        self.assertEqual("rolled_back", recovered["status"])
        self.assertEqual(self.old_content, (self.skill / "knowledge/WWZT-001.md").read_text())
        self.assertFalse(m.journal_path(self.skill).exists())
        # 可重新应用原先已审阅、未提交成功的批次。
        self.assertEqual("0.2.0", m.apply(batch, "0.2.0", self.skill, self.ws)["version"])

    def test_next_apply_recovers_exit_after_new_install_before_receipt(self):
        batch = self.stage()
        self.approve(batch)
        self.force_exit_during_apply(batch, after_switch=True)
        self.assertTrue(self.skill.is_dir())
        self.assertEqual("0.2.0", m.read_json(self.skill / "version.json")["version"])
        self.assertFalse((self.ws / "batches" / batch / "applied.json").exists())
        (self.skill.parent / ("." + self.skill.name + ".update.lock")).unlink()
        # 同版本再次 apply 必须先回滚未提交安装，才能合法按旧基线重新应用。
        result = m.apply(batch, "0.2.0", self.skill, self.ws)
        self.assertEqual("0.2.0", result["version"])
        self.assertFalse(m.journal_path(self.skill).exists())

    def test_edit_before_switch_preserves_non_candidate_card(self):
        other = self.skill / "knowledge/WWZT-002.md"
        other.write_text(self.old_content)
        catalog = m.read_json(self.skill / "knowledge/catalog.json")
        catalog["items"].append(item(2))
        m.write_json(self.skill / "knowledge/catalog.json", catalog)
        batch = self.stage()
        self.approve(batch)
        original_index = m.write_index
        def edit_while_preparing(new, catalog):
            original_index(new, catalog)
            other.write_text(self.old_content + "维护时用户更新的其他答案")
        with mock.patch.object(m, "write_index", side_effect=edit_while_preparing):
            with self.assertRaises(m.MaintenanceError):
                m.apply(batch, "0.2.0", self.skill, self.ws)
        self.assertTrue(other.read_text().endswith("维护时用户更新的其他答案"))
        self.assertFalse(m.journal_path(self.skill).exists())

    def test_updater_shared_lock_blocks_maintenance(self):
        batch = self.stage()
        self.approve(batch)
        lock = self.skill.parent / ("." + self.skill.name + ".update.lock")
        lock.write_text(json.dumps({"pid": os.getpid(), "created_at": 1}))
        with self.assertRaises(m.MaintenanceError):
            m.apply(batch, "0.2.0", self.skill, self.ws)
        self.assertTrue(lock.exists())
        self.assertEqual(self.old_content, (self.skill / "knowledge/WWZT-001.md").read_text())


if __name__ == "__main__":
    unittest.main()
