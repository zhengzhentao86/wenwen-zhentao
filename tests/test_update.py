"""更新器行为与安全边界；仅本地 mock，不启动网络服务。"""
import hashlib
import importlib.util
import io
import json
import socket
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "skills/wenwen-zhentao/scripts/update.py"
SPEC = importlib.util.spec_from_file_location("wenwen_update", SCRIPT)
updater = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(updater)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True).encode()


def package(version, source="https://updates.example.com/latest.json", extra=None):
    files = {
        "SKILL.md": ("version " + version).encode(),
        "version.json": encoded({"schema_version": 1, "skill": "wenwen-zhentao",
                                 "version": version, "update_manifest_url": source}),
        "knowledge/answer.md": ("answer " + version).encode(),
    }
    files.update(extra or {})
    files["release-files.json"] = encoded({"schema_version": 1,
                                          "files": {p: sha(b) for p, b in files.items()}})
    return files


def zipped(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, data in files.items():
            archive.writestr("wenwen-zhentao/" + path, data)
    return buf.getvalue()


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.skill = self.root / "skills" / "wenwen-zhentao"
        self.state = self.root / "state"
        self.write_package(package("0.1.0"))
        self.new_files = package("0.2.0")
        self.archive = zipped(self.new_files)
        self.manifest = {"schema_version": 1, "skill": "wenwen-zhentao", "version": "0.2.0",
                         "archive_url": "https://updates.example.com/0.2.0.zip",
                         "sha256": sha(self.archive), "min_updater_version": 1,
                         "files": {p: sha(b) for p, b in self.new_files.items()}}
        self.requests = []

    def tearDown(self):
        self.temp.cleanup()

    def write_package(self, files):
        self.skill.mkdir(parents=True, exist_ok=True)
        for path, data in files.items():
            dest = self.skill / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)

    def fetch(self, url, max_bytes, origin):
        self.requests.append(url)
        data = encoded(self.manifest) if url.endswith(".json") else self.archive
        if len(data) > max_bytes:
            raise updater.UpdateError("download_too_large", "too large")
        return data

    def run_check(self, apply=False, fetch=None, **kwargs):
        return updater.check(self.skill, self.state, apply=apply, fetch=fetch or self.fetch, **kwargs)

    def version(self):
        return json.loads((self.skill / "version.json").read_text())["version"]

    def assert_old(self):
        self.assertEqual(self.version(), "0.1.0")
        self.assertEqual((self.skill / "knowledge/answer.md").read_text(), "answer 0.1.0")

    def test_not_configured_does_not_contact_any_server(self):
        self.write_package(package("0.1.0", None))
        result = self.run_check(apply=True)
        self.assertEqual(result["status"], "not_configured")
        self.assertEqual(self.requests, [])

    def test_check_only_does_not_install(self):
        result = self.run_check()
        self.assertEqual(result["status"], "update_available")
        self.assertEqual(len(self.requests), 1)
        self.assert_old()

    def test_apply_and_rollback_preserve_user_state(self):
        self.state.mkdir()
        note = self.state / "questions.json"
        note.write_text("private user material")
        result = self.run_check(apply=True)
        self.assertEqual(result["status"], "installed")
        self.assertEqual(result["installed_version"], "0.2.0")
        self.assertIsNone(result["loaded_version"])
        self.assertTrue(result["reload_required"])
        self.assertEqual(note.read_text(), "private user material")
        result = updater.rollback(self.skill, self.state)
        self.assertEqual(result["status"], "rolled_back")
        self.assert_old()
        self.assertEqual(note.read_text(), "private user material")

    def test_offline_preserves_installed_package(self):
        def offline(*args, **kwargs):
            raise OSError("network unavailable")
        result = self.run_check(apply=True, fetch=offline)
        self.assertEqual(result["status"], "offline_or_unreachable")
        self.assert_old()

    def test_local_modified_added_and_deleted_files_are_preserved(self):
        for kind in ("modified", "added", "deleted"):
            with self.subTest(kind=kind):
                import shutil
                shutil.rmtree(self.skill)
                self.write_package(package("0.1.0"))
                target = self.skill / "SKILL.md"
                if kind == "modified":
                    target.write_text("my custom instructions")
                elif kind == "added":
                    (self.skill / "my-notes.md").write_text("my local notes")
                else:
                    target.unlink()
                result = self.run_check(apply=True)
                self.assertEqual(result["status"], "local_changes")
                self.assertEqual(self.version(), "0.1.0")

    def test_missing_baseline_does_not_overwrite_installation(self):
        (self.skill / "release-files.json").unlink()
        self.assertEqual(self.run_check(apply=True)["status"], "local_changes_unverified")
        self.assert_old()

    def test_cross_origin_manifest_override_rejected(self):
        result = self.run_check(apply=True, manifest_url="https://evil.example/latest.json")
        self.assertEqual(result["status"], "untrusted_source")
        self.assertEqual(self.requests, [])

    def test_non_https_and_private_network_urls_rejected(self):
        for url in ("http://updates.example.com/a", "file:///tmp/a", "https://127.0.0.1/a",
                    "https://[::1]/a", "https://localhost/a", "https://user:pass@example.com/a"):
            with self.subTest(url=url):
                self.write_package(package("0.1.0", url))
                self.assertEqual(self.run_check(apply=True)["status"], "untrusted_source")
        self.assertEqual(self.requests, [])

    def test_cross_origin_archive_rejected_before_download(self):
        self.manifest["archive_url"] = "https://evil.example/package.zip"
        self.assertEqual(self.run_check(apply=True)["status"], "untrusted_source")
        self.assertEqual(len(self.requests), 1)
        self.assert_old()

    def test_incompatible_updater_and_downgrade_do_not_download(self):
        self.manifest["min_updater_version"] = 999
        self.assertEqual(self.run_check(apply=True)["status"], "incompatible")
        self.manifest["min_updater_version"] = 1
        self.manifest["version"] = "0.0.9"
        self.assertEqual(self.run_check(apply=True)["status"], "remote_older")
        self.assertTrue(all(url.endswith(".json") for url in self.requests))
        self.assert_old()

    def test_corrupt_archive_hash_rejected(self):
        self.archive += b"tampered"
        self.assertEqual(self.run_check(apply=True)["status"], "integrity_error")
        self.assert_old()

    def malicious_archive(self, path, data=b"evil", symlink=False):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as archive:
            for key, value in self.new_files.items():
                archive.writestr("wenwen-zhentao/" + key, value)
            info = zipfile.ZipInfo(path)
            if symlink:
                info.create_system = 3
                info.external_attr = (0o120777 << 16)
            archive.writestr(info, data)
        self.archive = buf.getvalue()
        self.manifest["sha256"] = sha(self.archive)

    def test_zip_paths_and_symlinks_rejected(self):
        for path, symlink in (("wenwen-zhentao/../../escape", False),
                              ("/absolute", False), ("other-root/file", False),
                              ("wenwen-zhentao/link", True),
                              ("wenwen-zhentao/dir\\file", False)):
            with self.subTest(path=path):
                self.malicious_archive(path, symlink=symlink)
                result = self.run_check(apply=True)
                self.assertIn(result["status"], ("unsafe_archive", "integrity_error"))
                self.assert_old()
        self.assertFalse((self.root / "escape").exists())

    def test_extra_and_missing_archive_files_rejected(self):
        for change in ("extra", "missing"):
            with self.subTest(change=change):
                files = dict(self.new_files)
                if change == "extra":
                    files["undeclared.md"] = b"secret"
                else:
                    del files["knowledge/answer.md"]
                self.archive = zipped(files)
                self.manifest["sha256"] = sha(self.archive)
                self.assertEqual(self.run_check(apply=True)["status"], "integrity_error")
                self.assert_old()

    def test_archive_uncompressed_limit(self):
        initial_size = sum(p.stat().st_size for p in self.skill.rglob("*") if p.is_file())
        self.new_files = package("0.2.0", extra={"large.md": b"x" * initial_size * 3})
        self.archive = zipped(self.new_files)
        self.manifest["sha256"] = sha(self.archive)
        self.manifest["files"] = {p: sha(b) for p, b in self.new_files.items()}
        with mock.patch.object(updater, "MAX_EXPANDED_BYTES", initial_size * 2):
            self.assertEqual(self.run_check(apply=True)["status"], "unsafe_archive")
        self.assert_old()

    def test_downloaded_package_cannot_change_update_origin_or_version(self):
        for source, version in (("https://evil.example/latest.json", "0.2.0"),
                                ("https://updates.example.com/latest.json", "9.9.9")):
            with self.subTest(source=source, version=version):
                self.new_files = package(version, source)
                self.archive = zipped(self.new_files)
                self.manifest["sha256"] = sha(self.archive)
                self.manifest["files"] = {p: sha(b) for p, b in self.new_files.items()}
                self.assertEqual(self.run_check(apply=True)["status"], "invalid_package")
                self.assert_old()

    def test_rename_failure_restores_old_installation(self):
        original = updater.os.replace
        def fail_new_package(src, dst):
            if ".stage-" in str(src) and Path(dst) == self.skill:
                raise PermissionError("denied")
            return original(src, dst)
        with mock.patch.object(updater.os, "replace", side_effect=fail_new_package):
            self.assertEqual(self.run_check(apply=True)["status"], "permission_denied")
        self.assert_old()

    def test_state_inside_skill_and_symlink_install_rejected(self):
        result = updater.check(self.skill, self.skill / "state", apply=True, fetch=self.fetch)
        self.assertEqual(result["status"], "invalid_location")
        linked = self.root / "linked"
        linked.symlink_to(self.skill, target_is_directory=True)
        result = updater.check(linked, self.state, apply=True, fetch=self.fetch)
        self.assertEqual(result["status"], "invalid_location")

    def test_concurrent_update_lock(self):
        with updater.install_lock(self.skill):
            self.assertEqual(self.run_check(apply=True)["status"], "busy")
        self.assert_old()

    def test_rollback_refuses_modified_current_install(self):
        self.run_check(apply=True)
        (self.skill / "SKILL.md").write_text("local edits")
        self.assertEqual(updater.rollback(self.skill, self.state)["status"], "local_changes")
        self.assertEqual(self.version(), "0.2.0")

    def test_private_dns_and_cross_origin_redirect_blocked(self):
        with mock.patch.object(updater.socket, "getaddrinfo", return_value=[
                (2, 1, 6, "", ("10.0.0.1", 443))]):
            with self.assertRaises(updater.UpdateError):
                updater.public_addresses("updates.example.com", 443)
        with self.assertRaises(updater.UpdateError):
            updater.valid_url("https://evil.example/a", ("updates.example.com", 443))

    def test_interrupted_swap_restores_original_on_next_check(self):
        original = updater.os.replace
        def interrupt_new_package(src, dst):
            if ".stage-" in str(src) and Path(dst) == self.skill:
                raise KeyboardInterrupt("simulated process interruption")
            return original(src, dst)
        with mock.patch.object(updater.os, "replace", side_effect=interrupt_new_package):
            with self.assertRaises(KeyboardInterrupt):
                self.run_check(apply=True)
        self.assertFalse(self.skill.exists())
        self.assertTrue((self.state / "transaction.json").exists())
        self.assertEqual(self.run_check()["status"], "update_available")
        self.assert_old()
        self.assertFalse((self.state / "transaction.json").exists())

    def test_interrupted_receipt_write_restores_original(self):
        original = updater.atomic_json
        def interrupt_receipt(path, value):
            if path.name == "installation.json":
                raise KeyboardInterrupt("simulated process interruption")
            return original(path, value)
        with mock.patch.object(updater, "atomic_json", side_effect=interrupt_receipt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_check(apply=True)
        self.assertEqual(self.version(), "0.2.0")
        self.assertTrue((self.state / "transaction.json").exists())
        self.assertEqual(self.run_check()["status"], "update_available")
        self.assert_old()

    def test_receipt_write_failure_restores_original(self):
        original = updater.atomic_json
        def deny_receipt(path, value):
            if path.name == "installation.json":
                raise PermissionError("state denied")
            return original(path, value)
        with mock.patch.object(updater, "atomic_json", side_effect=deny_receipt):
            self.assertEqual(self.run_check(apply=True)["status"], "permission_denied")
        self.assert_old()

    def test_internal_package_baseline_must_match(self):
        self.new_files["release-files.json"] = encoded({"schema_version": 1, "files": {"SKILL.md": "0" * 64}})
        self.archive = zipped(self.new_files)
        self.manifest["sha256"] = sha(self.archive)
        self.manifest["files"] = {p: sha(b) for p, b in self.new_files.items()}
        self.assertEqual(self.run_check(apply=True)["status"], "invalid_package")
        self.assert_old()

    def test_duplicate_zip_entry_rejected(self):
        import warnings
        buf = io.BytesIO(self.archive)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(buf, "a") as archive:
                archive.writestr("wenwen-zhentao/SKILL.md", b"shadowed instructions")
        self.archive = buf.getvalue()
        self.manifest["sha256"] = sha(self.archive)
        self.assertEqual(self.run_check(apply=True)["status"], "unsafe_archive")
        self.assert_old()

    def test_network_redirect_checked_before_second_connection(self):
        response = mock.Mock(status=302)
        response.getheader.side_effect = lambda key, *args: {
            "Location": "https://evil.example/package.zip"}.get(key)
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with mock.patch.object(updater, "public_addresses", return_value=["8.8.8.8"]), \
                mock.patch.object(updater, "PinnedHTTPSConnection", return_value=connection) as factory:
            with self.assertRaises(updater.UpdateError) as raised:
                updater.fetch_https("https://updates.example.com/latest.json", 100,
                                    ("updates.example.com", 443))
        self.assertEqual(raised.exception.status, "untrusted_source")
        self.assertEqual(factory.call_count, 1)
        connection.close.assert_called_once()

    def test_dns_resolution_cannot_mix_public_and_private_addresses(self):
        with mock.patch.object(updater.socket, "getaddrinfo", return_value=[
                (socket.AF_INET, 1, 6, "", ("8.8.8.8", 443)),
                (socket.AF_INET, 1, 6, "", ("192.168.1.1", 443))]):
            with self.assertRaises(updater.UpdateError):
                updater.public_addresses("updates.example.com", 443)

    def test_not_configured_cli_exit_is_success_and_json_only(self):
        self.write_package(package("0.1.0", None))
        with mock.patch("sys.stdout", new_callable=io.StringIO) as output:
            status = updater.main(["--skill-dir", str(self.skill), "--state-dir", str(self.state), "check", "--apply"])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "not_configured")


if __name__ == "__main__":
    unittest.main()
