#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_all.py — 一键跑完全部验收门（CI 入口）

为什么需要这个文件
------------------
本项目的每个组件都有自己的自检，但"每个自检都通过"和"整条链能跑通"是两件事。
这个脚本按依赖顺序跑完所有门，任何一门失败就返回非零——**让"交付前忘了跑某个
自检"这件事不可能发生**。

它不产生任何真实结论：所有数据都是 mock（缺陷注入）或 DEMO（合成打分），
全部验收门在一次性临时副本中运行；只复制程序、规范、测试夹具与题库，
不读取或复制正式回答、盲化映射、manifest、评分与统计结果。退出时清理临时产物。

用法
----
    python src/verify_all.py            # 跑全部门
    python src/verify_all.py --fast     # 跳过渲染（约省 30 秒）

退出码
------
    0  全部门通过
    1  有门失败（见末尾汇总表）

注意
----
`blind_shuffle.py --unblind` **不在**本脚本里。它依赖真实打分表，
且会把揭盲结果写进 data/scores/——那是打分完成之后的手动步骤，
提前跑只会用空模板生成一个误导性的 unblinded 文件。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import console

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def prepare_verification_project(source: Path, destination: Path) -> None:
    """复制明确允许的输入；绝不整目录复制 data 或正式结果。

    拒绝链接，避免 allowlist 内的路径间接指向正式数据。忽略 Python 缓存，
    确保子进程载入本次复制的源码。
    """
    def reject_link(src: Path) -> None:
        if src.is_symlink() or getattr(src.lstat(), "st_file_attributes", 0) & 0x400:
            raise ValueError(f"验收输入不能是符号链接或重解析点：{src}")

    def copy_input(src: Path, dst: Path) -> None:
        reject_link(src)
        if src.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
            for child in src.iterdir():
                if child.name != "__pycache__" and child.suffix != ".pyc":
                    copy_input(child, dst / child.name)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

    for relative in ("src", "docs", "prompts", "tests", "tools",
                     "data/questions.jsonl", "requirements.txt"):
        src = source / relative
        if src.exists():
            # data/questions.jsonl 的父目录也不能通过链接绕过 allowlist。
            parent = src.parent
            while parent != source:
                reject_link(parent)
                parent = parent.parent
            copy_input(src, destination / relative)


def run(name: str, argv: list[str], expect_in_stdout: str = "",
        allow_rc: set[int] | None = None, *, root: Path) -> tuple[bool, str]:
    """跑一个子命令。返回 (是否通过, 摘要)。

    allow_rc：这些退出码也算通过（用于"退出码承载数据质量信号"的脚本，
    例如 score_audit 用 1 表示存在 invalid 行）。
    """
    t0 = time.time()
    # 不能只改 cwd：各组件通过 __file__ 定位 ROOT，脚本本身也必须来自副本。
    command = [PY, str(root / argv[0]), *argv[1:]]
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["MPLCONFIGDIR"] = str(root / ".cache" / "matplotlib")
    p = subprocess.run(command, cwd=str(root), env=env,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    dt = time.time() - t0
    out = (p.stdout or "") + (p.stderr or "")
    ok = p.returncode in (allow_rc or {0})
    if expect_in_stdout and expect_in_stdout not in out:
        ok = False
        return False, f"{dt:.1f}s　退出码 {p.returncode}　缺期望输出「{expect_in_stdout}」"
    if not ok:
        tail = [l for l in out.splitlines() if l.strip()][-3:]
        return False, f"{dt:.1f}s　退出码 {p.returncode}　{' | '.join(tail)[:160]}"
    # 从输出里摘一行关键数字，让汇总表不只是"通过/失败"
    key = ""
    for pat in (r"ERROR (\d+) \| WARN (\d+)",
                r"confirmed 数：(\d+)",
                r"解析成功率 ([\d.]+%)",
                r"注入缺陷实例 (\d+) 个",
                r"用例全部按预期检出"):
        m = re.search(pat, out)
        if m:
            key = m.group(0)
            break
    return True, f"{dt:.1f}s　{key}"


GATES: list[tuple[str, list[str], str, set[int] | None]] = [
    ("题库校验（--strict：ERROR 0 且 WARN 0）",
     ["src/schema.py", "--strict"], "[OK] 校验通过", None),
    ("词典与 error_taxonomy.md 同步",
     ["src/dictionaries.py", "--check-sync"], "完全同步", None),
    ("缺陷注入器生成阳性/阴性对照",
     ["src/mock_models.py", "--clean"], "在最终文本中确实成立", None),
    ("规则层自检（recall>0 且阴性 confirmed=0 且无越权升级）",
     ["src/rule_check.py", "--self-test"], "规则层自检通过", None),
    ("评分质控器自检（19 个阳性用例）",
     ["src/score_audit.py", "--self-test"], "用例全部按预期检出", None),
    ("rubric 查表探针（无空洞、无重叠）",
     ["src/score_audit.py", "--rubric-probe"], "无空洞、无重叠", None),
    ("统计实现自检（α/κ/bootstrap 的已知答案夹具）",
     ["src/stats.py", "--self-test"], "统计实现自检通过", None),
    ("盲测流水线：生成 DEMO 回答",
     ["src/blind_shuffle.py", "--make-demo"], "写入", None),
    ("盲测流水线：匿名化 + block 隔离校验",
     ["src/blind_shuffle.py"], "同题不同 block 约束已校验通过", None),
    ("盲测流水线：重测包（不同 seed 与编号体系）",
     ["src/blind_shuffle.py", "--pass2", "--retest-questions", "8"],
     "同题不同 block 约束已校验通过", None),
    ("judge 逐项评分输入生成（pointwise，文件名匿名）",
     ["src/llm_judge.py", "--emit", "pointwise", "--source", "demo"], "份待粘贴输入", None),
    ("judge 面板外校验（同源模型必须被拒）",
     ["src/llm_judge.py", "--parse", "--judge-model", "DeepSeek-V3.2", "--source", "demo"],
     "与被评面板同源", {1}),
]

RENDER_GATES: list[tuple[str, list[str], str, set[int] | None]] = [
    ("报告渲染（DEMO，含 5 张图与 SUMMARY.md）",
     ["src/report.py", "--demo", "--full"], "报告 ->", None),
]


def main(argv: list[str] | None = None) -> int:
    console.setup()
    ap = argparse.ArgumentParser(description="一键跑完全部验收门")
    ap.add_argument("--fast", action="store_true", help="跳过报告渲染")
    args = ap.parse_args(argv)

    gates = list(GATES)
    if not args.fast:
        gates += RENDER_GATES

    print("verify_all.py — 全部验收门（临时副本内的 mock / DEMO，不产生任何真实结论）\n")
    results: list[tuple[str, bool, str]] = []
    with tempfile.TemporaryDirectory(prefix="clinical-eval-verify-") as directory:
        verification_root = Path(directory)
        prepare_verification_project(ROOT, verification_root)
        for name, argv, expect, allow in gates:
            print(f"[run] {name} …", flush=True)
            ok, msg = run(name, argv, expect, allow, root=verification_root)
            results.append((name, ok, msg))
            print(f"      {'PASS' if ok else 'FAIL'}　{msg}")

    print("\n" + "=" * 72)
    print("验收门汇总")
    print("=" * 72)
    for name, ok, msg in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    n_fail = sum(1 for _, ok, _ in results if not ok)
    print("-" * 72)
    if n_fail:
        print(f"{n_fail} 门失败。修组件，不要改验收标准。")
        return 1
    print(f"全部 {len(results)} 门通过。")
    print("临时验收产物已清理；正式评测进度与待办请查看 docs/release_status.md。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
