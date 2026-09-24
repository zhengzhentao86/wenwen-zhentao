#!/usr/bin/env python3
"""Install a locally built preview without overwriting an existing Skill."""
import argparse
import json
import os
import shutil
import tempfile
import zipfile
import uuid
from pathlib import Path

from wwzt_core import SKILL, digest, read_json, safe_relative, fingerprint


def replace_local(archive, manifest, target, expected_fingerprint, workspace):
    """只替换已核对的本地安装；共用维护锁、持久事务及恢复机制。"""
    import maintain as m
    target, workspace = m.install_path(target), m.workspace(workspace)
    with m.install_lock(target):
        m.recover_locked(target, workspace)
        if fingerprint(target)[0] != expected_fingerprint:
            raise ValueError('安装已有新改动，停止覆盖')
        staging = Path(tempfile.mkdtemp(prefix='.' + target.name + '.maintain-', dir=target.parent))
        new, backup = staging / 'new', staging / 'backup'
        batch = 'local-install-' + uuid.uuid4().hex
        try:
            install(archive, manifest, new)
            new_fp = fingerprint(new)[0]
            record = {'schema_version': 1, 'transaction_id': uuid.uuid4().hex,
                      'skill_dir': str(target), 'workspace': str(workspace), 'batch': batch,
                      'staging_dir': str(staging), 'old_fingerprint': expected_fingerprint,
                      'new_fingerprint': new_fp, 'next_version': read_json(new / 'version.json')['version']}
            if fingerprint(target)[0] != expected_fingerprint:
                raise ValueError('安装暂存期间有新改动，停止覆盖')
            m.durable_json(m.journal_path(target), record)
            if fingerprint(target)[0] != expected_fingerprint:
                m.clear_journal(target)
                raise ValueError('写事务时安装发生变化，保留改动')
            os.replace(target, backup)
            m.sync_directory(target.parent)
            if fingerprint(backup)[0] != expected_fingerprint:
                os.replace(backup, target)
                m.clear_journal(target)
                raise ValueError('切换期间原安装出现修改，已经恢复')
            os.replace(new, target)
            m.sync_directory(target.parent)
            if fingerprint(target)[0] != new_fp:
                raise ValueError('安装结果不符，保留事务和备份以便恢复')
            m.durable_json(workspace / 'batches' / batch / 'applied.json', record)
            m.clear_journal(target)
            return {'installed': True, 'version': record['next_version'], 'target': str(target),
                    'backup': str(backup), 'fingerprint': new_fp, 'reload_required': True}
        except BaseException:
            if m.journal_path(target).exists():
                m.recover_locked(target, workspace)
            elif not backup.exists():
                shutil.rmtree(staging, ignore_errors=True)
            raise


def install(archive, manifest, target):
    archive, target = Path(archive).resolve(), Path(target).expanduser().absolute()
    meta = read_json(manifest)
    if meta.get("skill") != SKILL or digest(archive.read_bytes()) != meta.get("sha256"):
        raise ValueError("包哈希或名称与构建清单不匹配")
    if target.exists() or target.is_symlink():
        raise ValueError("目标已存在，保留现有安装；请先核对本地改动")
    files = meta.get("files", {})
    if not files or len(files) > 2000:
        raise ValueError("文件清单缺失或异常")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".wwzt-install-", dir=target.parent) as tmp:
        staged = Path(tmp) / SKILL
        with zipfile.ZipFile(archive) as zipped:
            infos = zipped.infolist()
            if len(infos) != len(files) or {i.filename for i in infos} != {SKILL + "/" + x for x in files}:
                raise ValueError("ZIP 内容与清单不匹配")
            for rel, expected in files.items():
                rel = safe_relative(rel)
                entry = zipped.getinfo(SKILL + "/" + rel)
                if entry.file_size > 10 * 1024 * 1024 or (entry.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError("文件大小或类型不允许")
                body = zipped.read(entry)
                if digest(body) != expected:
                    raise ValueError("文件哈希不匹配")
                output = staged / rel
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(body)
        if target.exists():
            raise ValueError("安装期间目标已出现，停止覆盖")
        os.rename(staged, target)
    return {"installed": True, "target": str(target), "version": meta["version"],
            "files": len(files), "reload_required": True}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--archive", type=Path, required=True)
    p.add_argument("--manifest", type=Path, required=True)
    p.add_argument("--target", type=Path, required=True)
    args = p.parse_args()
    try:
        print(json.dumps(install(args.archive, args.manifest, args.target), ensure_ascii=False, indent=2))
    except (ValueError, OSError, zipfile.BadZipFile) as exc:
        print(json.dumps({"installed": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
