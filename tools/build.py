#!/usr/bin/env python3
"""构建本地预览包，或有内容审批依据的正式发行包。不会上传。"""
import argparse
import json
import re
import shutil
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urlparse

from wwzt_core import SKILL, digest, fingerprint, package_privacy_issues, read_json, validate_catalog, write_json

ALLOWED_DIRS = {"knowledge", "references", "scripts", "agents"}
ALLOWED_ROOT = {"SKILL.md", "version.json", "README.md", "CHANGELOG.md", "LICENSE.txt"}
ALLOWED_SUFFIX = {".md", ".json", ".py", ".yaml", ".txt"}


def build(root, out, release=False, approval=None, base_url=None):
    root, out = Path(root).resolve(), Path(out).resolve()
    if root == out or root in out.parents:
        raise ValueError("输出目录不能位于 Skill 内")
    catalog = validate_catalog(root)
    meta = read_json(root / "version.json")
    if meta.get("skill") != SKILL or not re.fullmatch(r"\d+\.\d+\.\d+", meta.get("version", "")):
        raise ValueError("版本或 Skill 名称错误")
    fp, source_files = fingerprint(root)
    if release:
        if catalog['schema_version'] == 1 and any(x["public_status"] != "approved" for x in catalog["items"]):
            raise ValueError("仍有未获公开确认的知识；只能构建本地 preview")
        if not approval:
            raise ValueError("正式构建需要该批内容的审批记录")
        record = read_json(approval)
        if catalog['schema_version'] == 2 and set(record.get('approved_knowledge_ids', [])) != {x['id'] for x in catalog['items']}:
            raise ValueError("运行知识未获公开确认；包外审批须覆盖全部知识编号")
        if record.get("package_fingerprint") != fp or record.get("decision") != "approve_public_release":
            raise ValueError("审批记录与当前包不匹配，或没有批准公开")
        if not record.get("approved_by") or not record.get("approved_at"):
            raise ValueError("审批记录缺少确认人和时间")
        if not base_url or urlparse(base_url).scheme != "https" or not urlparse(base_url).netloc:
            raise ValueError("正式包需要真实 HTTPS 发行目录")
        if not (root / "LICENSE.txt").is_file():
            raise ValueError("正式包需要已选定的 LICENSE.txt")
    findings = package_privacy_issues(root)
    if findings:
        raise ValueError("发现不应随包分发的信息：" + json.dumps(findings, ensure_ascii=False))
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="wwzt-build-") as tmp:
        staged = Path(tmp) / SKILL
        staged.mkdir()
        for source in sorted(root.rglob("*")):
            rel = source.relative_to(root)
            if source.is_symlink():
                raise ValueError("包中禁止符号链接")
            if not source.is_file() or "__pycache__" in rel.parts:
                continue
            if rel.as_posix() == "release-files.json":
                continue
            if (len(rel.parts) == 1 and rel.name not in ALLOWED_ROOT) or (len(rel.parts) > 1 and rel.parts[0] not in ALLOWED_DIRS):
                raise ValueError("不在发行白名单中的文件：" + str(rel))
            if source.suffix not in ALLOWED_SUFFIX:
                raise ValueError("未批准的文件类型：" + str(rel))
            target = staged / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        # 审批校验与复制之间源文件可能被另一个任务改写。所有构建都冻结
        # 并比较快照；只有校验完成后才生成发行专用元数据。
        _, staged_files = fingerprint(staged)
        copied_files = {path: sha256 for path, sha256 in source_files.items() if path != "release-files.json"}
        # 已安装/解包目录含生成的基线，构建时将重新生成；只在拷贝内容比较
        # 中排除根目录基线。审批及源目录竞态检测仍覆盖原源的每一个文件。
        if staged_files != copied_files or fingerprint(root)[0] != fp:
            raise ValueError("复制期间源文件发生变化；未生成发行包，请重新审阅")
        meta["channel"] = "stable" if release else "preview"
        meta["update_manifest_url"] = base_url.rstrip("/") + "/latest.json" if release else None
        write_json(staged / "version.json", meta)
        _, files = fingerprint(staged)
        write_json(staged / "release-files.json", {"schema_version": 1, "files": files})
        _, files = fingerprint(staged)
        suffix = "" if release else "-preview"
        archive_name = f"{SKILL}-{meta['version']}{suffix}.zip"
        destination = out / archive_name
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
            for rel in sorted(files):
                # UTF-8 安全的 ASCII 包名和内部路径；固定时间保证可复验。
                info = zipfile.ZipInfo(SKILL + "/" + rel, (2026, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, (staged / rel).read_bytes())
        manifest = {"schema_version": 1, "skill": SKILL, "version": meta["version"],
                    "archive_url": base_url.rstrip("/") + "/" + archive_name if release else None,
                    "sha256": digest(destination.read_bytes()), "files": files, "min_updater_version": 1}
        write_json(out / ("latest.json" if release else "preview-manifest.json"), manifest)
        result = {"ok": True, "channel": meta["channel"], "archive": str(destination),
                  "sha256": manifest["sha256"], "knowledge_count": len(catalog["items"]),
                  "files_count": len(files), "uploaded": False}
        write_json(out / "build-result.json", result)
        return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--skill-dir", type=Path, default=Path("skills") / SKILL)
    p.add_argument("--out", type=Path, default=Path("dist"))
    p.add_argument("--release", action="store_true")
    p.add_argument("--approval", type=Path)
    p.add_argument("--base-url")
    args = p.parse_args()
    try:
        result = build(args.skill_dir, args.out, args.release, args.approval, args.base_url)
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
