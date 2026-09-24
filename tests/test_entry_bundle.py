import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from build_entry_bundle import build_entry_bundle, verify_bundle
from test_build import fixture
from wwzt_core import digest, fingerprint, write_json


class EntryBundleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.core = self.base / "wenwen-zhentao"
        fixture(self.core)
        self.entry = self.base / "zt"
        (self.entry / "agents").mkdir(parents=True)
        (self.entry / "SKILL.md").write_text("---\nname: zt\ndescription: 测试短入口\n---\n读取同级核心入口。")
        (self.entry / "agents/openai.yaml").write_text('interface:\n  display_name: "zt"\n')
        self.out = self.base / "out"

    def tearDown(self):
        self.tmp.cleanup()

    def approve_release(self):
        catalog = json.loads((self.core / "knowledge/catalog.json").read_text())
        catalog["items"][0]["public_status"] = "approved"
        write_json(self.core / "knowledge/catalog.json", catalog)
        (self.core / "LICENSE.txt").write_text("Synthetic fixture license.")
        approval = self.base / "approval.json"
        write_json(approval, {"package_fingerprint": fingerprint(self.core)[0],
                              "entry_fingerprint": fingerprint(self.entry)[0],
                              "decision": "approve_public_release", "approved_by": "synthetic_test",
                              "approved_at": "2026-09-24"})
        return approval

    def test_optional_entry_license_is_included_in_preview_and_verified(self):
        (self.entry / "LICENSE.txt").write_text("Synthetic fixture license.")
        result = build_entry_bundle(self.core, self.entry, self.out)
        manifest = json.loads(Path(result["manifest"]).read_text())
        self.assertIn("zt/LICENSE.txt", manifest["files"])
        self.assertTrue(verify_bundle(result["archive"], manifest)["ok"])

    def test_stable_bundle_preserves_approved_core_release_and_latest(self):
        (self.entry / "LICENSE.txt").write_text("Synthetic fixture license.")
        approval = self.approve_release()
        result = build_entry_bundle(self.core, self.entry, self.out, release=True,
                                    approval=approval, base_url="https://example.com/releases")
        self.assertEqual(Path(result["archive"]).name, "zt-0.1.0.zip")
        self.assertEqual(Path(result["manifest"]).name, "zt-0.1.0-manifest.json")
        self.assertEqual(Path(result["core_archive"]).name, "wenwen-zhentao-0.1.0.zip")
        self.assertEqual(result["channel"], "stable")
        manifest = json.loads(Path(result["manifest"]).read_text())
        self.assertEqual(manifest["channel"], "stable")
        self.assertTrue(verify_bundle(result["archive"], manifest)["ok"])
        latest = json.loads((self.out / "latest.json").read_text())
        self.assertEqual(latest["archive_url"], "https://example.com/releases/wenwen-zhentao-0.1.0.zip")
        self.assertEqual(latest["sha256"], digest(Path(result["core_archive"]).read_bytes()))
        with zipfile.ZipFile(result["archive"]) as bundle, zipfile.ZipFile(result["core_archive"]) as core:
            self.assertIn("zt/LICENSE.txt", bundle.namelist())
            for name in core.namelist():
                self.assertEqual(core.read(name), bundle.read(name))
            version = json.loads(bundle.read("wenwen-zhentao/version.json"))
            self.assertEqual(version["channel"], "stable")
            self.assertEqual(version["update_manifest_url"], "https://example.com/releases/latest.json")
        self.assertFalse((self.out / "preview-manifest.json").exists())

    def test_stable_does_not_bypass_core_public_approval(self):
        approval = self.approve_release()
        for provided, change in [(None, False), (approval, True)]:
            with self.subTest(approval=provided, changed_core=change):
                if change:
                    (self.core / "SKILL.md").write_text("审批后改变的核心")
                with self.assertRaisesRegex(ValueError, "审批"):
                    build_entry_bundle(self.core, self.entry, self.out, True, provided,
                                       "https://example.com/releases")
                self.assertEqual(list(self.out.iterdir()), [])

    def test_stable_entry_requires_matching_fingerprint_in_approval(self):
        approval = self.approve_release()
        record = json.loads(approval.read_text())
        for value in [None, "0" * 64]:
            with self.subTest(entry_fingerprint=value):
                record["entry_fingerprint"] = value
                write_json(approval, record)
                with self.assertRaisesRegex(ValueError, "入口.*审批|审批.*入口"):
                    build_entry_bundle(self.core, self.entry, self.out, True, approval,
                                       "https://example.com/releases")
                self.assertEqual(list(self.out.iterdir()), [])

    def test_stable_entry_change_during_build_keeps_previous_release(self):
        (self.entry / "LICENSE.txt").write_text("Synthetic fixture license.")
        approval = self.approve_release()
        build_entry_bundle(self.core, self.entry, self.out, True, approval, "https://example.com/releases")
        before = {p.name: p.read_bytes() for p in self.out.iterdir()}
        original = shutil.copy2

        def changing_copy(source, destination):
            result = original(source, destination)
            if Path(source).resolve() == (self.entry / "SKILL.md").resolve():
                (self.entry / "LICENSE.txt").write_text("构建时被改写的许可证")
            return result

        with mock.patch("build_entry_bundle.shutil.copy2", side_effect=changing_copy):
            with self.assertRaisesRegex(ValueError, "变化"):
                build_entry_bundle(self.core, self.entry, self.out, True, approval,
                                   "https://example.com/releases")
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.out.iterdir()})

    def test_verification_rejects_preview_relabelled_as_stable(self):
        result = build_entry_bundle(self.core, self.entry, self.out)
        manifest = json.loads(Path(result["manifest"]).read_text())
        manifest["channel"] = "stable"
        with self.assertRaisesRegex(ValueError, "channel|渠道"):
            verify_bundle(result["archive"], manifest)

    def test_release_cli_accepts_approval_and_base_url(self):
        approval = self.approve_release()
        command = [sys.executable, str(Path(__file__).resolve().parents[1] / "tools/build_entry_bundle.py"),
                   "--skill-dir", str(self.core), "--entry-dir", str(self.entry), "--out", str(self.out),
                   "--release", "--approval", str(approval), "--base-url", "https://example.com/releases"]
        completed = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        result = json.loads(completed.stdout)
        self.assertEqual(result["channel"], "stable")
        self.assertTrue(verify_bundle(result["archive"], result["manifest"])["ok"])

    def test_parallel_layout_hashes_and_core_update_artifact(self):
        result = build_entry_bundle(self.core, self.entry, self.out)
        manifest = json.loads(Path(result["manifest"]).read_text())
        self.assertTrue(verify_bundle(result["archive"], manifest)["ok"])
        with zipfile.ZipFile(result["archive"]) as bundle:
            self.assertEqual({p.split("/")[0] for p in bundle.namelist()}, {"zt", "wenwen-zhentao"})
            self.assertIn("zt/SKILL.md", bundle.namelist())
            self.assertIn("wenwen-zhentao/SKILL.md", bundle.namelist())
            self.assertEqual(set(bundle.namelist()), set(manifest["files"]))
            with zipfile.ZipFile(result["core_archive"]) as core:
                for name in core.namelist():
                    self.assertEqual(core.read(name), bundle.read(name))
        core_manifest = json.loads((self.out / "preview-manifest.json").read_text())
        self.assertEqual(core_manifest["sha256"], digest(Path(result["core_archive"]).read_bytes()))
        self.assertEqual(json.loads((self.out / "build-result.json").read_text())["archive"], result["core_archive"])
        self.assertEqual(result["channel"], "preview")

    def test_deterministic(self):
        first = build_entry_bundle(self.core, self.entry, self.out)
        second = build_entry_bundle(self.core, self.entry, self.base / "second")
        self.assertEqual(first["sha256"], second["sha256"])

    def test_unknown_files_and_symlinks_are_rejected(self):
        for root, name in ((self.entry, "notes.md"), (self.core, "secret.txt")):
            with self.subTest(root=root):
                target = root / name
                target.write_text("不能分发")
                with self.assertRaisesRegex(ValueError, "白名单"):
                    build_entry_bundle(self.core, self.entry, self.out)
                target.unlink()
        (self.entry / "extra").symlink_to(self.core)
        with self.assertRaisesRegex(ValueError, "符号链接"):
            build_entry_bundle(self.core, self.entry, self.out)

    def test_root_symlink_is_rejected(self):
        alias = self.base / "alias"
        alias.symlink_to(self.entry, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "符号链接"):
            build_entry_bundle(self.core, alias, self.out)

    def test_incomplete_entry_and_private_information_are_rejected(self):
        interface = self.entry / "agents/openai.yaml"
        original = interface.read_bytes()
        interface.unlink()
        with self.assertRaisesRegex(ValueError, "入口必须包含"):
            build_entry_bundle(self.core, self.entry, self.out)
        interface.write_bytes(original)
        (self.entry / "SKILL.md").write_text("https://example.feishu.cn/docx/private")
        with self.assertRaisesRegex(ValueError, "飞书私有入口"):
            build_entry_bundle(self.core, self.entry, self.out)

    def test_input_change_preserves_previous_archive(self):
        previous = build_entry_bundle(self.core, self.entry, self.out)
        snapshots = {p.name: p.read_bytes() for p in self.out.iterdir()}
        original_copy = shutil.copy2

        def changing_copy(src, dst):
            copied = original_copy(src, dst)
            if Path(src).resolve() == (self.entry / "SKILL.md").resolve():
                (self.entry / "agents/openai.yaml").write_text("另一任务的编辑")
            return copied

        with mock.patch("build_entry_bundle.shutil.copy2", side_effect=changing_copy):
            with self.assertRaisesRegex(ValueError, "变化"):
                build_entry_bundle(self.core, self.entry, self.out)
        self.assertEqual(snapshots, {p.name: p.read_bytes() for p in self.out.iterdir()})
        self.assertTrue(verify_bundle(previous["archive"], previous["manifest"])["ok"])

    def test_core_change_after_inner_build_is_rejected(self):
        from build import build
        def changed_core(*args, **kwargs):
            result = build(*args, **kwargs)
            (self.core / "SKILL.md").write_text("构建期间修改核心入口")
            return result
        with mock.patch("build_entry_bundle.build", side_effect=changed_core):
            with self.assertRaisesRegex(ValueError, "源文件发生变化"):
                build_entry_bundle(self.core, self.entry, self.out)
        self.assertEqual(list(self.out.iterdir()), [])

    def test_failed_zip_check_preserves_previous_artifacts(self):
        build_entry_bundle(self.core, self.entry, self.out)
        snapshots = {p.name: p.read_bytes() for p in self.out.iterdir()}
        with mock.patch("build_entry_bundle.zipfile.ZipFile.testzip", return_value="broken"):
            with self.assertRaisesRegex(ValueError, "完整性"):
                build_entry_bundle(self.core, self.entry, self.out)
        self.assertEqual(snapshots, {p.name: p.read_bytes() for p in self.out.iterdir()})

    def test_independent_verification_detects_tampering(self):
        result = build_entry_bundle(self.core, self.entry, self.out)
        manifest = json.loads(Path(result["manifest"]).read_text())
        manifest["files"]["zt/SKILL.md"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "文件哈希"):
            verify_bundle(result["archive"], manifest)
        Path(result["archive"]).write_bytes(b"broken")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            verify_bundle(result["archive"], result["manifest"])

    def test_nested_output_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "输出目录"):
            build_entry_bundle(self.core, self.entry, self.entry / "dist")


if __name__ == "__main__":
    unittest.main()
