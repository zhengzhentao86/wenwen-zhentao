import importlib.util
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
from build import build
from wwzt_core import digest, fingerprint, write_json


def fixture(root, status="pending_owner_review"):
    root.mkdir()
    (root / "knowledge").mkdir()
    (root / "SKILL.md").write_text("---\nname: wenwen-zhentao\ndescription: 测试技能\n---\n内容")
    write_json(root / "version.json", {"schema_version": 1, "skill": "wenwen-zhentao", "version": "0.1.0", "update_manifest_url": None})
    item = {"id": "WWZT-001", "question": "故事板个别镜头错误", "aliases": [], "category": "visual",
            "path": "knowledge/WWZT-001.md", "summary": "单独修正", "source_label": "测试资料",
            "source_date": None, "verified_at": "2026-09-23", "evidence_type": "AI整理",
            "public_status": status, "applicable_when": "局部问题", "limitations": "先检查参考图", "tags": []}
    write_json(root / "knowledge/catalog.json", {"schema_version": 1, "items": [item]})
    (root / item["path"]).write_text("# 故事板局部修正\n\n只修复存在问题的镜头，逐个验证参考图、输入条件和生成结果。其他镜头保持原样以便对照。", encoding="utf-8")


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / "skill"
        fixture(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_preview_is_deterministic_and_has_exact_hashes(self):
        a = build(self.root, self.base / "a")
        b = build(self.root, self.base / "b")
        self.assertEqual(a["sha256"], b["sha256"])
        manifest = json.loads((self.base / "a/preview-manifest.json").read_text())
        with zipfile.ZipFile(a["archive"]) as z:
            self.assertEqual(set(z.namelist()), {"wenwen-zhentao/" + p for p in manifest["files"]})
            for p, h in manifest["files"].items():
                self.assertEqual(digest(z.read("wenwen-zhentao/" + p)), h)
            baseline = json.loads(z.read("wenwen-zhentao/release-files.json"))
            self.assertEqual(set(baseline["files"]), set(manifest["files"]) - {"release-files.json"})

    def test_unapproved_release_rejected(self):
        with self.assertRaisesRegex(ValueError, "未获公开确认"):
            build(self.root, self.base / "out", release=True)

    def test_extra_private_file_rejected(self):
        (self.root / "raw-notes.txt").write_text("私人记录")
        with self.assertRaisesRegex(ValueError, "白名单"):
            build(self.root, self.base / "out")

    def test_private_lark_link_rejected(self):
        (self.root / "knowledge/WWZT-001.md").write_text("参考资料 https://example.feishu.cn/wiki/private 不能随公共版本分发。" * 2)
        with self.assertRaisesRegex(ValueError, "飞书私有入口"):
            build(self.root, self.base / "out")

    def test_symlink_rejected(self):
        (self.root / "secret").symlink_to(self.base / "outside")
        with self.assertRaisesRegex(ValueError, "符号链接"):
            build(self.root, self.base / "out")

    def test_stale_public_approval_rejected(self):
        cat = json.loads((self.root / "knowledge/catalog.json").read_text())
        cat["items"][0]["public_status"] = "approved"
        write_json(self.root / "knowledge/catalog.json", cat)
        fp, _ = fingerprint(self.root)
        approval = self.base / "approval.json"
        write_json(approval, {"package_fingerprint": fp, "decision": "approve_public_release", "approved_by": "测试用户", "approved_at": "2026-09-23"})
        (self.root / "SKILL.md").write_text("内容后来被修改")
        with self.assertRaisesRegex(ValueError, "不匹配"):
            build(self.root, self.base / "out", True, approval, "https://example.com/releases")

    def test_source_change_during_copy_does_not_package_unreviewed_content(self):
        import shutil
        original = shutil.copy2
        def copying(src, dst):
            if Path(src).name == "WWZT-001.md":
                Path(src).write_text("审批后另一任务改写的未审阅内容" * 8)
            return original(src, dst)
        with mock.patch("build.shutil.copy2", side_effect=copying):
            with self.assertRaisesRegex(ValueError, "复制期间"):
                build(self.root, self.base / "out")
        self.assertFalse(list((self.base / "out").glob("*.zip")))


if __name__ == "__main__":
    unittest.main()
