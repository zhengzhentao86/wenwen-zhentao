#!/usr/bin/env python3
"""在隔离目录运行本机已安装宿主；只采集回答，不自动给质量打分。"""
import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from wwzt_core import SKILL, fingerprint, write_json


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", choices=["codex", "claude"], required=True)
    p.add_argument("--skill-dir", type=Path, default=Path("skills") / SKILL)
    p.add_argument("--entry", choices=[SKILL, "zt"], default=SKILL,
                   help="实际调用的 Skill 入口；zt 需配合 --companion-skill skills/zt")
    p.add_argument("--companion-skill", type=Path, action="append", default=[],
                   help="一并复制到隔离安装目录的 Skill，可重复；不会安装到用户全局目录")
    p.add_argument("--prompts", type=Path, default=Path("evals/prompts.json"))
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--claude-cli", type=Path, help="可选：直接使用本机 Claude Code cli.js，避开第三方启动器")
    p.add_argument("--codex-cli", type=Path, help="可选：使用桌面应用内已安装的 Codex CLI")
    p.add_argument("--timeout", type=int, default=420)
    p.add_argument("--limit", type=int, default=0)
    args = p.parse_args()
    prompts = json.loads(args.prompts.read_text(encoding="utf-8"))
    if args.limit:
        prompts = prompts[:args.limit]
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    fp, _ = fingerprint(args.skill_dir)
    prompt = f"请调用当前项目中的 ${args.entry} Skill 处理以下独立情境。\n" + """
先读取安装目录内的 SKILL.md，按需读取相关知识卡。每题当作一个独立学员的首次提问，不把上一题的背景带到下一题。
只解答，不做维护或发布，不修改文件，不联网，不访问当前隔离目录之外的资料，不启动服务。联网或媒体限制以题内给定条件为准。
这是回答采集，不需要你评分、解释评测流程或写代码。最终只输出一个 JSON 对象：{"answers":[{"id":"题号","answer":"给学员的实际中文答复"}]}。按正常学员答复，不显示内部来源或知识卡编号。
题目：\n""" + json.dumps(prompts, ensure_ascii=False)
    with tempfile.TemporaryDirectory(prefix="wwzt-host-eval-") as tmp:
        work = Path(tmp)
        skill_home = work / (".agents/skills" if args.host == "codex" else ".claude/skills")
        sources = {SKILL: args.skill_dir}
        for source in args.companion_skill:
            name = source.name
            if name in sources or not re.fullmatch(r"[a-z0-9-]+", name):
                p.error("重复或非法的陪测 Skill 名称：" + name)
            sources[name] = source
        if args.entry not in sources:
            p.error("指定入口未包含在隔离安装集合中")
        companion_fingerprints = {}
        for name, source in sources.items():
            target = skill_home / name
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git", ".DS_Store"))
            if name != SKILL:
                companion_fingerprints[name] = fingerprint(target)[0]
        write_json(work / "questions.json", prompts)
        if args.host == "codex":
            command = [str(args.codex_cli.resolve()) if args.codex_cli else "codex", "exec", "--ephemeral", "--sandbox", "read-only", "--skip-git-repo-check",
                       "-c", 'approval_policy="never"', "--color", "never", "--json",
                       "--output-last-message", str(out / "answer.txt"), "-"]
            # 只提取配置中的 MCP 名称，关闭本次测试的外部连接，不输出配置正文。
            config = Path.home() / ".codex/config.toml"
            if config.exists():
                for name in re.findall(r"^\[mcp_servers\.([A-Za-z0-9_-]+)\]", config.read_text(), re.M):
                    command[2:2] = ["-c", "mcp_servers." + name + ".enabled=false"]
        else:
            command = (["node", str(args.claude_cli.resolve())] if args.claude_cli else ["claude"]) + [
                "--print", "--no-session-persistence", "--permission-mode", "dontAsk", "--no-chrome",
                "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                "--settings", '{"disableAllHooks":true}', "--tools", "Read,Grep,Glob,Skill",
                "--allowedTools", "Read,Grep,Glob,Skill", "--output-format", "json"]
        try:
            result = subprocess.run(command, input=prompt, cwd=work, text=True, capture_output=True,
                                    timeout=args.timeout, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
            (out / "stdout.log").write_text(result.stdout, encoding="utf-8")
            (out / "stderr.log").write_text(result.stderr, encoding="utf-8")
            if args.host == "claude" and result.returncode == 0:
                envelope = json.loads(result.stdout)
                (out / "answer.txt").write_text(envelope.get("result", ""), encoding="utf-8")
            raw = (out / "answer.txt").read_text(encoding="utf-8") if (out / "answer.txt").exists() else ""
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
            try:
                answers = json.loads(raw)
                write_json(out / "answers.json", answers)
                ids = [x["id"] for x in answers.get("answers", [])]
                complete = result.returncode == 0 and len(ids) == len(set(ids)) and set(ids) == {x["id"] for x in prompts}
            except (ValueError, KeyError, TypeError):
                complete = False
            status = {"host": args.host, "exit_code": result.returncode, "responses_complete": complete,
                      "prompt_count": len(prompts), "skill_fingerprint": fp, "evaluated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                      "quality_graded": False, "log_directory": str(out)}
        except subprocess.TimeoutExpired as exc:
            # 子进程已由 subprocess 终止并回收；保留已收到的输出供检查，不启动重试。
            for name, data in [("stdout.log", exc.stdout), ("stderr.log", exc.stderr)]:
                (out / name).write_bytes(data if isinstance(data, bytes) else (data or "").encode())
            status = {"host": args.host, "status": "timeout", "responses_complete": False,
                      "skill_fingerprint": fp, "quality_graded": False}
        except (OSError, ValueError) as exc:
            status = {"host": args.host, "status": type(exc).__name__, "responses_complete": False,
                      "skill_fingerprint": fp, "quality_graded": False}
        status["entry"] = args.entry
        status["companion_fingerprints"] = companion_fingerprints
        write_json(out / "run.json", status)
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0 if status.get("responses_complete") else 1


if __name__ == "__main__":
    raise SystemExit(main())
