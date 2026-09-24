#!/usr/bin/env python3
"""离线检索知识目录；不联网，不保存用户提问。Python 3.9+。"""
import argparse
import json
import math
import re
from pathlib import Path


def terms(text):
    text = text.lower()
    result = set(re.findall(r"[a-z0-9]+", text))
    for span in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(span) == 1:
            result.add(span)
        else:
            result.update(span[i:i + 2] for i in range(len(span) - 1))
    return result


def search(query, root, limit=5):
    catalog = json.loads((root / "knowledge/catalog.json").read_text(encoding="utf-8"))
    items = catalog["items"]
    query_terms = terms(query)
    if not query_terms:
        return []
    fields = []
    for item in items:
        title = item["question"] + " " + " ".join(item.get("aliases", []))
        fields.append((terms(title), terms(" ".join(item.get("tags", []))),
                       terms(item.get("summary", ""))))
    freq = {term: sum(term in a | b | c for a, b, c in fields) for term in query_terms}
    results = []
    for item, (title, tags, summary) in zip(items, fields):
        overlap = query_terms & (title | tags | summary)
        if not overlap:
            continue
        score = sum((3 if t in title else 2 if t in tags else 1) *
                    (1 + math.log((1 + len(items)) / (1 + freq[t]))) for t in overlap)
        score /= math.sqrt(max(len(title), 4))
        rel = item["path"]
        path = (root / rel).resolve()
        if root.resolve() not in path.parents or not path.is_file():
            continue
        results.append({k: item.get(k) for k in
                        ["id", "question", "path", "summary", "applicable_when", "limitations",
                         ]} | {"score": round(score, 3)})
    return sorted(results, key=lambda x: (-x["score"], x["id"]))[:limit]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--skill-dir", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    if not 1 <= args.limit <= 20:
        parser.error("limit 必须在 1—20 之间")
    try:
        results = search(args.query, args.skill_dir, args.limit)
        print(json.dumps({"ok": True, "results": results,
                          "note": "分数只用于排序；命中后须读卡片正文，未命中不等于没有答案。"},
                         ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__,
                          "hint": "检索暂不可用，请读取 knowledge/INDEX.md 继续定位。"}, ensure_ascii=False))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
