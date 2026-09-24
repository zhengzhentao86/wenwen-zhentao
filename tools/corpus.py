#!/usr/bin/env python3
"""私有语料盘点、完整正文采集与无损段落归一化；不生成知识卡或发布内容。"""
import argparse
import contextlib
import copy
import fcntl
import json
from pathlib import Path
import re
from urllib.parse import urlparse

import maintain as maintenance
from wwzt_core import digest, read_json, safe_relative, write_json, validate_catalog

LARK_DOMAINS = ("feishu.cn", "larkoffice.com", "larksuite.com")
TEXT_SUFFIXES = {".md", ".txt", ".json"}


def lark(args):
    return maintenance.lark(args)


def _root(workspace):
    return maintenance.workspace(maintenance.workspace(workspace) / "corpus")


@contextlib.contextmanager
def _locked(root):
    with (root / ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise maintenance.MaintenanceError("另一个语料任务正在运行，请等它结束后续跑") from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _state(root):
    path = root / "state.json"
    state = read_json(path) if path.exists() else {
        "schema_version": 1, "sources": {}, "documents": {}, "nodes": {}, "active_sources": [],
    }
    if state.get("schema_version") != 1:
        raise maintenance.MaintenanceError("语料状态版本不支持")
    return state


def _save(root, state):
    write_json(root / "state.json", state)


def _raw(root, category, identity, envelope):
    raw_hash = digest(maintenance.canonical(envelope))
    relative = f"raw/{category}/{digest(identity)[:24]}/{raw_hash}.json"
    if not (root / relative).exists():
        write_json(root / relative, envelope)
    return {"path": relative, "sha256": digest((root / relative).read_bytes())}


def _envelope(data):
    envelope = getattr(data, "envelope", None)
    return envelope if envelope is not None else {"ok": True, "data": dict(data)}


def _lark_url(value):
    parsed = urlparse(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or not any(parsed.hostname == domain or parsed.hostname.endswith("." + domain) for domain in LARK_DOMAINS)):
        raise maintenance.MaintenanceError("来源必须为飞书或 Lark HTTPS 链接")
    return parsed


def _register(state, identity, info, location):
    ident = "doc-" + digest(identity)[:24]
    doc = state["documents"].setdefault(ident, {
        "id": ident, "identity": identity, "locations": [], "discovered_at": maintenance.now(),
        "fetch_status": "pending", "body_status": "pending", "normalization_status": "pending",
        "distillation_status": "not_ready", "versions": [],
    })
    doc.update(info)
    location_key = (location["source_id"], location.get("node_token"), location.get("path_tokens"))
    doc["locations"] = [old for old in doc["locations"] if (
        old["source_id"], old.get("node_token"), old.get("path_tokens")) != location_key]
    doc["locations"].append(location)
    if not info["supported"]:
        doc["fetch_status"] = "needs_format"
        doc["format_status"] = "pending"
    elif doc["fetch_status"] == "needs_format":
        doc["fetch_status"] = "pending"
    return ident


def _node(state, source, node, path_titles, path_tokens):
    token = maintenance.safe_id(node.get("node_token"))
    obj_token = node.get("obj_token")
    if obj_token:
        maintenance.safe_id(obj_token)
    obj_type = node.get("obj_type") or "unknown"
    title = node.get("title") or token
    identity = f"lark:{obj_type}:{obj_token}" if obj_token else f"wiki:{source['space_id']}:{token}"
    location = {"source_id": source["id"], "space_id": str(source["space_id"]),
                "node_token": token, "obj_token": obj_token,
                "path_titles": path_titles + [title], "path_tokens": path_tokens + [token]}
    info = {"kind": "lark_doc", "obj_type": obj_type, "obj_token": obj_token,
            "title": title, "target": obj_token or token, "supported": obj_type == "docx"}
    ident = _register(state, identity, info, location)
    state["nodes"][str(source["space_id"]) + ":" + token] = {
        "node_token": token, "obj_token": obj_token, "obj_type": obj_type, "document_id": ident,
        "parent_node_token": path_tokens[-1] if path_tokens else None,
        "title": title, "space_id": str(source["space_id"]),
    }
    return location


def _wiki_inventory(root, state, source, progress):
    space = str(source.get("space_id", ""))
    if not space.isdigit() and space != "my_library":
        raise maintenance.MaintenanceError("知识库来源需要数字 space_id 或 my_library")
    if "queue" not in progress:
        progress["queue"] = [{"parent": None, "titles": [], "tokens": [], "page": None, "seen_pages": []}]
        progress["scheduled"] = []
    while progress["queue"]:
        job = progress["queue"][0]
        args = ["wiki", "+node-list", "--space-id", space, "--page-size", "50"]
        if job["parent"]:
            args += ["--parent-node-token", job["parent"]]
        if job["page"]:
            args += ["--page-token", job["page"]]
        data = lark(args)
        raw = _raw(root, "inventory", source["id"], _envelope(data))
        progress["last_response"] = raw
        nodes, more = data.get("nodes"), data.get("has_more")
        if not isinstance(nodes, list) or not isinstance(more, bool):
            raise maintenance.EnvelopeError("节点或分页结构不完整")
        next_page = data.get("page_token") if more else None
        if more and (not isinstance(next_page, str) or not next_page or next_page in job["seen_pages"]):
            raise maintenance.EnvelopeError("知识库分页游标缺失或循环")
        # 整页先校验，再推进游标；结构损坏时不会跳过这一页。
        for node in nodes:
            if not isinstance(node, dict):
                raise maintenance.EnvelopeError("知识库节点结构错误")
            maintenance.safe_id(node.get("node_token"))
            if node.get("obj_token"):
                maintenance.safe_id(node["obj_token"])
        for node in nodes:
            location = _node(state, source, node, job["titles"], job["tokens"])
            token = node["node_token"]
            if node.get("has_child") and token not in progress["scheduled"]:
                progress["scheduled"].append(token)
                progress["queue"].append({"parent": token, "titles": location["path_titles"],
                    "tokens": location["path_tokens"], "page": None, "seen_pages": []})
        progress["pages_completed"] = progress.get("pages_completed", 0) + 1
        if more:
            job["page"] = next_page
            job["seen_pages"].append(next_page)
        else:
            progress["queue"].pop(0)
        _save(root, state)


def _subtree_inventory(root, state, source, progress):
    if not progress.get("resolved"):
        target = source.get("url") or source.get("node_token") or source.get("token")
        if not isinstance(target, str) or not target:
            raise maintenance.MaintenanceError("Wiki 子树缺少 node_token 或 URL")
        if "://" in target:
            _lark_url(target)
        data = lark(["wiki", "+node-get", "--node-token", target])
        progress["root_response"] = _raw(root, "inventory", source["id"], _envelope(data))
        node = data.get("node", data)
        if not isinstance(node, dict) or not node.get("space_id"):
            raise maintenance.EnvelopeError("Wiki 子树解析缺少节点或空间")
        resolved = dict(source, space_id=str(node["space_id"]))
        location = _node(state, resolved, node, [], [])
        token = node["node_token"]
        progress["resolved"] = resolved
        progress["queue"] = [{"parent": token, "titles": location["path_titles"],
            "tokens": [token], "page": None, "seen_pages": []}] if node.get("has_child") else []
        progress["scheduled"] = [token]
        _save(root, state)
    _wiki_inventory(root, state, progress["resolved"], progress)


def _single_inventory(root, state, source, registry_parent):
    location = {"source_id": source["id"], "url": source.get("url"), "path_titles": [source.get("title", source["id"])]}
    if source["kind"] == "local":
        path = Path(source.get("path", "")).expanduser()
        if not path.is_absolute():
            path = registry_parent / path
        path = path.resolve()
        if not path.is_file():
            raise maintenance.MaintenanceError("本地来源不存在或不是文件")
        info = {"kind": "local", "path": str(path), "title": source.get("title", path.stem),
                "obj_type": path.suffix.lower().lstrip("."), "supported": path.suffix.lower() in TEXT_SUFFIXES}
        location["local_path"] = str(path)
        location["original_source"] = source.get("original_source")
        _register(state, "local:" + str(path), info, location)
        return
    target = source.get("url") or source.get("obj_token") or source.get("token")
    if not isinstance(target, str) or not target:
        raise maintenance.MaintenanceError("文档来源缺少 URL 或 token")
    parsed = _lark_url(target) if "://" in target else None
    parts = parsed.path.strip("/").split("/") if parsed else []
    if parts and parts[0] == "wiki":
        data = lark(["wiki", "+node-get", "--node-token", target])
        _raw(root, "inventory", source["id"], _envelope(data))
        node = data.get("node", data)
        if not isinstance(node, dict) or not node.get("space_id"):
            raise maintenance.EnvelopeError("Wiki 文档解析缺少节点或空间")
        _node(state, dict(source, space_id=str(node["space_id"])), node, [], [])
        return
    token = parts[1] if len(parts) >= 2 else target
    maintenance.safe_id(token)
    obj_type = parts[0] if parts else source.get("obj_type", "docx")
    info = {"kind": "lark_doc", "obj_type": obj_type, "obj_token": token,
            "target": token, "title": source.get("title", source["id"]), "supported": obj_type == "docx"}
    location["obj_token"] = token
    _register(state, f"lark:{obj_type}:{token}", info, location)


def inventory(registry, workspace, refresh=False):
    root, registry = _root(workspace), Path(registry).resolve()
    config = read_json(registry)
    if config.get("schema_version") != 1 or not isinstance(config.get("sources"), list):
        raise maintenance.MaintenanceError("来源清单格式错误")
    seen, enabled = set(), []
    for source in config["sources"]:
        if not isinstance(source, dict) or source.get("kind") not in {"wiki_space", "wiki_node", "lark_doc", "local"}:
            raise maintenance.MaintenanceError("来源类型不受支持")
        ident = maintenance.safe_id(source.get("id"))
        if ident in seen:
            raise maintenance.MaintenanceError("来源 ID 重复")
        seen.add(ident)
        if source.get("enabled") is True:
            enabled.append(source)
    with _locked(root):
        state = _state(root)
        state["active_sources"] = [source["id"] for source in enabled]
        write_json(root / "registry.json", config)
        failures = []
        for source in enabled:
            config_hash = digest(maintenance.canonical(source))
            progress = state["sources"].get(source["id"], {})
            if refresh or progress.get("config_hash") != config_hash:
                progress = {"config_hash": config_hash, "source": source, "status": "pending"}
                state["sources"][source["id"]] = progress
            if progress.get("status") == "complete":
                continue
            try:
                if source["kind"] == "wiki_space":
                    _wiki_inventory(root, state, source, progress)
                elif source["kind"] == "wiki_node":
                    _subtree_inventory(root, state, source, progress)
                else:
                    _single_inventory(root, state, source, registry.parent)
                progress["status"] = "complete"
                progress.pop("error", None)
            except (maintenance.MaintenanceError, OSError, ValueError) as error:
                progress["status"] = "error"
                progress["error"] = {"kind": type(error).__name__, "at": maintenance.now()}
                failures.append({"source_id": source["id"], "reason": type(error).__name__})
            _save(root, state)
        result = _summary(state)
        result.update({"ok": not failures, "failures": failures})
        return result


def _local_content(path):
    path = Path(path)
    if path.stat().st_size > 20 * 1024 * 1024:
        raise maintenance.MaintenanceError("本地文本超过 20 MB")
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() != ".json":
        return text, {"kind": "local_text", "path": str(path), "content": text}, {}
    value = json.loads(text)
    data = value.get("data", value) if isinstance(value, dict) else {}
    document = data.get("document", data) if isinstance(data, dict) else {}
    if not isinstance(document, dict):
        document = {}
    if isinstance(value, dict) and value.get("ok") is False:
        raise maintenance.EnvelopeError("本地 JSON 是失败响应，不能作为正文")
    return document.get("content"), value, document


def normalize(content, document=None):
    """只切分原文并标记定位；不改写、不分类、不判断作者或生成语义知识。"""
    if not isinstance(content, str) or not content.strip():
        raise maintenance.EnvelopeError("正文为空或缺失，未推进当前版本")
    content_hash, paragraphs = digest(content), []
    offset, start, line_start, line_number = 0, None, None, 0
    for line in content.splitlines(keepends=True) + [""]:
        line_number += 1
        if line.strip():
            if start is None:
                start, line_start = offset, line_number
        elif start is not None:
            raw = content[start:offset]
            blocks = sorted(set(re.findall(r'(?:block[-_]id|\bid)\s*[=:]\s*["\x27]?([A-Za-z0-9_-]+)', raw)))
            paragraphs.append({"id": "p%06d" % (len(paragraphs) + 1), "text": raw,
                "text_hash": digest(raw), "content_hash": content_hash, "char_start": start,
                "char_end": offset, "line_start": line_start, "line_end": line_number - 1, "block_ids": blocks})
            start = None
        offset += len(line)
    links = sorted(set(re.findall(r'https?://[^\s<>"\x27)\]]+', content)))
    external = [link for link in links if not any((urlparse(link).hostname or "") == domain or
        (urlparse(link).hostname or "").endswith("." + domain) for domain in LARK_DOMAINS)]
    reference_map = (document or {}).get("reference_map", {})
    media_references = sum(len(value) for key, value in reference_map.items()
        if key in {"image", "img", "file", "video", "audio", "whiteboard", "media"} and isinstance(value, dict)) if isinstance(reference_map, dict) else 0
    return {"schema_version": 1, "content_hash": content_hash, "method": "lossless_paragraph_index",
        "semantic_distillation": False, "paragraphs": paragraphs,
        "counts": {"paragraphs": len(paragraphs), "characters": len(content),
            "media_markup": len(re.findall(r'!\[[^\]]*\]\(|<(?:img|image|video|audio|file|whiteboard|source)\b', content)),
            "media_references": media_references, "links": len(links), "external_links": len(external)},
        "external_links": external, "media_status": "not_inspected"}


def _active(state):
    enabled = set(state["active_sources"])
    return [doc for doc in state["documents"].values() if any(loc["source_id"] in enabled for loc in doc["locations"])]


def _maintenance_copy(doc, existing):
    """仅显式复用时查已采集快照；匹配对象/节点 token，不按相似标题猜测。"""
    expected = {doc.get("obj_token"), doc.get("target")}
    expected.update(location.get("node_token") for location in doc["locations"])
    expected.discard(None)
    candidates = []
    for source_id, record in existing.get("sources", {}).items():
        if record.get("status") != "ready" or not record.get("snapshot"):
            continue
        source = record.get("source", {})
        tokens = {source.get("obj_token"), source.get("token"), source.get("node_token")}
        if source.get("url"):
            parts = urlparse(source["url"]).path.strip("/").split("/")
            if len(parts) == 2:
                tokens.add(parts[1])
        tokens.discard(None)
        if expected & tokens:
            candidates.append((record.get("checked_at", ""), source_id, record))
    return max(candidates, default=None)


def _fetch_one(root, doc, maintenance_root, reuse_state=None):
    reused = _maintenance_copy(doc, reuse_state) if reuse_state and doc["kind"] != "local" else None
    if reused:
        _, reused_id, record = reused
        original = read_json(maintenance_root / record["snapshot"])
        content = original.get("content")
        if not isinstance(content, str) or digest(content) != record.get("content_hash"):
            raise maintenance.EnvelopeError("已采集快照的正文哈希不匹配")
        envelope = original.get("envelope") or {"ok": True, "data": {"document": {"content": content}}}
        data = envelope.get("data", envelope)
        document = data.get("document", {})
        doc["reused_maintenance_source"] = reused_id
    elif doc["kind"] == "local":
        content, envelope, document = _local_content(doc["path"])
    else:
        data = lark(["docs", "+fetch", "--doc", doc["target"], "--doc-format", "markdown", "--detail", "with-ids"])
        envelope, document = _envelope(data), data.get("document")
        content = document.get("content") if isinstance(document, dict) else None
    raw = _raw(root, "documents", doc["id"], envelope)
    doc["last_response"] = raw
    # 空正文不能替换旧版；完整原响应仍然在私有 raw 目录中。
    if not isinstance(content, str) or not content.strip():
        raise maintenance.EnvelopeError("来源正文缺失或为空")
    content_hash = digest(content)
    version_id = digest(content_hash + raw["sha256"])
    relative = "documents/" + doc["id"] + "/" + version_id
    snapshot = {"schema_version": 1, "document_id": doc["id"], "content_hash": content_hash,
        "content": content, "raw_response": raw, "locations": copy.deepcopy(doc["locations"]),
        "fetched_at": maintenance.now(), "revision_id": (document or {}).get("revision_id")}
    write_json(root / relative / "body.json", snapshot)
    doc["body_status"] = "ready"
    doc["pending_body"] = relative + "/body.json"
    return relative, snapshot, document


def fetch(workspace, limit=None, refresh=False, reuse_maintenance=False):
    if limit is not None and (not isinstance(limit, int) or limit < 1):
        raise maintenance.MaintenanceError("limit 必须为正整数")
    root = _root(workspace)
    with _locked(root):
        state = _state(root)
        reuse_state = maintenance.state_for(root.parent) if reuse_maintenance else None
        documents = _active(state)
        if refresh and not state.get("refresh_pending"):
            state["refresh_pending"] = [doc["id"] for doc in documents if doc["supported"]]
            for doc in documents:
                if doc["supported"]:
                    doc["fetch_status"] = "pending"
        attempted, failures = 0, []
        _save(root, state)
        for doc in documents:
            if not doc["supported"] or doc["fetch_status"] == "complete":
                continue
            if limit is not None and attempted >= limit:
                break
            attempted += 1
            try:
                # 正文写入后若归一化被中断，从私有快照继续，不重复请求云端。
                if doc.get("pending_body"):
                    snapshot = read_json(root / doc["pending_body"])
                    relative = str(Path(doc["pending_body"]).parent)
                    envelope = read_json(root / snapshot["raw_response"]["path"])
                    data = envelope.get("data", envelope)
                    document = data.get("document", data)
                else:
                    relative, snapshot, document = _fetch_one(root, doc, root.parent, reuse_state)
                    _save(root, state)
                envelope = read_json(root / snapshot["raw_response"]["path"])
                compatibility_source = {"id": doc["id"], "kind": doc["kind"], "enabled": True,
                    "title": doc["title"], "token": doc.get("target"), "obj_token": doc.get("obj_token"),
                    "path": doc.get("path"), "corpus_document_id": doc["id"]}
                maintenance.save_source(root.parent, compatibility_source, snapshot["content"], envelope)
                doc["maintenance_source_id"] = doc["id"]
                normalized = normalize(snapshot["content"], document)
                write_json(root / relative / "normalized.json", normalized)
                current = {"content_hash": snapshot["content_hash"], "body": relative + "/body.json",
                    "normalized": relative + "/normalized.json", "raw_response": snapshot["raw_response"],
                    "counts": normalized["counts"]}
                if doc.get("current") and doc["current"] != current:
                    doc["versions"].append(doc["current"])
                review_matches = doc.get("text_review", {}).get("content_hash") == current["content_hash"]
                if not review_matches:
                    doc.pop("text_review", None)
                doc.update({"current": current, "fetch_status": "complete", "body_status": "ready",
                    "normalization_status": "ready", "distillation_status": "reviewed" if review_matches else "pending",
                    "checked_at": maintenance.now()})
                doc.pop("pending_body", None)
                doc.pop("error", None)
                if doc["id"] in state.get("refresh_pending", []):
                    state["refresh_pending"].remove(doc["id"])
            except (maintenance.MaintenanceError, OSError, ValueError) as error:
                doc["fetch_status"] = "error"
                doc["error"] = {"kind": type(error).__name__, "at": maintenance.now()}
                failures.append({"document_id": doc["id"], "reason": type(error).__name__})
            _save(root, state)
        result = _summary(state)
        result.update({"ok": not failures, "attempted": attempted, "failures": failures})
        return result


def _summary(state):
    documents = _active(state)
    enabled = set(state["active_sources"])
    sources = [value for ident, value in state["sources"].items() if ident in enabled]
    return {"sources": len(sources), "inventory_complete": sum(s.get("status") == "complete" for s in sources),
        "discovered": len(documents), "body_ready": sum(d["body_status"] == "ready" for d in documents),
        "normalized": sum(d["normalization_status"] == "ready" for d in documents),
        "pending_distillation": sum(d["distillation_status"] == "pending" for d in documents),
        "text_reviewed": sum(d["distillation_status"] == "reviewed" for d in documents),
        "distilled": sum(d.get("text_review", {}).get("decision") == "integrated" for d in documents),
        "needs_format": sum(not d["supported"] for d in documents),
        "fetch_pending": sum(d["supported"] and d["fetch_status"] != "complete" for d in documents),
        "fetch_errors": sum(d["fetch_status"] == "error" for d in documents),
        "format_types": sorted({d["obj_type"] for d in documents if not d["supported"]})}


def status(workspace):
    root = _root(workspace)
    with _locked(root):
        return _summary(_state(root))


def _review_modules(value, skill):
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise maintenance.MaintenanceError("覆盖模块必须为相对路径数组")
    for module in value:
        try:
            relative = Path(safe_relative(module))
        except ValueError:
            raise maintenance.MaintenanceError("覆盖模块路径不合法") from None
        if relative.suffix != ".md" or not (
                relative.parts[0] == "references" or module == "knowledge/METHODS.md"):
            raise maintenance.MaintenanceError("覆盖模块只允许 references 下的 Markdown 或 knowledge/METHODS.md")
        path = skill
        for part in relative.parts:
            path = path / part
            if path.is_symlink():
                raise maintenance.MaintenanceError("覆盖模块及其目录不能是符号链接")
        if not path.is_file():
            raise maintenance.MaintenanceError("覆盖模块不存在或不是普通文件")
    return list(value)


def record_text_review(report, workspace, skill):
    """记录已经完成的语义审阅，不代替阅读、判定或知识合入。"""
    data = read_json(report)
    entries = data.get("documents") if isinstance(data, dict) else None
    if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(entries, list) or not entries:
        raise maintenance.MaintenanceError("审阅报告需要非空 documents")
    skill = Path(skill).resolve()
    known = {x["id"] for x in validate_catalog(skill)["items"]}
    root = _root(workspace)
    with _locked(root):
        state = _state(root)
        active = {d["id"]: d for d in _active(state)}
        checked, seen = [], set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise maintenance.MaintenanceError("审阅条目必须为对象")
            ident = entry.get("document_id")
            if ident not in active or ident in seen:
                raise maintenance.MaintenanceError("审阅对象不在当前来源中或重复")
            seen.add(ident)
            doc = active[ident]
            current = doc.get("current", {})
            if doc.get("fetch_status") != "complete" or not current or entry.get("content_hash") != current["content_hash"]:
                raise maintenance.MaintenanceError("正文未完成或审阅对应的原文已改变，不能沿用旧结论")
            body = read_json(root / current["body"])
            if digest(body["content"]) != current["content_hash"]:
                raise maintenance.MaintenanceError("正文快照完整性核验失败")
            if entry.get("text_read_complete") is not True:
                raise maintenance.MaintenanceError("部分阅读不能记录为全文语义审阅完成")
            for key in ("reviewer", "reading_evidence", "reason", "media_boundary"):
                if not isinstance(entry.get(key), str) or not entry[key].strip():
                    raise maintenance.MaintenanceError("审阅记录缺少 " + key)
            decision, ids = entry.get("decision"), entry.get("knowledge_ids")
            if decision not in {"integrated", "covered", "not_applicable"}:
                raise maintenance.MaintenanceError("审阅决定应为 integrated、covered 或 not_applicable")
            if not isinstance(ids, list) or not all(isinstance(x, str) and x in known for x in ids):
                raise maintenance.MaintenanceError("审阅记录关联了未合入的知识")
            modules = _review_modules(entry.get("covered_modules", []), skill)
            if decision == "integrated" and not ids:
                raise maintenance.MaintenanceError("合入知识必须关联知识卡")
            if decision == "covered" and not (ids or modules):
                raise maintenance.MaintenanceError("已覆盖需关联知识卡或模块")
            if decision == "not_applicable" and (ids or modules):
                raise maintenance.MaintenanceError("不适用不能声明知识卡或模块覆盖")
            checked.append((doc, dict(copy.deepcopy(entry), covered_modules=modules)))
        # 整批校验后再推进状态，避免一份过期报告导致前半批被标记完成。
        for doc, entry in checked:
            entry["recorded_at"] = maintenance.now()
            record_hash = digest(maintenance.canonical(entry))
            # 同一原文可以复审。新记录按完整内容寻址，不能先覆盖旧 state
            # 正在引用的文件；提交中断时最多留下未引用的新文件。
            relative = "reviews/" + doc["id"] + "/" + entry["content_hash"] + "/" + record_hash + ".json"
            record_path = root / relative
            if record_path.exists():
                if digest(maintenance.canonical(read_json(record_path))) != record_hash:
                    raise maintenance.MaintenanceError("已有审阅快照完整性核验失败")
            else:
                write_json(record_path, entry)
            doc["text_review"] = {"content_hash": entry["content_hash"], "decision": entry["decision"],
                "knowledge_ids": entry["knowledge_ids"], "covered_modules": entry["covered_modules"],
                "record": relative,
                "record_hash": record_hash}
            doc["distillation_status"] = "reviewed"
        _save(root, state)
        return dict(_summary(state), recorded=len(checked), ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("inventory", help="盘点显式来源，失败时从上次成功页续跑")
    scan.add_argument("--registry", type=Path, required=True)
    scan.add_argument("--workspace", type=Path, default=Path(".maintenance"))
    scan.add_argument("--refresh", action="store_true", help="显式重新盘点完成的来源")
    read = commands.add_parser("fetch", help="只采集未完成正文，保留全文与完整原响应")
    read.add_argument("--workspace", type=Path, default=Path(".maintenance"))
    read.add_argument("--limit", type=int)
    read.add_argument("--refresh", action="store_true", help="开启已完成正文的新一轮刷新；未完成轮次继续续跑")
    read.add_argument("--reuse-maintenance", action="store_true", help="显式复用维护快照中匹配对象/节点 token 的已采集正文")
    check = commands.add_parser("status", help="分别查看发现、正文、归一化及待提炼数量")
    check.add_argument("--workspace", type=Path, default=Path(".maintenance"))
    review = commands.add_parser("review", help="登记已完成的全文语义审阅；校验原文版本与合入知识")
    review.add_argument("--report", type=Path, required=True)
    review.add_argument("--workspace", type=Path, default=Path(".maintenance"))
    review.add_argument("--skill-dir", type=Path, default=Path("skills/wenwen-zhentao"))
    args = parser.parse_args(argv)
    try:
        if args.command == "inventory":
            result = inventory(args.registry, args.workspace, args.refresh)
        elif args.command == "fetch":
            result = fetch(args.workspace, args.limit, args.refresh, args.reuse_maintenance)
        elif args.command == "review":
            result = record_text_review(args.report, args.workspace, args.skill_dir)
        else:
            result = status(args.workspace)
    except (maintenance.MaintenanceError, OSError, ValueError) as error:
        print(json.dumps({"ok": False, "reason": type(error).__name__, "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
