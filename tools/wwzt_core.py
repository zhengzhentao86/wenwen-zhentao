"""维护端共享校验；不随答疑 Skill 分发。"""
import hashlib
import json
import re
from pathlib import Path, PurePosixPath

SKILL = "wenwen-zhentao"
CATEGORIES = {"tools", "prompts", "visual", "workflow", "tables"}
PUBLIC_STATUS = {"pending_owner_review", "approved", "private"}
RUNTIME_FIELDS = {"id", "question", "aliases", "category", "path", "summary",
                  "applicable_when", "limitations", "tags", "knowledge_version"}
RUNTIME_REQUIRED = RUNTIME_FIELDS - {"knowledge_version"}


def runtime_item(item):
    """学员索引只保留运行字段；来源与审核字段留在维护候选及快照。"""
    return {key: value for key, value in item.items() if key in RUNTIME_FIELDS}


def validate_runtime_text(content):
    if re.search(r"来源说明|来源性质|来源边界|AI 补充说明|课程原话|课程逐字|待批次确认|待确认公开|原编者|原始发布日期", content):
        raise ValueError("运行知识仍含来源或维护说明，请先在候选中移出并保留私人记录")


def runtime_content(content):
    """只移出明确的维护章节；操作条件和限制保留，内联归属需编辑后再校验。"""
    body = re.sub(r'\n## (?:来源说明|AI 补充说明)\n.*?(?=\n## |\Z)', '', content, flags=re.S)
    validate_runtime_text(body)
    return body.rstrip() + '\n'


def digest(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def safe_relative(value):
    path = PurePosixPath(value)
    if not value or "\\" in value or path.is_absolute() or any(p in {".", ".."} for p in value.split("/")):
        raise ValueError("非法相对路径")
    if ":" in value or "\x00" in value:
        raise ValueError("非法路径字符")
    return str(path)


def validate_item(item, runtime=False):
    required = {"id", "question", "aliases", "category", "path", "summary", "source_label", "source_date",
                "verified_at", "evidence_type", "public_status", "applicable_when", "limitations", "tags"}
    if runtime:
        required = RUNTIME_REQUIRED
        if set(item) - RUNTIME_FIELDS:
            raise ValueError("运行知识索引含非运行字段，来源和审核信息须保存在包外")
    if not required <= item.keys():
        raise ValueError("知识卡缺少字段：" + ",".join(sorted(required - item.keys())))
    if not re.fullmatch(r"WWZT-\d{3,6}", item["id"]):
        raise ValueError("知识 ID 应为 WWZT-数字")
    if item["path"] != "knowledge/" + item["id"] + ".md":
        raise ValueError("知识路径必须对应稳定 ID")
    if item["category"] not in CATEGORIES or (not runtime and item["public_status"] not in PUBLIC_STATUS):
        raise ValueError("分类或公开状态不合法")
    for key in (["question", "summary", "applicable_when", "limitations"] if runtime else ["question", "summary", "source_label", "evidence_type", "applicable_when", "limitations"]):
        if not isinstance(item[key], str) or not item[key].strip():
            raise ValueError(key + " 需要非空文字")
    for key in ["aliases", "tags"]:
        if not isinstance(item[key], list) or not all(isinstance(x, str) for x in item[key]):
            raise ValueError(key + " 必须为文字数组")
    for key in ([] if runtime else ["verified_at", "source_date"]):
        if item[key] is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", item[key]):
            raise ValueError(key + " 日期格式错误")


def validate_catalog(root):
    catalog = read_json(root / "knowledge/catalog.json")
    if catalog.get("schema_version") not in {1, 2} or not isinstance(catalog.get("items"), list):
        raise ValueError("catalog 版本或结构错误")
    seen = set()
    for item in catalog["items"]:
        validate_item(item, runtime=catalog['schema_version'] == 2)
        if item["id"] in seen:
            raise ValueError("重复知识 ID")
        seen.add(item["id"])
        path = root / safe_relative(item["path"])
        if path.is_symlink() or not path.is_file():
            raise ValueError("知识文件缺失或是符号链接：" + item["id"])
        if len(path.read_text(encoding="utf-8").strip()) < 40:
            raise ValueError("知识卡正文过短：" + item["id"])
        if catalog['schema_version'] == 2:
            validate_runtime_text(path.read_text(encoding="utf-8"))
    if not seen:
        raise ValueError("知识目录为空")
    return catalog


def fingerprint(root):
    files = {}
    for p in sorted(Path(root).rglob("*")):
        if p.is_symlink():
            raise ValueError("不允许符号链接")
        if p.is_file() and "__pycache__" not in p.parts:
            files[p.relative_to(root).as_posix()] = digest(p.read_bytes())
    return digest(json.dumps(files, sort_keys=True)), files


def package_privacy_issues(root):
    """机械扫描只是防误带入，不能替代内容归属和公开许可审阅。"""
    patterns = {
        "本机绝对路径": r"/(?:Users|home)/[^\s\"<>]+",
        "飞书私有入口": r"https?://[^\s/]*(?:feishu\.cn|larkoffice\.com|larksuite\.com)/(?:wiki|docx|base|sheets)/",
        "手机号": r"(?<!\d)1[3-9]\d{9}(?!\d)",
        "疑似凭据": r"(?:sk-[A-Za-z0-9_-]{20,}|Bearer\s+[A-Za-z0-9._-]{20,}|authcode=)",
    }
    findings = []
    # 只扫描会承载业务内容的文本；脚本中的示例匹配式不当成泄密。
    for p in Path(root).rglob("*"):
        if p.is_file() and p.suffix in {".md", ".json", ".yaml"}:
            content = p.read_text(encoding="utf-8")
            for label, pattern in patterns.items():
                if re.search(pattern, content):
                    findings.append({"file": p.relative_to(root).as_posix(), "kind": label})
    return findings
