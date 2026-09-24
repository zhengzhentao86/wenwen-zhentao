#!/usr/bin/env python3
"""构建并排安装的 zt 入口与核心包；正式发行复用核心审批，保留独立升级包，不上传。"""
import argparse
import json
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

from build import ALLOWED_DIRS, ALLOWED_ROOT, ALLOWED_SUFFIX, build
from wwzt_core import SKILL, digest, fingerprint, package_privacy_issues, read_json, safe_relative, write_json

ENTRY = "zt"
ENTRY_FILES = {"SKILL.md", "agents/openai.yaml"}
ENTRY_OPTIONAL_FILES = {"LICENSE.txt"}


def _check_source(root, entry=False):
    if root.is_symlink() or not root.is_dir():
        raise ValueError("源目录不存在或是符号链接")
    for source in root.rglob("*"):
        rel = source.relative_to(root)
        name = rel.as_posix()
        if source.is_symlink():
            raise ValueError("包中禁止符号链接")
        if entry:
            allowed = name in ENTRY_FILES | ENTRY_OPTIONAL_FILES if source.is_file() else name == "agents"
        elif source.is_dir():
            allowed = rel.parts[0] in ALLOWED_DIRS and "__pycache__" not in rel.parts
        else:
            allowed = (
                ((len(rel.parts) == 1 and rel.name in ALLOWED_ROOT | {"release-files.json"})
                 or (len(rel.parts) > 1 and rel.parts[0] in ALLOWED_DIRS))
                and source.suffix in ALLOWED_SUFFIX
                and "__pycache__" not in rel.parts
            )
        if not allowed or not (source.is_file() or source.is_dir()):
            raise ValueError("不在发行白名单中的文件或目录：" + name)
    fp, files = fingerprint(root)
    if entry and not ENTRY_FILES <= set(files) <= ENTRY_FILES | ENTRY_OPTIONAL_FILES:
        raise ValueError("zt 入口必须包含 SKILL.md 和 agents/openai.yaml，仅可另附 LICENSE.txt")
    findings = package_privacy_issues(root)
    if findings:
        raise ValueError("发现不应随包分发的信息：" + json.dumps(findings, ensure_ascii=False))
    return fp, files


def _verify_archive(archive, files, expected_sha256=None):
    if expected_sha256 and digest(Path(archive).read_bytes()) != expected_sha256:
        raise ValueError("归档 SHA-256 不匹配")
    with zipfile.ZipFile(archive) as package:
        names = package.namelist()
        if len(names) != len(set(names)) or set(names) != set(files):
            raise ValueError("归档文件清单不匹配或有重复条目")
        if package.testzip() is not None:
            raise ValueError("归档完整性检查失败")
        for name, expected in files.items():
            if digest(package.read(name)) != expected:
                raise ValueError("归档文件哈希不匹配：" + name)


def verify_bundle(archive, manifest):
    """独立验证发行 ZIP 与外部逐文件清单；不解压、不执行其中的文件。"""
    if isinstance(manifest, (str, Path)):
        manifest = read_json(manifest)
    if (manifest.get("schema_version") != 1 or manifest.get("bundle") != ENTRY
            or manifest.get("channel") not in {"preview", "stable"}):
        raise ValueError("入口包清单格式错误")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise ValueError("归档文件清单为空")
    if not isinstance(manifest.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", manifest["sha256"]):
        raise ValueError("归档 SHA-256 格式错误")
    if {name.split("/")[0] for name in files} != {ENTRY, SKILL}:
        raise ValueError("归档必须包含并排的 zt 与 wenwen-zhentao")
    entry_files = {name.removeprefix(ENTRY + "/") for name in files if name.startswith(ENTRY + "/")}
    if not ENTRY_FILES <= entry_files <= ENTRY_FILES | ENTRY_OPTIONAL_FILES:
        raise ValueError("zt 入口文件清单错误")
    for name, expected in files.items():
        safe_relative(name)
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("归档文件哈希格式错误：" + name)
    _verify_archive(archive, files, manifest.get("sha256"))
    with zipfile.ZipFile(archive) as package:
        core_version = json.loads(package.read(SKILL + "/version.json"))
    if core_version.get("channel") != manifest["channel"]:
        raise ValueError("入口清单与核心包发行渠道不一致")
    if core_version.get("version") != manifest.get("version"):
        raise ValueError("入口清单与核心包版本不一致")
    return {"ok": True, "files_count": len(files), "sha256": digest(Path(archive).read_bytes())}


def build_entry_bundle(core_root, entry_root, out, release=False, approval=None, base_url=None):
    core_root, entry_root, out = Path(core_root), Path(entry_root), Path(out).resolve()
    # 先检查未 resolve 的根，避免根目录符号链接被悄悄接受。
    core_fp, _ = _check_source(core_root)
    entry_fp, entry_files = _check_source(entry_root, entry=True)
    core_root, entry_root = core_root.resolve(), entry_root.resolve()
    if core_root == entry_root or core_root in entry_root.parents or entry_root in core_root.parents:
        raise ValueError("两个 Skill 源目录必须独立")
    if any(root == out or root in out.parents for root in (core_root, entry_root)):
        raise ValueError("输出目录不能位于 Skill 内")
    out.mkdir(parents=True, exist_ok=True)
    # 临时目录与最终输出同一文件系统，验收全部成功后才替换最终文件。
    with tempfile.TemporaryDirectory(prefix=".zt-build-", dir=out) as tmp:
        staged_out = Path(tmp)
        core_result = build(core_root, staged_out, release=release, approval=approval, base_url=base_url)
        if release and read_json(approval).get("entry_fingerprint") != entry_fp:
            raise ValueError("公开审批记录与当前 zt 入口指纹不匹配")
        core_manifest_name = "latest.json" if release else "preview-manifest.json"
        core_manifest = read_json(staged_out / core_manifest_name)
        core_archive = Path(core_result["archive"])
        _verify_archive(core_archive,
                        {SKILL + "/" + p: h for p, h in core_manifest["files"].items()},
                        core_manifest["sha256"])
        staged_entry = staged_out / ENTRY
        staged_entry.mkdir()
        for rel in sorted(entry_files):
            target = staged_entry / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(entry_root / rel, target)
        if fingerprint(staged_entry)[1] != entry_files:
            raise ValueError("复制期间入口文件发生变化")
        version = core_manifest["version"]
        channel = "stable" if release else "preview"
        suffix = "" if release else "-preview"
        archive_name = f"{ENTRY}-{version}{suffix}.zip"
        archive_path = staged_out / archive_name
        files = {SKILL + "/" + rel: h for rel, h in core_manifest["files"].items()}
        files.update({ENTRY + "/" + rel: h for rel, h in entry_files.items()})
        with zipfile.ZipFile(core_archive) as core_zip, zipfile.ZipFile(
                archive_path, "w", zipfile.ZIP_DEFLATED) as bundle:
            for name in sorted(files):
                data = (staged_entry / name.removeprefix(ENTRY + "/")).read_bytes() if name.startswith(
                    ENTRY + "/") else core_zip.read(name)
                info = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                bundle.writestr(info, data)
        manifest = {
            "schema_version": 1, "bundle": ENTRY, "version": version, "channel": channel,
            "sha256": digest(archive_path.read_bytes()), "files": files,
            "source_fingerprints": {SKILL: core_fp, ENTRY: entry_fp},
            "core_archive": {"name": core_archive.name, "sha256": core_manifest["sha256"]},
        }
        manifest_name = f"{ENTRY}-{version}{suffix}-manifest.json"
        write_json(staged_out / manifest_name, manifest)
        verify_bundle(archive_path, staged_out / manifest_name)
        if _check_source(core_root)[0] != core_fp or _check_source(entry_root, entry=True)[0] != entry_fp:
            raise ValueError("构建期间源文件发生变化；未替换发行产物")
        # 核心 ZIP、清单和结果保持与原单包工具兼容，供既有更新器使用。
        core_result["archive"] = str(out / core_archive.name)
        write_json(staged_out / "build-result.json", core_result)
        result = {
            "ok": True, "channel": channel, "archive": str(out / archive_name),
            "manifest": str(out / manifest_name), "sha256": manifest["sha256"],
            "files_count": len(files), "core_archive": core_result["archive"], "uploaded": False,
        }
        write_json(staged_out / "entry-bundle-result.json", result)
        # 最终 ZIP 仅由已经逐文件复核过的临时 ZIP 原子替换。
        for name in (core_archive.name, core_manifest_name, "build-result.json",
                     manifest_name, "entry-bundle-result.json", archive_name):
            (staged_out / name).replace(out / name)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill-dir", type=Path, default=Path("skills") / SKILL)
    parser.add_argument("--entry-dir", type=Path, default=Path("skills") / ENTRY)
    parser.add_argument("--out", type=Path, default=Path("dist"))
    parser.add_argument("--release", action="store_true")
    parser.add_argument("--approval", type=Path, help="正式审批记录；同时包含核心与 entry_fingerprint 指纹")
    parser.add_argument("--base-url")
    args = parser.parse_args()
    try:
        result = build_entry_bundle(args.skill_dir, args.entry_dir, args.out,
                                    args.release, args.approval, args.base_url)
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
