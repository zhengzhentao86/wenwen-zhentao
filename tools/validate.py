#!/usr/bin/env python3
"""验证 Skill 结构、相对引用、知识文件及发布边界，不代表答疑质量评分。"""
import argparse
import json
import re
from collections import Counter
from pathlib import Path
from wwzt_core import SKILL, package_privacy_issues, validate_catalog


def validate(root):
    root = Path(root)
    catalog = validate_catalog(root)
    entry = (root / "SKILL.md").read_text(encoding="utf-8")
    if not entry.startswith("---\n") or f"name: {SKILL}" not in entry.split("---", 2)[1]:
        raise ValueError("入口 frontmatter 不合法")
    errors = []
    for md in root.rglob("*.md"):
        body = md.read_text(encoding="utf-8")
        for target in re.findall(r"\]\(([^)]+)\)", body):
            if "://" in target or target.startswith("#"):
                continue
            path = target.split("#", 1)[0]
            if not (md.parent / path).is_file():
                errors.append({"file": str(md.relative_to(root)), "missing": path})
    errors += package_privacy_issues(root)
    if errors:
        raise ValueError(json.dumps(errors, ensure_ascii=False))
    return {"ok": True, "knowledge_count": len(catalog["items"]),
            "categories": dict(Counter(x["category"] for x in catalog["items"])),
            "catalog_schema": catalog['schema_version'],
            "public_status": dict(Counter(x.get("public_status", "maintained_outside_package") for x in catalog["items"])),
            "entry_lines": len(entry.splitlines()), "answer_quality_tested": False}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--skill-dir", type=Path, default=Path("skills") / SKILL)
    args = p.parse_args()
    try:
        print(json.dumps(validate(args.skill_dir), ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError) as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        raise SystemExit(1)
