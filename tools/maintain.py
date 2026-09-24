#!/usr/bin/env python3
"""本机知识维护：采集原文、保存候选、绑定审阅批准、原子应用；不发布或调度。"""
import argparse
import contextlib
import datetime as dt
import difflib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from urllib.parse import urlparse

from wwzt_core import digest, fingerprint, read_json, validate_catalog, validate_item, write_json


class MaintenanceError(ValueError):
    pass


class EnvelopeError(MaintenanceError):
    pass


class LarkData(dict):
    """保留完整信封供私有快照追溯，调用者使用已校验的 data。"""
    envelope = None


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def safe_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", value) or ".." in value:
        raise MaintenanceError("来源或批次 ID 不合法")
    return value


def workspace(path):
    path = Path(path).absolute()
    if path.is_symlink():
        raise MaintenanceError("维护目录不能是符号链接")
    path = path.parent.resolve() / path.name
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    # 私有正文的目录树不应被其他本机用户直接读取。
    path.chmod(0o700)
    return path


def state_for(ws):
    path = ws / "state.json"
    return read_json(path) if path.exists() else {"schema_version": 1, "sources": {}}


def save_source(ws, source, content=None, envelope=None, status="ready"):
    ident = safe_id(source["id"])
    state = state_for(ws)
    previous = state["sources"].get(ident, {})
    if status != "ready":
        record = {"status": status, "source": source, "checked_at": now()}
        changed = previous.get("status") != status or previous.get("source") != source
    else:
        if not isinstance(content, str) or not content.strip():
            raise EnvelopeError("来源正文为空或不是文本，未推进该来源快照")
        content_hash = digest(content)
        relative = "snapshots/%s/%s.json" % (ident, content_hash)
        snapshot = ws / relative
        if not snapshot.exists():
            write_json(snapshot, {"schema_version": 1, "source_id": ident, "content_hash": content_hash,
                       "content": content, "source": source, "fetched_at": now(), "envelope": envelope})
        record = {"status": "ready", "source": source, "content_hash": content_hash,
                  "snapshot": relative, "checked_at": now()}
        changed = previous.get("content_hash") != content_hash or previous.get("status") != "ready"
    state["sources"][ident] = record
    write_json(ws / "state.json", state)
    return {"source_id": ident, "status": status, "changed": changed}


def import_local(source, ws):
    path = Path(source["path"]).expanduser().resolve()
    if not path.is_file():
        raise MaintenanceError("指定本地素材不存在或不是文件")
    source = dict(source, path=str(path))
    if path.suffix.lower() not in {".md", ".txt", ".json"}:
        return save_source(ws, source, status="needs_transcription")
    if path.stat().st_size > 20 * 1024 * 1024:
        raise MaintenanceError("文本超过 20 MB，请先按内容拆分")
    try:
        content = path.read_text(encoding="utf-8")
        if path.suffix.lower() == ".json":
            json.loads(content)
    except (UnicodeError, json.JSONDecodeError):
        raise MaintenanceError("本地素材不是有效 UTF-8 文本或 JSON") from None
    return save_source(ws, source, content)


def manual_local(path, ws):
    path = Path(path).expanduser().resolve()
    source = {"id": "local-" + digest(str(path))[:20], "kind": "local", "path": str(path), "enabled": True}
    return import_local(source, workspace(ws))


def parse_envelopes(text):
    decoder, found, pos = json.JSONDecoder(), [], 0
    while pos < len(text):
        start = text.find("{", pos)
        if start < 0:
            break
        try:
            obj, end = decoder.raw_decode(text, start)
            pos = end
            if isinstance(obj, dict) and isinstance(obj.get("ok"), bool):
                found.append(obj)
        except json.JSONDecodeError:
            pos = start + 1
    return found


def lark(args):
    try:
        completed = subprocess.run(["lark-cli"] + list(args) + ["--as", "user", "--format", "json"],
                                   capture_output=True, text=True, timeout=90, check=False)
    except subprocess.TimeoutExpired:
        raise MaintenanceError("lark-cli 读取超时，本来源保留待重试") from None
    except OSError:
        raise MaintenanceError("无法运行 lark-cli，请检查安装与本机权限") from None
    # 错误信封可能在 stderr，notice 可能包围 JSON；不得按某一行截断解析。
    streams = [completed.stderr, completed.stdout] if completed.returncode else [completed.stdout, completed.stderr]
    envelopes = [entry for stream in streams for entry in parse_envelopes(stream)]
    matching = [x for x in envelopes if x["ok"] == (completed.returncode == 0)]
    if not matching:
        raise EnvelopeError("lark-cli 未返回可验证的 JSON 信封，停止本批读取")
    envelope = matching[0] if completed.returncode else matching[-1]
    if completed.returncode or not envelope["ok"]:
        code = envelope.get("error", {}).get("code")
        suffix = "，错误码 %s" % code if isinstance(code, int) else ""
        # 不打印 CLI message/hint，可能包含凭据、媒体签名及私人内容。
        raise MaintenanceError("lark-cli 读取失败，请检查来源权限与登录状态" + suffix)
    if not isinstance(envelope.get("data"), dict):
        raise EnvelopeError("lark-cli data 结构不符合约定，停止本批读取")
    result = LarkData(envelope["data"])
    result.envelope = envelope
    return result


def fetch_doc(source, ws):
    target = source.get("url") or source.get("token")
    if not isinstance(target, str) or not target:
        raise MaintenanceError("文档来源缺少 URL 或 token")
    if "://" in target:
        url = urlparse(target)
        if url.scheme != "https" or not url.hostname or not any(url.hostname.endswith("." + domain) or url.hostname == domain
                for domain in ("feishu.cn", "larkoffice.com", "larksuite.com")):
            raise MaintenanceError("文档来源必须使用飞书/Lark HTTPS 链接")
    data = lark(["docs", "+fetch", "--doc", target, "--doc-format", "markdown", "--detail", "with-ids"])
    document = data.get("document")
    if not isinstance(document, dict) or not isinstance(document.get("content"), str):
        raise EnvelopeError("飞书文档正文结构不完整，停止本批读取")
    return save_source(ws, source, document["content"], getattr(data, "envelope", None))


def wiki_sources(source):
    space_id = str(source.get("space_id", ""))
    if not (space_id.isdigit() or space_id == "my_library"):
        raise MaintenanceError("知识库来源需要数字 space_id 或 my_library")
    max_depth = source.get("max_depth", 2)
    if not isinstance(max_depth, int) or not 0 <= max_depth <= 2:
        raise MaintenanceError("知识库采集深度必须为 0 到 2")
    queue, visited, documents = [(None, 0)], set(), set()
    while queue:
        parent, depth = queue.pop(0)
        page_token, seen_pages = None, set()
        while True:
            args = ["wiki", "+node-list", "--space-id", space_id, "--page-size", "50"]
            if parent:
                args += ["--parent-node-token", parent]
            if page_token:
                args += ["--page-token", page_token]
            data = lark(args)
            if not isinstance(data.get("nodes"), list) or not isinstance(data.get("has_more"), bool):
                raise EnvelopeError("知识库节点或分页结构不完整，停止本批读取")
            for node in data["nodes"]:
                if not isinstance(node, dict) or not node.get("node_token"):
                    raise EnvelopeError("知识库节点缺少稳定编号")
                token = safe_id(node["node_token"])
                if node.get("obj_type") == "docx" and token not in documents:
                    documents.add(token)
                    yield {"id": safe_id(source["id"] + "-" + token), "kind": "lark_doc",
                           "token": token, "parent_source_id": source["id"], "enabled": True}
                if node.get("has_child") and depth < max_depth and token not in visited:
                    visited.add(token)
                    queue.append((token, depth + 1))
            if not data["has_more"]:
                break
            page_token = data.get("page_token")
            if not isinstance(page_token, str) or not page_token or page_token in seen_pages:
                raise EnvelopeError("知识库分页游标缺失或循环，停止本批读取")
            seen_pages.add(page_token)


def sync(registry, ws):
    ws = workspace(ws)
    config = read_json(registry)
    if config.get("schema_version") != 1 or not isinstance(config.get("sources"), list):
        raise MaintenanceError("来源清单结构错误")
    sources, seen = [], set()
    for source in config["sources"]:
        if not isinstance(source, dict) or source.get("kind") not in {"local", "lark_doc", "wiki_space"}:
            raise MaintenanceError("来源类型不受支持")
        ident = safe_id(source.get("id"))
        if ident in seen:
            raise MaintenanceError("来源 ID 重复")
        seen.add(ident)
        if source.get("enabled") is True:
            sources.append(source)
    result = {"changed": 0, "unchanged": 0, "deferred": 0, "failures": [], "stopped": False}
    progress_path = ws / "sync-progress.json"
    progress = read_json(progress_path) if progress_path.exists() else {}
    config_hash = digest(canonical(config))
    if progress.get("registry_hash") != config_hash or progress.get("complete"):
        progress = {"registry_hash": config_hash, "complete": False, "processed": [], "completed_sources": []}
    processed, completed_sources = set(progress["processed"]), set(progress["completed_sources"])
    for source in sources:
        if source["id"] in completed_sources:
            continue
        try:
            entries = wiki_sources(source) if source["kind"] == "wiki_space" else [source]
            for entry in entries:
                if entry["id"] in processed:
                    continue
                status = import_local(entry, ws) if entry["kind"] == "local" else fetch_doc(entry, ws)
                processed.add(entry["id"])
                progress["processed"] = sorted(processed)
                write_json(progress_path, progress)
                result["changed" if status["changed"] else "unchanged"] += 1
                result["deferred"] += int(status["status"] != "ready")
            completed_sources.add(source["id"])
            progress["completed_sources"] = sorted(completed_sources)
            write_json(progress_path, progress)
        except (MaintenanceError, OSError) as error:
            # 失败只记录类别和稳定编号，不覆盖已有成功快照。
            result["failures"].append({"source_id": source["id"], "reason": type(error).__name__})
            if isinstance(error, EnvelopeError):
                result["stopped"] = True
                break
    progress["complete"] = not result["failures"]
    write_json(progress_path, progress)
    write_json(ws / "last-sync.json", dict(result, checked_at=now()))
    return result


def file_hash(path):
    return digest(path.read_bytes()) if path.exists() else None


def baseline(skill, items):
    return {"catalog": file_hash(skill / "knowledge/catalog.json"), "version": file_hash(skill / "version.json"),
            "cards": {x["item"]["path"]: file_hash(skill / x["item"]["path"]) for x in items}}


def normalized_question(text):
    return re.sub(r"[\W_]+", "", text).casefold()


def verify_evidence(entry, ws):
    evidence = entry.get("evidence", {})
    ident = safe_id(evidence.get("source_id"))
    state = state_for(ws)["sources"].get(ident, {})
    content_hash = evidence.get("content_hash") or state.get("content_hash")
    if not isinstance(content_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", content_hash):
        raise MaintenanceError("候选的原文快照不存在")
    path = ws / "snapshots" / ident / (content_hash + ".json")
    if not path.is_file():
        raise MaintenanceError("候选的原文快照不存在")
    snapshot = read_json(path)
    content = snapshot.get("content", "")
    if snapshot.get("source_id") != ident or digest(content) != content_hash:
        raise MaintenanceError("原文快照完整性检查失败")
    for field in ("quote", "anchor"):
        value = evidence.get(field)
        if not isinstance(value, str) or not value.strip() or value not in content:
            raise MaintenanceError("引用或定位文字未匹配原文快照")
    if len(evidence["quote"].strip()) < 8:
        raise MaintenanceError("引用过短，无法支撑有效审阅")
    return dict(evidence, content_hash=content_hash)


def stage(input_path, skill, ws):
    ws, skill = workspace(ws), Path(skill).absolute()
    catalog = validate_catalog(skill)
    fingerprint(skill)  # 拒绝安装树中的符号链接。
    candidate = read_json(input_path)
    entries = candidate.get("items")
    if not isinstance(entries, list) or not entries:
        raise MaintenanceError("候选必须包含非空 items")
    questions = {normalized_question(x["question"]): x["id"] for x in catalog["items"]}
    seen, clean = set(), []
    for entry in entries:
        item = entry.get("item", {})
        validate_item(item)
        for key in ("source_date", "verified_at"):
            if item[key] is not None:
                try:
                    dt.date.fromisoformat(item[key])
                except ValueError:
                    raise MaintenanceError("来源或核验日期不是有效日历日期") from None
        if item["id"] in seen:
            raise MaintenanceError("候选知识 ID 重复")
        seen.add(item["id"])
        question = normalized_question(item["question"])
        if not question:
            raise MaintenanceError("问题需要有效文字")
        if question in questions and questions[question] != item["id"]:
            raise MaintenanceError("同一问题已有不同稳定 ID，请先合并或明确场景差异")
        questions[question] = item["id"]
        if item["public_status"] != "pending_owner_review":
            raise MaintenanceError("候选必须先标记 pending_owner_review")
        if not isinstance(entry.get("content"), str) or len(entry["content"].strip()) < 40:
            raise MaintenanceError("候选正文缺失或过短")
        if catalog['schema_version'] == 2:
            from wwzt_core import validate_runtime_text
            validate_runtime_text(entry['content'])
        clean.append({"item": item, "content": entry["content"], "evidence": verify_evidence(entry, ws)})
    candidate = {"schema_version": 1, "items": clean, "baseline": baseline(skill, clean)}
    candidate_digest = digest(canonical(candidate))
    batch = "batch-" + candidate_digest[:20]
    folder = ws / "batches" / batch
    if folder.exists():
        load_batch(batch, ws)
        return {"batch": batch, "candidate_digest": candidate_digest, "existing": True}
    folder.mkdir(parents=True)
    write_json(folder / "candidate.json", candidate)
    write_json(folder / "integrity.json", {"candidate_digest": candidate_digest, "created_at": now()})
    changes = []
    for entry in clean:
        path = skill / entry["item"]["path"]
        old = path.read_text(encoding="utf-8") if path.exists() else ""
        diff = "".join(difflib.unified_diff(old.splitlines(True), entry["content"].splitlines(True), fromfile="before", tofile="after"))
        changes.append({"id": entry["item"]["id"], "question": entry["item"]["question"], "diff": diff,
                        "evidence": entry["evidence"], "metadata": entry["item"], "test_status": "not_run"})
    report = {"candidate_digest": candidate_digest, "changes": changes,
              "required_review": ["核对来源实际归属；合编 FAQ 不等于本人原话", "核对时效和适用版本", "确认允许公开的内容与摘要", "运行关联问题测试并记录结果"],
              "scope": "批准仅使指定知识在本地正式版本生效，不授权公开发布"}
    write_json(folder / "review.json", report)
    return {"batch": batch, "candidate_digest": candidate_digest, "review_digest": digest(canonical(report))}


def load_batch(batch, ws):
    folder = Path(ws) / "batches" / safe_id(batch)
    candidate = read_json(folder / "candidate.json")
    actual = digest(canonical(candidate))
    if actual != read_json(folder / "integrity.json").get("candidate_digest") or batch != "batch-" + actual[:20]:
        raise MaintenanceError("候选已变更，必须重新 stage 和审阅")
    for entry in candidate["items"]:
        validate_item(entry["item"])
        verify_evidence(entry, Path(ws))
    report = read_json(folder / "review.json")
    if report.get("candidate_digest") != actual:
        raise MaintenanceError("审阅材料与候选不一致")
    return folder, candidate, actual, report, digest(canonical(report))


def review(batch, ws):
    folder, candidate, actual, report, review_digest = load_batch(batch, ws)
    return {"batch": batch, "candidate_digest": actual, "review_digest": review_digest,
            "items": [{"id": x["item"]["id"], "question": x["item"]["question"]} for x in candidate["items"]],
            "review_file": str(folder / "review.json"), "scope": report["scope"]}


def approve(batch, review_digest, approved_by, ids, confirm, ws):
    if not confirm or not isinstance(approved_by, str) or not approved_by.strip():
        raise MaintenanceError("需要真实用户明确确认及审核人标识后才能记录批准")
    folder, candidate, actual, report, expected = load_batch(batch, ws)
    if review_digest != expected:
        raise MaintenanceError("审阅摘要已变更，请重新读取审阅材料")
    available = {x["item"]["id"] for x in candidate["items"]}
    selected = available if ids == "all" else set(ids.split(","))
    if not selected or not selected <= available:
        raise MaintenanceError("批准的知识 ID 不属于本候选批次")
    approval = {"candidate_digest": actual, "review_digest": expected, "ids": sorted(selected),
                "approved_by": approved_by.strip(), "approved_at": now(), "scope": "local_apply_only"}
    write_json(folder / "approval.json", approval)
    return {"batch": batch, "approved": approval["ids"], "scope": approval["scope"]}


def semantic_version(version):
    if not isinstance(version, str) or not re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", version):
        raise MaintenanceError("版本号必须为 x.y.z")
    return tuple(int(part) for part in version.split("."))


def write_index(skill, catalog):
    lines = ["# 问问镇涛知识索引", "", "按真实问题定位知识卡，回答前读取正文与适用条件。", ""]
    groups = {"tools": "工具与操作排错", "prompts": "提示词与内容表达", "visual": "视觉效果与素材",
              "workflow": "制作方法与业务落地", "tables": "多维表格与数据分析"}
    for category, heading in groups.items():
        entries = sorted((x for x in catalog["items"] if x["category"] == category), key=lambda x: x["id"])
        if not entries:
            continue
        lines += ["## " + heading, ""]
        for entry in entries:
            title = entry["question"].replace("[", "（").replace("]", "）").replace("\n", " ")
            lines.append("- [%s · %s](%s.md) — %s" % (entry["id"], title, entry["id"], entry["summary"].replace("\n", " ")))
        lines.append("")
    (skill / "knowledge/INDEX.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def install_path(skill):
    path = Path(skill).absolute()
    if path.is_symlink():
        raise MaintenanceError("安装目录不能是符号链接")
    return path.parent.resolve() / path.name


@contextlib.contextmanager
def install_lock(skill):
    # 与学员端 updater 的 install_lock 完全共用路径、O_EXCL 和锁文件格式。
    lock = skill.parent / ("." + skill.name + ".update.lock")
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise MaintenanceError("存在更新锁；确认无更新进程后再处理：" + str(lock)) from None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"pid": os.getpid(), "created_at": int(time.time())}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        yield
    finally:
        lock.unlink(missing_ok=True)


def sync_directory(path):
    # POSIX 上同时落盘目录项，避免只写出 JSON 内容却丢失 rename 元数据。
    if os.name == "posix":
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def durable_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, str(path))
        sync_directory(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def journal_path(skill):
    return skill.parent / ("." + skill.name + ".maintain-transaction.json")


def clear_journal(skill):
    journal_path(skill).unlink()
    sync_directory(skill.parent)


def recover_locked(skill, ws):
    journal = journal_path(skill)
    if not journal.exists():
        return {"status": "no_transaction"}
    if journal.is_symlink():
        raise MaintenanceError("事务日志不能是符号链接")
    record = read_json(journal)
    if record.get("skill_dir") != str(skill) or record.get("workspace") != str(ws):
        raise MaintenanceError("事务属于其他安装或维护目录，请使用日志对应的原目录恢复")
    staging = Path(record["staging_dir"])
    if staging.parent != skill.parent or not staging.name.startswith("." + skill.name + ".maintain-") or staging.is_symlink():
        raise MaintenanceError("事务暂存路径不属于本安装，停止恢复")
    backup, new = staging / "backup", staging / "new"
    if backup.is_symlink() or new.is_symlink():
        raise MaintenanceError("事务备份或暂存目录不能是符号链接")
    receipt = ws / "batches" / safe_id(record["batch"]) / "applied.json"
    if receipt.is_file() and read_json(receipt).get("transaction_id") == record["transaction_id"]:
        if not skill.is_dir():
            raise MaintenanceError("事务已提交但安装目录缺失，需要人工核对，备份保留")
        fingerprint(skill)
        clear_journal(skill)
        return {"status": "committed", "batch": record["batch"]}
    if backup.exists():
        if fingerprint(backup)[0] != record["old_fingerprint"]:
            raise MaintenanceError("恢复备份已变化，停止自动覆盖；请保留事务日志与备份")
        if skill.exists():
            if fingerprint(skill)[0] != record["new_fingerprint"]:
                raise MaintenanceError("中断后的安装出现外部改动，停止自动覆盖；请保留事务日志与备份")
            interrupted = staging / "interrupted"
            if interrupted.exists():
                raise MaintenanceError("恢复暂存目录已存在，需要人工核对")
            os.replace(str(skill), str(interrupted))
            try:
                os.replace(str(backup), str(skill))
            except BaseException:
                os.replace(str(interrupted), str(skill))
                raise
        else:
            os.replace(str(backup), str(skill))
        sync_directory(skill.parent)
    elif not skill.is_dir() or fingerprint(skill)[0] != record["old_fingerprint"]:
        raise MaintenanceError("无法核对原安装或恢复备份，停止自动覆盖；请保留事务日志")
    if fingerprint(skill)[0] != record["old_fingerprint"]:
        raise MaintenanceError("恢复后指纹不符，保留日志等待检查")
    clear_journal(skill)
    shutil.rmtree(staging, ignore_errors=True)
    return {"status": "rolled_back", "batch": record["batch"]}


def recover(skill, ws):
    ws, skill = workspace(ws), install_path(skill)
    with install_lock(skill):
        return recover_locked(skill, ws)


def apply(batch, version, skill, ws):
    ws, skill = workspace(ws), install_path(skill)
    if skill == ws or skill in ws.parents or ws in skill.parents:
        raise MaintenanceError("维护目录与安装目录必须分离，不能互相包含")
    with install_lock(skill):
        recover_locked(skill, ws)
        return apply_locked(batch, version, skill, ws)


def apply_locked(batch, version, skill, ws):
    folder, candidate, actual, report, review_digest = load_batch(batch, ws)
    approval_path = folder / "approval.json"
    if not approval_path.exists():
        raise MaintenanceError("候选尚未获明确批准")
    approval = read_json(approval_path)
    selected = set(approval.get("ids", []))
    available = {x["item"]["id"] for x in candidate["items"]}
    if approval.get("candidate_digest") != actual or approval.get("review_digest") != review_digest or not selected or not selected <= available or approval.get("scope") != "local_apply_only":
        raise MaintenanceError("批准已失效或范围不匹配，请重新审阅")
    if (folder / "applied.json").exists():
        raise MaintenanceError("本批次已经应用，不能重复执行")
    before = fingerprint(skill)[0]
    if baseline(skill, candidate["items"]) != candidate["baseline"]:
        raise MaintenanceError("知识基线已经变化，必须重新生成差异并审阅")
    previous_version = read_json(skill / "version.json")
    if semantic_version(version) <= semantic_version(previous_version["version"]):
        raise MaintenanceError("新版本必须高于当前版本")
    staging = Path(tempfile.mkdtemp(prefix="." + skill.name + ".maintain-", dir=str(skill.parent)))
    new, backup = staging / "new", staging / "backup"
    try:
        shutil.copytree(skill, new)
        if fingerprint(skill)[0] != before or fingerprint(new)[0] != before:
            raise MaintenanceError("复制期间安装目录发生变化，已停止应用并保留外部修改")
        catalog = validate_catalog(new)
        by_id = {entry["id"]: entry for entry in catalog["items"]}
        for entry in candidate["items"]:
            if entry["item"]["id"] not in selected:
                continue
            # 本地内容应用和公开许可是两项决定。本批审批 scope 只有
            # local_apply_only，不能据此把新知识提升为允许公开。
            value = dict(entry["item"])
            if catalog['schema_version'] == 2:
                from wwzt_core import runtime_item, validate_runtime_text
                validate_runtime_text(entry['content'])
                value = runtime_item(value)
            by_id[value["id"]] = value
            (new / value["path"]).write_text(entry["content"], encoding="utf-8")
        catalog["items"] = sorted(by_id.values(), key=lambda x: x["id"])
        write_json(new / "knowledge/catalog.json", catalog)
        write_index(new, catalog)
        write_json(new / "version.json", dict(previous_version, version=version, knowledge_version=version))
        validate_catalog(new)
        next_fingerprint = fingerprint(new)[0]
        if fingerprint(skill)[0] != before:
            raise MaintenanceError("切换前安装目录发生变化，已停止应用并保留外部修改")
        transaction_id = uuid.uuid4().hex
        record = {"schema_version": 1, "transaction_id": transaction_id, "skill_dir": str(skill),
                  "workspace": str(ws), "batch": batch, "staging_dir": str(staging),
                  "old_fingerprint": before, "new_fingerprint": next_fingerprint, "next_version": version,
                  "candidate_digest": actual, "review_digest": review_digest, "created_at": now()}
        durable_json(journal_path(skill), record)
        try:
            if fingerprint(skill)[0] != before:
                # 尚未移动旧包，保留外部新修改且取消本次事务。
                clear_journal(skill)
                raise MaintenanceError("写事务日志期间安装发生变化，已保留外部修改")
            os.replace(str(skill), str(backup))
            sync_directory(skill.parent)
            if fingerprint(backup)[0] != before:
                # 捕获最后一次核对与 rename 之间发生的非协作写入。
                os.replace(str(backup), str(skill))
                clear_journal(skill)
                raise MaintenanceError("切换瞬间发现外部改动，已还原其完整目录")
            os.replace(str(new), str(skill))
            sync_directory(skill.parent)
            if fingerprint(skill)[0] != next_fingerprint:
                raise MaintenanceError("新安装指纹不符，需保留事务进一步核对")
            result = {"batch": batch, "version": version, "applied": sorted(selected), "applied_at": now(),
                      "backup_dir": str(backup), "published": False, "transaction_id": transaction_id}
            durable_json(folder / "applied.json", result)
            clear_journal(skill)
        except BaseException:
            if journal_path(skill).exists():
                try:
                    recover_locked(skill, ws)
                except BaseException:
                    raise MaintenanceError("切换中断且自动恢复未完成；保留事务与备份，请运行 recover：" + str(journal_path(skill))) from None
            raise
        return result
    except (OSError, ValueError) as error:
        if isinstance(error, MaintenanceError):
            raise
        raise MaintenanceError("应用失败，原可用版本已保留；请检查本机磁盘和写权限") from None
    finally:
        if not journal_path(skill).exists() and not backup.exists():
            shutil.rmtree(staging, ignore_errors=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("sync", "manual-local", "stage", "review", "approve", "apply", "recover"):
        command = commands.add_parser(name)
        command.add_argument("--workspace", default=".maintenance")
        if name == "sync":
            command.add_argument("--registry", required=True)
        if name == "manual-local":
            command.add_argument("path")
        if name in {"stage", "apply", "recover"}:
            command.add_argument("--skill-dir", required=True)
        if name == "stage":
            command.add_argument("--input", required=True)
        if name in {"review", "approve", "apply"}:
            command.add_argument("--batch", required=True)
        if name == "approve":
            command.add_argument("--review-digest", required=True)
            command.add_argument("--approved-by", required=True)
            command.add_argument("--ids", required=True)
            command.add_argument("--confirm", action="store_true")
        if name == "apply":
            command.add_argument("--version", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "sync":
            result = sync(args.registry, args.workspace)
        elif args.command == "manual-local":
            result = manual_local(args.path, args.workspace)
        elif args.command == "stage":
            result = stage(args.input, args.skill_dir, args.workspace)
        elif args.command == "review":
            result = review(args.batch, args.workspace)
        elif args.command == "approve":
            result = approve(args.batch, args.review_digest, args.approved_by, args.ids, args.confirm, args.workspace)
        elif args.command == "recover":
            result = recover(args.skill_dir, args.workspace)
        else:
            result = apply(args.batch, args.version, args.skill_dir, args.workspace)
        if args.command != "sync" or result["changed"] or result["failures"]:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get("failures") else 0
    except (MaintenanceError, ValueError, OSError, KeyError, TypeError) as error:
        message = str(error) if isinstance(error, MaintenanceError) else "维护失败：输入结构、文件或权限异常；请检查本地材料"
        print(json.dumps({"ok": False, "error": message}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
