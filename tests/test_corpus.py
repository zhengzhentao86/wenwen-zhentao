import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import corpus as c
import maintain as m
from wwzt_core import digest, read_json, write_json


def response(data):
    value = m.LarkData(data)
    value.envelope = {"ok": True, "identity": "user", "data": data}
    return value


def node(token, obj=None, children=False, kind="docx"):
    return {"node_token": token, "obj_token": obj or "obj" + token, "obj_type": kind,
            "has_child": children, "title": "标题" + token, "space_id": "123"}


class CorpusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.ws = self.root / ".maintenance"
        self.registry = self.root / "sources.json"

    def registry_for(self, sources):
        write_json(self.registry, {"schema_version": 1, "sources": sources})
        return self.registry

    def state(self):
        return read_json(self.ws / "corpus/state.json")

    def doc_sources(self):
        return [{"id": "first", "kind": "lark_doc", "url": "https://example.feishu.cn/docx/a", "enabled": True},
                {"id": "second", "kind": "lark_doc", "url": "https://example.feishu.cn/docx/b", "enabled": True}]

    def review_fixture(self):
        c.inventory(self.registry_for(self.doc_sources()), self.ws)
        with mock.patch.object(c, "lark", return_value=response({"document": {"content": "完整原文与操作建议"}})):
            c.fetch(self.ws)
        return [{"document_id": d["id"], "content_hash": d["current"]["content_hash"],
                 "text_read_complete": True, "decision": "covered", "knowledge_ids": ["WWZT-001"],
                 "reviewer": "AI测试审阅记录", "reading_evidence": "逐段阅读台账",
                 "reason": "与既有知识对应的操作流程相同", "media_boundary": "媒体未观看"}
                for d in self.state()["documents"].values()]

    def record_reviews(self, entries):
        report = self.root / "review.json"
        write_json(report, {"schema_version": 1, "documents": entries})
        with mock.patch.object(c, "validate_catalog", return_value={"items": [{"id": "WWZT-001"}]}):
            return c.record_text_review(report, self.ws, self.root)

    def test_review_stale_hash_rejects_entire_batch(self):
        entries = self.review_fixture()
        entries[-1]["content_hash"] = "0" * 64
        with self.assertRaises(m.MaintenanceError):
            self.record_reviews(entries)
        self.assertEqual(c.status(self.ws)["text_reviewed"], 0)
        self.assertFalse((self.ws / "corpus/reviews").exists())

    def test_review_cannot_claim_partial_reading_or_missing_knowledge(self):
        entries = self.review_fixture()
        entries[0]["text_read_complete"] = False
        with self.assertRaises(m.MaintenanceError):
            self.record_reviews(entries)
        entries[0]["text_read_complete"] = True
        entries[0]["knowledge_ids"] = ["WWZT-99999"]
        with self.assertRaises(m.MaintenanceError):
            self.record_reviews(entries)
        self.assertEqual(c.status(self.ws)["text_reviewed"], 0)

    def test_review_survives_unchanged_refresh_but_changed_text_reopens_review(self):
        entries = self.review_fixture()
        first = self.record_reviews(entries)
        self.assertEqual(first["text_reviewed"], 2)
        self.assertEqual(first["distilled"], 0)  # 已覆盖不是新产生两份知识。
        with mock.patch.object(c, "lark", return_value=response({"document": {"content": "完整原文与操作建议"}})):
            same = c.fetch(self.ws, refresh=True)
        self.assertEqual(same["text_reviewed"], 2)
        with mock.patch.object(c, "lark", return_value=response({"document": {"content": "操作建议已变，旧审阅不可沿用"}})):
            changed = c.fetch(self.ws, refresh=True)
        self.assertEqual(changed["text_reviewed"], 0)
        self.assertEqual(changed["pending_distillation"], 2)
        self.assertEqual(len(list((self.ws / "corpus/reviews").rglob("*.json"))), 2)

    def test_review_of_non_applicable_source_retains_reason_without_inventing_knowledge(self):
        entries = self.review_fixture()
        for entry in entries:
            entry.update(decision="not_applicable", knowledge_ids=[], reason="此页只包含班务通知")
        result = self.record_reviews(entries)
        self.assertEqual(result["text_reviewed"], 2)
        self.assertEqual(result["distilled"], 0)

    def test_review_can_reference_existing_modules_and_keeps_legacy_entries(self):
        entries = self.review_fixture()
        modules = ["references/assets/media-cleanup.md", "knowledge/METHODS.md"]
        for module in modules:
            path = self.root / module
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("已提供的可复用方法", encoding="utf-8")
        entries[0].update(knowledge_ids=[], covered_modules=modules)
        result = self.record_reviews(entries)
        self.assertEqual(result["text_reviewed"], 2)
        self.assertEqual(result["distilled"], 0)
        for entry, expected in zip(entries, [modules, []]):
            review = self.state()["documents"][entry["document_id"]]["text_review"]
            self.assertEqual(review["covered_modules"], expected)
            record = read_json(self.ws / "corpus" / review["record"])
            self.assertEqual(record["covered_modules"], expected)
        self.assert_review_records_intact(self.state())

    def test_review_rejects_invalid_module_paths_before_writing_any_records(self):
        entries = self.review_fixture()
        reference = self.root / "references"
        reference.mkdir()
        (reference / "plain.txt").write_text("非 Markdown", encoding="utf-8")
        (reference / "directory.md").mkdir()
        (self.root / "other.md").write_text("不在允许范围", encoding="utf-8")
        bad_paths = [str(self.root / "other.md"), "../other.md", "references/../other.md",
                     "references/missing.md", "references/plain.txt", "references/directory.md",
                     "other.md", "knowledge/OTHER.md", "references\\outside.md"]
        before = self.state()
        for path in bad_paths:
            with self.subTest(path=path):
                entries[-1]["covered_modules"] = [path]
                with self.assertRaises(m.MaintenanceError):
                    self.record_reviews(entries)
                self.assertEqual(self.state(), before)
                self.assertFalse((self.ws / "corpus/reviews").exists())

    def test_review_rejects_module_symlinks_and_linked_parent_directories(self):
        entries = self.review_fixture()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "source.md").write_text("不属于 Skill 模块", encoding="utf-8")
        reference = self.root / "references"
        reference.mkdir()
        (reference / "file.md").symlink_to(outside / "source.md")
        (reference / "linked").symlink_to(outside, target_is_directory=True)
        for path in ["references/file.md", "references/linked/source.md"]:
            with self.subTest(path=path):
                entries[-1]["covered_modules"] = [path]
                with self.assertRaises(m.MaintenanceError):
                    self.record_reviews(entries)
        self.assertEqual(c.status(self.ws)["text_reviewed"], 0)

    def test_review_module_coverage_does_not_claim_integration_or_non_applicability(self):
        entries = self.review_fixture()
        module = self.root / "references/method.md"
        module.parent.mkdir()
        module.write_text("已提供的方法", encoding="utf-8")
        for decision in ["integrated", "not_applicable"]:
            with self.subTest(decision=decision):
                entries[-1].update(decision=decision, knowledge_ids=[],
                                   covered_modules=["references/method.md"])
                with self.assertRaises(m.MaintenanceError):
                    self.record_reviews(entries)
        self.assertEqual(c.status(self.ws)["text_reviewed"], 0)

    def test_review_rejects_non_list_or_non_string_module_references(self):
        entries = self.review_fixture()
        for modules in [None, "references/method.md", [None], [3]]:
            with self.subTest(modules=modules):
                entries[-1]["covered_modules"] = modules
                with self.assertRaises(m.MaintenanceError):
                    self.record_reviews(entries)
        self.assertEqual(c.status(self.ws)["text_reviewed"], 0)

    def assert_review_records_intact(self, state):
        for doc in state["documents"].values():
            review = doc["text_review"]
            record = read_json(self.ws / "corpus" / review["record"])
            self.assertEqual(digest(m.canonical(record)), review["record_hash"])

    def test_rereview_state_commit_failure_keeps_old_records_and_allows_retry(self):
        entries = self.review_fixture()
        self.record_reviews(entries)
        before = self.state()
        for entry in entries:
            entry["reason"] = "复审发现需要修正结论"
        with mock.patch.object(c, "_save", side_effect=OSError("模拟状态提交失败")):
            with self.assertRaises(OSError):
                self.record_reviews(entries)
        self.assertEqual(self.state(), before)
        self.assert_review_records_intact(before)
        self.record_reviews(entries)
        self.assert_review_records_intact(self.state())
        self.assert_review_records_intact(before)
        for ident, doc in before["documents"].items():
            self.assertNotEqual(doc["text_review"]["record"], self.state()["documents"][ident]["text_review"]["record"])

    def test_rereview_second_record_write_failure_preserves_entire_old_batch(self):
        entries = self.review_fixture()
        self.record_reviews(entries)
        before = self.state()
        for entry in entries:
            entry["reason"] = "本批复审修正结论"
        writes = []

        def fail_second_review(path, value):
            if "reviews" in Path(path).parts:
                writes.append(path)
                if len(writes) == 2:
                    raise OSError("模拟第二份审阅写盘失败")
            write_json(path, value)

        with mock.patch.object(c, "write_json", side_effect=fail_second_review):
            with self.assertRaises(OSError):
                self.record_reviews(entries)
        self.assertEqual(len(writes), 2)
        self.assertEqual(self.state(), before)
        self.assert_review_records_intact(before)

    def test_pagination_deep_tree_deduplicates_objects_and_defers_formats(self):
        self.registry_for([{"id": "space", "kind": "wiki_space", "space_id": "123", "enabled": True, "max_depth": 2}])
        def fake(args):
            if "--parent-node-token" in args:
                parent = args[args.index("--parent-node-token") + 1]
                depth = int(parent[1:])
                return response({"nodes": [node("n" + str(depth + 1), children=depth < 6)], "has_more": False})
            if "--page-token" in args:
                return response({"nodes": [node("alias", "objn0"), node("sheet", kind="sheet")], "has_more": False})
            return response({"nodes": [node("n0", children=True)], "has_more": True, "page_token": "p2"})
        with mock.patch.object(c, "lark", side_effect=fake) as cli:
            result = c.inventory(self.registry, self.ws)
        self.assertTrue(result["ok"])
        self.assertEqual(result["discovered"], 9)  # n0..n7 加 sheet；alias 同一对象。
        self.assertEqual(result["needs_format"], 1)
        state = self.state()
        shared = [doc for doc in state["documents"].values() if doc.get("obj_token") == "objn0"][0]
        self.assertEqual(len(shared["locations"]), 2)
        deepest = [doc for doc in state["documents"].values() if doc.get("obj_token") == "objn7"][0]
        self.assertEqual(len(deepest["locations"][0]["path_tokens"]), 8)
        with mock.patch.object(c, "lark") as repeated:
            c.inventory(self.registry, self.ws)
        repeated.assert_not_called()
        self.assertEqual(cli.call_count, 9)

    def test_inventory_resumes_only_failed_page(self):
        self.registry_for([{"id": "space", "kind": "wiki_space", "space_id": "123", "enabled": True}])
        first = response({"nodes": [node("one")], "has_more": True, "page_token": "next"})
        with mock.patch.object(c, "lark", side_effect=[first, m.MaintenanceError("权限失败")]):
            failed = c.inventory(self.registry, self.ws)
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["discovered"], 1)
        with mock.patch.object(c, "lark", return_value=response({"nodes": [node("two")], "has_more": False})) as cli:
            completed = c.inventory(self.registry, self.ws)
        self.assertTrue(completed["ok"])
        self.assertEqual(completed["discovered"], 2)
        self.assertEqual(cli.call_count, 1)
        self.assertIn("--page-token", cli.call_args.args[0])
        self.assertIn("next", cli.call_args.args[0])

    def test_subtree_never_enumerates_the_whole_space(self):
        self.registry_for([{"id": "transcripts", "kind": "wiki_node", "node_token": "parent", "enabled": True}])
        def fake(args):
            if args[:2] == ["wiki", "+node-get"]:
                return response(node("parent", children=True))
            self.assertIn("--parent-node-token", args)
            self.assertEqual(args[args.index("--parent-node-token") + 1], "parent")
            return response({"nodes": [node("child")], "has_more": False})
        with mock.patch.object(c, "lark", side_effect=fake):
            result = c.inventory(self.registry, self.ws)
        self.assertEqual(result["discovered"], 2)

    def test_wiki_doc_resolves_real_format_instead_of_fetching_sheet(self):
        self.registry_for([{"id": "coaching", "kind": "lark_doc", "url": "https://example.feishu.cn/wiki/sheet", "enabled": True}])
        with mock.patch.object(c, "lark", return_value=response(node("sheet", kind="sheet"))):
            result = c.inventory(self.registry, self.ws)
        self.assertEqual(result["format_types"], ["sheet"])
        with mock.patch.object(c, "lark") as cli:
            fetched = c.fetch(self.ws)
        cli.assert_not_called()
        self.assertEqual(fetched["attempted"], 0)

    def test_fetch_failure_is_resumable_and_envelopes_are_preserved(self):
        c.inventory(self.registry_for(self.doc_sources()), self.ws)
        good = response({"document": {"content": "完整正文\n\n下一段", "reference_map": {"image": {"i1": {"token": "private"}}}}})
        with mock.patch.object(c, "lark", side_effect=[good, m.MaintenanceError("读取失败")]):
            first = c.fetch(self.ws)
        self.assertEqual(first["body_ready"], 1)
        self.assertEqual(first["fetch_errors"], 1)
        with mock.patch.object(c, "lark", return_value=response({"document": {"content": "补齐正文"}})) as cli:
            second = c.fetch(self.ws)
        self.assertEqual(cli.call_count, 1)
        self.assertIn("b", cli.call_args.args[0])
        self.assertEqual(second["pending_distillation"], 2)
        self.assertEqual(second["distilled"], 0)
        first_doc = [doc for doc in self.state()["documents"].values() if doc["target"] == "a"][0]
        raw = first_doc["current"]["raw_response"]
        raw_path = self.ws / "corpus" / raw["path"]
        self.assertEqual(read_json(raw_path), good.envelope)
        self.assertEqual(digest(raw_path.read_bytes()), raw["sha256"])
        compatible = read_json(self.ws / "state.json")["sources"][first_doc["maintenance_source_id"]]
        self.assertEqual(compatible["content_hash"], digest("完整正文\n\n下一段"))
        self.assertFalse((self.root / "skills").exists())

    def test_shorter_document_replaces_whole_current_body_and_keeps_history(self):
        local = self.root / "lesson.md"
        local.write_text("新头部\n\n旧尾巴应在更新后消失。", encoding="utf-8")
        self.registry_for([{"id": "local", "kind": "local", "path": "lesson.md", "enabled": True}])
        c.inventory(self.registry, self.ws)
        c.fetch(self.ws)
        local.write_text("只有短正文", encoding="utf-8")
        c.fetch(self.ws, refresh=True)
        doc = next(iter(self.state()["documents"].values()))
        current = read_json(self.ws / "corpus" / doc["current"]["body"])
        normalized = read_json(self.ws / "corpus" / doc["current"]["normalized"])
        self.assertEqual(current["content"], "只有短正文")
        self.assertEqual("".join(p["text"] for p in normalized["paragraphs"]), "只有短正文")
        self.assertEqual(len(doc["versions"]), 1)
        old = read_json(self.ws / "corpus" / doc["versions"][0]["body"])
        self.assertIn("旧尾巴", old["content"])

    def test_missing_content_preserves_current_version_and_raw_response(self):
        c.inventory(self.registry_for(self.doc_sources()[:1]), self.ws)
        with mock.patch.object(c, "lark", return_value=response({"document": {"content": "旧版完整正文"}})):
            c.fetch(self.ws)
        before = next(iter(self.state()["documents"].values()))["current"]
        with mock.patch.object(c, "lark", return_value=response({"document": {"revision_id": 2}})):
            result = c.fetch(self.ws, refresh=True)
        doc = next(iter(self.state()["documents"].values()))
        self.assertFalse(result["ok"])
        self.assertEqual(doc["current"], before)
        self.assertEqual(doc["fetch_status"], "error")
        raw = read_json(self.ws / "corpus" / doc["last_response"]["path"])
        self.assertEqual(raw["data"]["document"], {"revision_id": 2})

    def test_normalization_retry_uses_saved_body_without_repeating_fetch(self):
        c.inventory(self.registry_for(self.doc_sources()[:1]), self.ws)
        with mock.patch.object(c, "lark", return_value=response({"document": {"content": "已读正文"}})), \
                mock.patch.object(c, "normalize", side_effect=ValueError("模拟归一化中断")):
            result = c.fetch(self.ws)
        self.assertEqual(result["body_ready"], 1)
        self.assertEqual(result["normalized"], 0)
        with mock.patch.object(c, "lark") as cli:
            result = c.fetch(self.ws)
        cli.assert_not_called()
        self.assertEqual(result["normalized"], 1)

    def test_explicit_reuse_maintenance_avoids_network_and_matches_object_token(self):
        self.ws.mkdir()
        m.save_source(self.ws, {"id": "already-fetched", "kind": "lark_doc", "obj_token": "a", "token": "wikiAlias"},
                      "已读取的课程全文", response({"document": {"content": "已读取的课程全文"}}).envelope)
        c.inventory(self.registry_for(self.doc_sources()[:1]), self.ws)
        with mock.patch.object(c, "lark") as cli:
            result = c.fetch(self.ws, reuse_maintenance=True)
        cli.assert_not_called()
        self.assertEqual(result["body_ready"], 1)
        doc = next(iter(self.state()["documents"].values()))
        self.assertEqual(doc["reused_maintenance_source"], "already-fetched")

    def test_normalization_keeps_exact_text_offsets_hashes_and_media_counts(self):
        original = '# 章节\r\n\r\n<!-- block-id="blockA" -->\n实际原文。\n\n![图](https://example.org/image.png)\n<video token="a"/>\nhttps://example.feishu.cn/wiki/x'
        normalized = c.normalize(original, {"reference_map": {"image": {"r1": {"token": "img"}}}})
        self.assertFalse(normalized["semantic_distillation"])
        self.assertEqual(normalized["counts"]["media_markup"], 2)
        self.assertEqual(normalized["counts"]["media_references"], 1)
        self.assertEqual(normalized["counts"]["external_links"], 1)
        for paragraph in normalized["paragraphs"]:
            self.assertEqual(original[paragraph["char_start"]:paragraph["char_end"]], paragraph["text"])
            self.assertEqual(digest(paragraph["text"]), paragraph["text_hash"])
            self.assertEqual(digest(original), paragraph["content_hash"])
        self.assertEqual(normalized["paragraphs"][1]["block_ids"], ["blockA"])

    def test_limit_continues_unfinished_documents(self):
        c.inventory(self.registry_for(self.doc_sources()), self.ws)
        with mock.patch.object(c, "lark", return_value=response({"document": {"content": "完整正文"}})) as cli:
            first = c.fetch(self.ws, limit=1)
            second = c.fetch(self.ws, limit=1)
            third = c.fetch(self.ws, limit=1)
        self.assertEqual((first["attempted"], second["attempted"], third["attempted"]), (1, 1, 0))
        self.assertEqual(cli.call_count, 2)

    def test_local_json_envelope_and_non_text_media(self):
        raw_path = self.root / "fetched.json"
        write_json(raw_path, {"ok": True, "data": {"document": {"content": "本地原响应的全文"}}})
        (self.root / "video.mp4").write_bytes(b"media")
        self.registry_for([{"id": "raw", "kind": "local", "path": str(raw_path), "enabled": True},
            {"id": "video", "kind": "local", "path": "video.mp4", "enabled": True}])
        c.inventory(self.registry, self.ws)
        with mock.patch.object(c, "lark") as cli:
            result = c.fetch(self.ws)
        cli.assert_not_called()
        self.assertEqual(result["normalized"], 1)
        self.assertEqual(result["needs_format"], 1)
        self.assertEqual(result["format_types"], ["mp4"])


if __name__ == "__main__":
    unittest.main()
