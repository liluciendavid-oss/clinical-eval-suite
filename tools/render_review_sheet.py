#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
render_review_sheet.py — 把 questions.jsonl 渲染成给人读的评审稿

为什么需要它
------------
题库的机器可读格式（JSONL）对评分脚本友好，但对**临床评审者**极不友好。
而本项目的关键质量门是"用户本人逐题评审临床正确性"——如果评审要读 JSONL，
评审质量必然下降，这个门就形同虚设。

用法
----
    python tools/render_review_sheet.py                 # 输出 docs/_review_sheet.md
    python tools/render_review_sheet.py --only SIRT-007,TACE-003
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    import console
except ImportError:
    class console:  # type: ignore
        OK = "[OK]"; WARN = "[!]"; FAIL = "[x]"; ARROW = "[->]"; BULLET = "-"
        @staticmethod
        def setup() -> None: ...

console.setup()

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "questions.jsonl"
OUT = ROOT / "docs" / "_review_sheet.md"

SEV_ORDER = {"critical": 0, "high": 1, "moderate": 2}
SEV_ZH = {"critical": "致命", "high": "高", "moderate": "中"}


def render(q: dict) -> str:
    L: list[str] = []
    tags = "、".join(q.get("capability_tags") or [])
    focus = "、".join(q.get("judge_focus") or [])
    L.append(f"## {q['qid']}｜{q.get('domain')}｜{q.get('task_type')}")
    L.append("")
    L.append(f"**难度 {q.get('difficulty')}/4　污染风险 {q.get('contamination_risk')}"
             f"　能力标签 {tags}　聚焦维度 {focus}"
             f"{'　[格式探针子集]' if q.get('format_probe') else ''}**")
    L.append("")
    L.append("### 题干")
    L.append("")
    L.append(q.get("stem", ""))
    L.append("")
    L.append("### 提问")
    L.append("")
    L.append("> " + q.get("question", ""))
    L.append("")
    L.append("### 必要推理链（D2 的 n_gap 按此计数）")
    L.append("")
    for i, s in enumerate(q.get("required_chain") or [], 1):
        L.append(f"{i}. {s}")
    L.append("")
    L.append("### 标准答案要点（key points）")
    L.append("")
    for kp in q.get("key_points") or []:
        star = "**必答**" if kp.get("must_have") else "加分"
        L.append(f"- [{star} w{kp.get('weight')}] {kp.get('text')}")
    L.append("")
    L.append("### 安全红旗（red flags，按严重度排序）")
    L.append("")
    L.append("> **D3 判定规则**：`[mention]` 型——提到该实体且作出正确处置 = `hit`；只提到未处置 = `partial`；未提 = `miss`。"
              "`[forbid]` 型——hint 描述的是**不许说出的错误结论**，按二值判：未作出该表述 = `hit`；作出 = `miss`；**无 `partial`**。"
              "看结论方向不看措辞。详见 `docs/rubric.md` §2·D3。")
    L.append("")
    for i, rf in enumerate(sorted(q.get("red_flags") or [], key=lambda r: SEV_ORDER.get(r.get("severity"), 9)), 1):
        pol = rf.get("hint_polarity") or "未标注"
        L.append(f"- **RF{i}｜[{SEV_ZH.get(rf.get('severity'), rf.get('severity'))}｜应表述为{rf.get('expected_modality')}｜{pol}]** {rf.get('text')}")
    L.append("")
    L.append("### 可接受的合理分歧（D4 的判据）")
    L.append("")
    L.append(q.get("acceptable_divergence", ""))
    L.append("")
    L.append("### 明确错误（命中即整体分 0）")
    L.append("")
    for u in q.get("unacceptable") or []:
        L.append(f"- {u}")
    L.append("")
    L.append("### 这道题在考什么")
    L.append("")
    L.append(q.get("author_note", ""))
    L.append("")
    L.append("### 依据来源")
    L.append("")
    for r in q.get("reference_basis") or []:
        L.append(f"- {r}")
    L.append("")
    L.append("---")
    L.append("")
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="逗号分隔的 qid，只渲染指定题")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args(argv)

    if not SRC.exists():
        sys.exit(f"[ERROR] 题库不存在：{SRC}")
    qs = [json.loads(l) for l in SRC.read_text(encoding="utf-8").splitlines() if l.strip()]
    if args.only:
        want = {x.strip() for x in args.only.split(",") if x.strip()}
        qs = [q for q in qs if q["qid"] in want]
    if not qs:
        sys.exit("[ERROR] 没有匹配的题")

    head = [
        "# 题库评审稿（供临床评审，非交付物）",
        "",
        f"**共 {len(qs)} 题　生成自 data/questions.jsonl　评审方式：逐题核对「标准答案要点 / 安全红旗 / 合理分歧 / 明确错误」四组字段**",
        "",
        "> 评审时请特别关注三件事：① 数值与阈值是否与你的临床经验和最新指南一致；",
        "> ② `合理分歧` 是否写得太宽（会放过错误答案）或太窄（会把合法分歧判错）；",
        "> ③ `明确错误` 里有没有其实可以接受的表述。",
        "> 任何一处不同意，直接告诉我 qid + 字段 + 你的改法，我来改题库并重跑校验。",
        "",
    ]
    body = "\n".join(render(q) for q in qs)
    out = Path(args.out)
    out.write_text("\n".join(head) + "\n" + body, encoding="utf-8")
    print(f"{console.OK} 评审稿 -> {out.relative_to(ROOT)}（{len(qs)} 题）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
