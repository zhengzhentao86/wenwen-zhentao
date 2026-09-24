"""真实构建器→ZIP→更新器合同联调；只在临时目录运行，HTTPS 传输用 mock。"""
import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from build import build
from wwzt_core import digest, fingerprint, read_json, write_json

MODULE = importlib.util.spec_from_file_location(
    "release_integration_updater", ROOT / "skills/wenwen-zhentao/scripts/update.py")
updater = importlib.util.module_from_spec(MODULE)
MODULE.loader.exec_module(updater)


class ReleaseIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "knowledge").mkdir()
        (self.source / "SKILL.md").write_text(
            "---\nname: wenwen-zhentao\ndescription: 仅用于离线集成测试的知识包\n---\n读取随包知识回答。", encoding="utf-8")
        (self.source / "LICENSE.txt").write_text("Synthetic integration fixture only.", encoding="utf-8")
        self.card = "knowledge/WWZT-001.md"
        item = {"id": "WWZT-001", "path": self.card, "question": "如何查看测试结果", "aliases": [],
                "category": "tools", "summary": "使用合成数据验证发行工具。", "source_label": "合成测试材料",
                "source_date": None, "verified_at": "2026-09-23", "evidence_type": "synthetic_fixture",
                "public_status": "approved", "applicable_when": "仅本测试", "limitations": "不是学员实测结果", "tags": []}
        write_json(self.source / "knowledge/catalog.json", {"schema_version": 1, "items": [item]})
        self.set_version("0.1.0")
        self.installed = self.root / "installed" / "wenwen-zhentao"
        self.state = self.root / "state"
        self.base_url = "https://updates.example.com/releases"

    def set_version(self, version):
        write_json(self.source / "version.json", {"schema_version": 1, "skill": "wenwen-zhentao",
                   "version": version, "channel": "preview", "update_manifest_url": None})
        (self.source / self.card).write_text(
            "# 合成测试答案 " + version + "\n\n" + "这是用于构建器与更新器离线联调的合成知识，不代表真实课程或真实业务效果。" * 2,
            encoding="utf-8")

    def release(self, version):
        self.set_version(version)
        approval = self.root / ("fixture-approval-" + version + ".json")
        write_json(approval, {"package_fingerprint": fingerprint(self.source)[0],
                   "decision": "approve_public_release", "approved_by": "synthetic_test_fixture",
                   "approved_at": "2026-09-23"})
        output = self.root / ("release-" + version)
        result = build(self.source, output, release=True, approval=approval, base_url=self.base_url)
        return result, read_json(output / "latest.json")

    def install_built_archive(self, result):
        self.installed.parent.mkdir(exist_ok=True)
        # 仅解压本测试刚由实际构建器生成且检查过清单的可信产物。
        with zipfile.ZipFile(result["archive"]) as archive:
            self.assertTrue(all(name.startswith("wenwen-zhentao/") and ".." not in Path(name).parts
                                for name in archive.namelist()))
            archive.extractall(self.installed.parent)
        updater.verify_local(self.installed)

    def test_actual_preview_bundle_passes_updater_baseline_and_stays_offline(self):
        output = self.root / "actual-preview"
        artifact = build(ROOT / "skills/wenwen-zhentao", output)
        manifest = read_json(output / "preview-manifest.json")
        self.assertEqual(1, manifest["schema_version"])
        self.assertIsNone(manifest["archive_url"])
        self.assertEqual(digest(Path(artifact["archive"]).read_bytes()), manifest["sha256"])
        with zipfile.ZipFile(artifact["archive"]) as archive:
            self.assertEqual(set(archive.namelist()), {"wenwen-zhentao/" + path for path in manifest["files"]})
            for path, expected in manifest["files"].items():
                self.assertEqual(expected, digest(archive.read("wenwen-zhentao/" + path)))
        self.install_built_archive(artifact)
        transport = mock.Mock(side_effect=AssertionError("preview must not contact a server"))
        status = updater.check(self.installed, self.state, apply=True, fetch=transport)
        self.assertEqual("not_configured", status["status"])
        transport.assert_not_called()
        # 基线确实保护包内文件；预览未配发布地址，因此不声称已经完成公网升级。
        (self.installed / "SKILL.md").write_text("local user changes", encoding="utf-8")
        with self.assertRaises(updater.UpdateError) as caught:
            updater.verify_local(self.installed)
        self.assertEqual("local_changes", caught.exception.status)

    def test_extracted_preview_can_be_rebuilt_with_existing_baseline(self):
        first = build(self.source, self.root / "first-preview")
        self.install_built_archive(first)
        second = build(self.installed, self.root / "rebuilt-preview")
        self.assertEqual(first["sha256"], second["sha256"])

    def test_existing_baseline_is_still_covered_by_source_race_check(self):
        first = build(self.source, self.root / "first-preview")
        self.install_built_archive(first)
        original = shutil.copy2
        changed = False
        def changing_baseline_after_copy(source, destination):
            nonlocal changed
            copied = original(source, destination)
            if not changed:
                changed = True
                (self.installed / "release-files.json").write_text('{"changed_during_build": true}', encoding="utf-8")
            return copied
        with mock.patch("build.shutil.copy2", side_effect=changing_baseline_after_copy):
            with self.assertRaisesRegex(ValueError, "复制期间"):
                build(self.installed, self.root / "raced-preview")
        self.assertFalse(list((self.root / "raced-preview").glob("*.zip")))

    def test_real_release_packages_upgrade_and_rollback_without_state_loss(self):
        old, _ = self.release("0.1.0")
        self.install_built_archive(old)
        latest, manifest = self.release("0.2.0")
        self.state.mkdir()
        note = self.state / "user-notes.txt"
        note.write_text("仅保存在本机的学习记录", encoding="utf-8")
        requests = []
        def transport(url, max_bytes, origin):
            requests.append(url)
            self.assertEqual(("updates.example.com", 443), origin)
            payload = json.dumps(manifest).encode() if url.endswith("latest.json") else Path(latest["archive"]).read_bytes()
            self.assertLessEqual(len(payload), max_bytes)
            return payload
        result = updater.check(self.installed, self.state, apply=True, fetch=transport)
        self.assertEqual("installed", result["status"], result)
        self.assertEqual("0.2.0", result["installed_version"])
        self.assertTrue(result["reload_required"])
        self.assertIsNone(result["loaded_version"])
        self.assertIn("0.2.0", (self.installed / self.card).read_text())
        self.assertEqual([self.base_url + "/latest.json", manifest["archive_url"]], requests)
        updater.verify_local(self.installed)
        rollback = updater.rollback(self.installed, self.state)
        self.assertEqual("rolled_back", rollback["status"], rollback)
        self.assertEqual("0.1.0", rollback["installed_version"])
        self.assertIn("0.1.0", (self.installed / self.card).read_text())
        self.assertEqual("仅保存在本机的学习记录", note.read_text())

    def test_tampered_built_release_is_rejected_and_old_content_survives(self):
        old, _ = self.release("0.1.0")
        self.install_built_archive(old)
        latest, manifest = self.release("0.2.0")
        def transport(url, max_bytes, origin):
            return json.dumps(manifest).encode() if url.endswith("latest.json") else Path(latest["archive"]).read_bytes() + b"tampered"
        result = updater.check(self.installed, self.state, apply=True, fetch=transport)
        self.assertEqual("integrity_error", result["status"], result)
        self.assertEqual("0.1.0", updater.metadata(self.installed)["version"])
        self.assertIn("0.1.0", (self.installed / self.card).read_text())
        updater.verify_local(self.installed)


if __name__ == "__main__":
    unittest.main()
