#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
report.py — 一键出表出图到 results/

它做的事
--------
把 rule_check / score_audit / stats 三层的产物汇总成一份**可交付的报告**：

  results/
  ├── SUMMARY.md            主报告：结论、两套排序、CI、局限，全部带数字
  ├── fig_capability_heatmap.png
  ├── fig_dimension_bars.png
  ├── fig_pairwise_ci.png
  ├── fig_error_codes.png
  ├── fig_length_bias.png
  └── tables/*.csv          图背后的原始表（图会骗人，表不会）

一键重跑
--------
`python src/report.py --full` 会依次跑规则层自检、打分质控、统计分析，再渲染。
不带 `--full` 时只读已有产物渲染，便于反复调图而不重算。

三条渲染纪律
------------
1. **门控前与门控后两套排序都要出现在 SUMMARY.md 里**（rubric §0 总则 3）。
   只报一套等于把安全否决的作用藏起来。
2. **每个胜率都必须带 CI**，且 CI 跨过 0.5 时明确写"不足以支撑占优表述"。
3. **confirmed 与 flag_for_review 分开画、分开数**（error_taxonomy.md §2.7）。
   混在一起报，就是用不确定性制造虚假的确定性。

跨平台字体
----------
图里的中文标签在缺 CJK 字体的机器上会变成方框。本脚本先探测可用字体，
探测不到就整体切换成英文标签——**宁可少一层信息，也不要交付一张满是方框的图**。

依赖：numpy、pandas、matplotlib。
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import console  # noqa: E402

console.setup()

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import matplotlib  # noqa: E402

matplotlib.use("Agg")           # 无显示环境（CI / ssh）也必须能出图
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
STATS_DIR = ROOT / "data" / "stats"
SCORES_DIR = ROOT / "data" / "scores"
RESULTS = ROOT / "results"
DIMS = ["D1", "D2", "D3", "D4", "D5", "D6"]
DIM_NAMES_ZH = {"D1": "事实准确", "D2": "推理链", "D3": "风险安全",
                "D4": "不确定性", "D5": "可用性", "D6": "整体可采纳"}
DIM_NAMES_EN = {"D1": "Factual", "D2": "Reasoning", "D3": "Safety",
                "D4": "Uncertainty", "D5": "Usability", "D6": "Adoptability"}
REPORT_VERSION = "1.0"


# =====================================================================
# 字体探测
# =====================================================================
CJK_CANDIDATES = ["Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "Source Han Sans SC",
                  "PingFang SC", "Hiragino Sans GB", "WenQuanYi Zen Hei", "Arial Unicode MS"]


def pick_cjk_font() -> tuple[str | None, bool]:
    """返回 (字体名, 是否支持中文)。不支持时全图切英文标签。"""
    avail = {f.name for f in font_manager.fontManager.ttflist}
    for name in CJK_CANDIDATES:
        if name in avail:
            plt.rcParams["font.sans-serif"] = [name, "DejaVu Sans"]
            plt.rcParams["axes.unicode_minus"] = False
            return name, True
    plt.rcParams["font.sans-serif"] = ["DejaVu Sans"]
    return None, False


CJK_OK = False
FONT_NAME = None


def L(zh: str, en: str) -> str:
    """按字体可用性选中英文标签。"""
    return zh if CJK_OK else en


# =====================================================================
# 读产物
# =====================================================================
def _load_json(p: Path):
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"{console.WARN} {p.name} 解析失败：{e}", file=sys.stderr)
        return None


def _load_csv(p: Path) -> pd.DataFrame | None:
    if not p.exists():
        return None
    try:
        return pd.read_csv(p, encoding="utf-8-sig")
    except Exception as e:  # pandas 各版本异常类型不一
        print(f"{console.WARN} {p.name} 读取失败：{e}", file=sys.stderr)
        return None


def gather(demo: bool) -> dict:
    base = STATS_DIR / "demo" if demo else STATS_DIR
    scores = SCORES_DIR / "demo" if demo else SCORES_DIR
    data = {
        "demo": demo,
        "stats_dir": base,
        "pairwise": _load_json(base / "pairwise.json"),
        "dimensions": _load_csv(base / "dimensions.csv"),
        "heatmap": _load_csv(base / "capability_heatmap.csv"),
        "length": _load_json(base / "length_bias.json"),
        "reliability": _load_json(base / "reliability.json"),
        "judge": _load_json(base / "judge_agreement.json"),
        "rule_vs_human": _load_json(base / "rule_vs_human.json"),
        "rule_findings": None,
        "audit_rows": _load_csv(scores / "audit_rows_pass1.csv"),
        "selftest": _load_json(SCORES_DIR / "mock" / "selftest_summary.json"),
    }
    # 规则层产物：DEMO 模式下 rule_check 仍写默认目录，两处都找
    for cand in (scores / "rule" / "rule_findings.jsonl",
                 SCORES_DIR / "rule" / "rule_findings.jsonl"):
        if cand.exists():
            data["rule_findings"] = [json.loads(l) for l in
                                     cand.read_text(encoding="utf-8").splitlines() if l.strip()]
            data["rule_findings_src"] = str(cand)
            break
    return data


# =====================================================================
# 图
# =====================================================================
def fig_heatmap(d: dict, out: Path) -> bool:
    h = d.get("heatmap")
    if h is None or h.empty:
        return False
    d6 = h[h["dim"] == "D6"]
    if d6.empty:
        return False
    piv = d6.pivot_table(index="tag", columns="model", values="value", aggfunc="mean")
    piv = piv.sort_index()
    fig, ax = plt.subplots(figsize=(max(5, 1.4 * len(piv.columns) + 3), max(4, 0.5 * len(piv) + 1.5)))
    im = ax.imshow(piv.values, cmap="RdYlGn", vmin=0, vmax=4, aspect="auto")
    ax.set_xticks(range(len(piv.columns)), list(piv.columns))
    ax.set_yticks(range(len(piv.index)), list(piv.index))
    for i in range(piv.shape[0]):
        for j in range(piv.shape[1]):
            v = piv.values[i, j]
            if pd.notna(v):
                ax.text(j, i, f"{v:.2f}", ha="center", va="center", fontsize=8,
                        color="black" if 1.0 < v < 3.2 else "white")
    ax.set_title(L("能力标签 × 模型（D6 整体可采纳度均值）",
                   "Capability tag x model (mean D6 adoptability)"))
    ax.set_xlabel(L("模型", "model"))
    fig.colorbar(im, ax=ax, shrink=0.8, label="D6 (0-4)")
    fig.text(0.5, -0.02,
             L("每格仅 1–3 题的均值，格子级差异不具推断力，只用于生成假设",
               "Each cell averages 1-3 questions; cell-level differences are hypothesis-generating only"),
             ha="center", fontsize=8, color="#666666")
    fig.tight_layout()
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return True


def fig_dimensions(d: dict, out: Path) -> bool:
    t = d.get("dimensions")
    if t is None or t.empty:
        return False
    piv = t.pivot_table(index="model", columns="dim", values="mean")
    piv = piv.reindex(columns=[c for c in DIMS if c in piv.columns])
    models = list(piv.index)
    x = np.arange(len(piv.columns))
    w = 0.8 / max(1, len(models))
    fig, ax = plt.subplots(figsize=(9, 4.5))
    for i, m in enumerate(models):
        vals = piv.loc[m].values.astype(float)
        sd = t[t["model"] == m].set_index("dim")["sd"].reindex(piv.columns).values.astype(float)
        ax.bar(x + (i - (len(models) - 1) / 2) * w, vals, w, label=m,
               yerr=np.nan_to_num(sd), capsize=2, alpha=0.88)
    ax.set_xticks(x, [L(f"{c} {DIM_NAMES_ZH[c]}", f"{c} {DIM_NAMES_EN[c]}") for c in piv.columns])
    ax.set_ylim(0, 4.4)
    ax.set_ylabel(L("均值（0–4，误差棒为 SD）", "mean (0-4, error bar = SD)"))
    ax.set_title(L("分维均值 —— 本套件不输出任何加权总分",
                   "Per-dimension means - this suite reports no weighted total"))
    ax.legend(title=L("模型", "model"), fontsize=8)
    ax.grid(axis="y", alpha=0.25)
    fig.text(0.5, -0.04,
             L("rubric §0 总则 3：把 6 个维度压成一个数字会销毁「哪个能力维度是短板」这一最有价值的信息",
               "rubric S0: collapsing 6 dimensions into one number destroys the most valuable signal - which capability is weak"),
             ha="center", fontsize=8, color="#666666")
    fig.tight_layout()
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return True


def fig_pairwise(d: dict, out: Path) -> bool:
    pw = d.get("pairwise")
    if not pw or not pw.get("gated"):
        return False
    rows = []
    for key, v in sorted(pw["gated"].items()):
        ci = v.get("ci") or {}
        if ci.get("point") is None:
            continue
        rows.append((key.replace("__vs__", " > "), ci["point"], ci.get("lo"), ci.get("hi"),
                     (pw.get("ungated", {}).get(key, {}) or {}).get("ci", {}).get("point")))
    if not rows:
        return False
    rows.sort(key=lambda r: r[1])
    labels = [r[0] for r in rows]
    y = np.arange(len(rows))
    fig, ax = plt.subplots(figsize=(8.5, max(2.6, 0.75 * len(rows))))
    lo = np.array([r[2] if r[2] is not None else np.nan for r in rows])
    hi = np.array([r[3] if r[3] is not None else np.nan for r in rows])
    pt = np.array([r[1] for r in rows])
    ax.errorbar(pt, y, xerr=[pt - lo, hi - pt], fmt="o", color="#1f77b4",
                capsize=4, lw=1.6, ms=6, label=L("门控后 D6", "gated D6"))
    ung = np.array([r[4] if r[4] is not None else np.nan for r in rows])
    ax.plot(ung, y, "x", color="#888888", ms=8, mew=1.6,
            label=L("门控前 D6_raw", "ungated D6_raw"))
    ax.axvline(0.5, color="#d62728", ls="--", lw=1.2)
    ax.text(0.5, len(rows) - 0.35, L(" 0.5 = 无差异", " 0.5 = no difference"),
            color="#d62728", fontsize=8, va="bottom")
    ax.set_yticks(y, labels, fontsize=9)
    ax.set_xlim(-0.02, 1.02)
    ax.set_xlabel(L("胜率（tie 记半分）与 95% bootstrap CI", "win rate (tie = 0.5) with 95% bootstrap CI"))
    ax.set_title(L("D6 派生的 pairwise 胜率", "Pairwise win rate derived from D6"))
    ax.legend(fontsize=8, loc="lower right")
    ax.grid(axis="x", alpha=0.25)
    fig.text(0.5, -0.06,
             L("CI 跨过 0.5 的模型对，不得表述为任何一方占优；n=20 题下 CI 宽属预期结果",
               "Pairs whose CI crosses 0.5 support no superiority claim; wide CIs are expected at n=20"),
             ha="center", fontsize=8, color="#666666")
    fig.tight_layout()
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return True


def fig_error_codes(d: dict, out: Path) -> bool:
    f = d.get("rule_findings")
    if not f:
        return False
    df = pd.DataFrame(f)
    if df.empty or "code" not in df.columns:
        return False
    codes = sorted(df["code"].unique(), key=lambda c: (len(c), c))
    models = sorted(df["model"].dropna().unique())
    conf = df[df["verdict"] == "confirmed"]
    flag = df[df["verdict"] == "flag_for_review"]
    x = np.arange(len(codes))
    w = 0.38
    fig, ax = plt.subplots(figsize=(max(8, 0.7 * len(codes) + 2), 4.2))
    c_by = conf.groupby("code").size().reindex(codes, fill_value=0).values
    f_by = flag.groupby("code").size().reindex(codes, fill_value=0).values
    ax.bar(x - w / 2, c_by, w, color="#d62728", label=L("confirmed（可直接采信）", "confirmed"))
    ax.bar(x + w / 2, f_by, w, color="#ff9f43", label=L("flag_for_review（须人工裁定）", "flag_for_review"))
    for xi, v in zip(x - w / 2, c_by):
        if v:
            ax.text(xi, v + 0.4, str(int(v)), ha="center", fontsize=8)
    for xi, v in zip(x + w / 2, f_by):
        if v:
            ax.text(xi, v + 0.4, str(int(v)), ha="center", fontsize=8)
    ax.set_xticks(x, codes)
    ax.set_ylabel(L("错误实例数", "error instances"))
    ax.set_xlabel(L("错误码（error_taxonomy.md §1）", "error code (error_taxonomy.md S1)"))
    ax.set_title(L("规则层检出的错误类型分布", "Error types detected by the rule layer"))
    ax.legend(fontsize=8)
    ax.grid(axis="y", alpha=0.25)
    fig.text(0.5, -0.05,
             L("两个数必须分开报：混在一起就是用不确定性制造虚假的确定性（§2.7）",
               "The two counts must stay separate - merging them manufactures false certainty (S2.7)"),
             ha="center", fontsize=8, color="#666666")
    fig.tight_layout()
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return True


def fig_length(d: dict, out: Path) -> bool:
    lb = d.get("length")
    if not lb or not lb.get("rows"):
        return False
    rows = lb["rows"]
    labels = [r["pair"] for r in rows]
    x = np.arange(len(rows))
    w = 0.38
    fig, ax = plt.subplots(figsize=(max(6.5, 1.6 * len(rows) + 2), 4.2))
    ax.bar(x - w / 2, [r["win_all"] or np.nan for r in rows], w,
           color="#1f77b4", label=L("未调整胜率", "unadjusted"))
    ax.bar(x + w / 2, [r["win_matched"] or np.nan for r in rows], w,
           color="#2ca02c", label=L("长度匹配后", "length-matched"))
    for i, r in enumerate(rows):
        n = r.get("n_matched") or 0
        if n and n < 8:
            ax.text(i + w / 2, (r["win_matched"] or 0) + 0.04, f"n={n}",
                    ha="center", fontsize=8, color="#d62728")
    ax.axhline(0.5, color="#d62728", ls="--", lw=1.1)
    ax.set_xticks(x, labels, fontsize=9)
    ax.set_ylim(0, 1.05)
    ax.set_ylabel(L("胜率（tie 记半分）", "win rate (tie = 0.5)"))
    ax.set_title(L("长度偏置诊断：未调整 vs 长度匹配", "Length-bias check: unadjusted vs length-matched"))
    ax.legend(fontsize=8)
    ax.grid(axis="y", alpha=0.25)
    fig.text(0.5, -0.05,
             L("红字 n<8 的格子不具解释力。这只是长度匹配，不是完整的协变量调整",
               "Cells marked n<8 are not interpretable. Matching is not full covariate adjustment"),
             ha="center", fontsize=8, color="#666666")
    fig.tight_layout()
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return True


FIGS = [
    ("fig_capability_heatmap.png", fig_heatmap),
    ("fig_dimension_bars.png", fig_dimensions),
    ("fig_pairwise_ci.png", fig_pairwise),
    ("fig_error_codes.png", fig_error_codes),
    ("fig_length_bias.png", fig_length),
]


# =====================================================================
# SUMMARY.md
# =====================================================================
def _ci_txt(ci: dict | None) -> str:
    if not ci or ci.get("point") is None or ci.get("lo") is None:
        return "CI 无法计算（n<3）"
    return f"{ci['point']:.2f} [{ci['lo']:.2f}, {ci['hi']:.2f}]（宽 {ci['width']:.2f}）"


def write_summary(d: dict, out: Path, figs: list[str]) -> None:
    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="minutes")
    Ln: list[str] = []
    A = Ln.append
    A("# 评测结果报告")
    A("")
    A(f"**生成时间**：{now}　**report v{REPORT_VERSION}**　**一键重跑**：`python src/report.py --full`")
    if d["demo"]:
        A("")
        A("> ## ⚠ 本报告基于 DEMO 合成数据")
        A(">")
        A("> `data/scores/demo/` 下的打分表由 `stats.py --make-demo` 生成，**不是任何真实模型的输出**。")
        A("> 它存在的唯一目的是验证统计与渲染代码，并演示三种真实数据里必然出现的情形：")
        A("> 门控前后排序翻转、长度与 D5 正相关、以及一行质控不合格的数据被正确剔除。")
        A("> **报告里的每一个数字都无意义，不得引用。**")
    A("")
    A("> 本仓库是方法学作品集，**不构成医疗建议，不用于任何临床决策**。")
    A("")
    A("---")
    A("")

    # 一、门控前后两套排序
    pw = d.get("pairwise") or {}
    A("## 一、整体可采纳度排序（门控前 vs 门控后）")
    A("")
    A("rubric §0 总则 3 规定**两套排序都必须给出**：加权求和会让一个漏掉 critical 红旗的")
    A("答案靠流畅排版把分数拉回中位数，而安全否决门控就是用来阻止这件事的。")
    A("如果两套排序不同，那本身就是一个发现。")
    A("")
    rk_g = pw.get("ranking_gated") or []
    rk_u = pw.get("ranking_ungated") or []
    A("| 排名 | 门控后 D6 | 门控前 D6_raw |")
    A("|---|---|---|")
    for i in range(max(len(rk_g), len(rk_u))):
        g = f"{rk_g[i][0]}（{rk_g[i][1]}）" if i < len(rk_g) else "—"
        u = f"{rk_u[i][0]}（{rk_u[i][1]}）" if i < len(rk_u) else "—"
        A(f"| {i + 1} | {g} | {u} |")
    A("")
    if [m for m, _ in rk_g] != [m for m, _ in rk_u]:
        A("**两套排序不一致。** 安全否决门控改变了模型排名——")
        A("说明有模型的原始整体判断分被它的致命缺陷拉低了。这一条必须写进结论，不能只报门控后的那一套。")
    elif rk_g:
        A("**两套排序一致。** 门控未改变排名；但仍须同时报告，")
        A("否则读者无法判断门控究竟起了作用还是从未触发。")
    else:
        A("（尚无打分数据）")
    A("")

    # 二、pairwise 胜率与 CI
    A("## 二、D6 pairwise 胜率与置信区间")
    A("")
    A("bootstrap 对**题目**重采样（不是对回答重采样）——同一题的 3 份回答由同一评分者打出，")
    A("题目内部存在相关性，按回答重采样会把 CI 做得假窄。tie 记半分。")
    A("")
    A("| 模型对 | n | 门控后胜率 [95% CI] | 门控前 | tie | 可下的结论 |")
    A("|---|---|---|---|---|---|")
    for r in pw.get("rows") or []:
        ci = {"point": r.get("win_rate_gated"), "lo": r.get("ci_lo"),
              "hi": r.get("ci_hi"), "width": r.get("ci_width")}
        # verdict 文本里已自带 markdown 强调，这里只去掉控制台标记，不再套一层粗体
        verdict = (r.get("verdict") or "").replace(console.WARN, "").strip() or "—"
        u = r.get("win_rate_ungated")
        u_s = f"{u:.2f}" if u is not None else "—"
        A(f"| {r['pair']} | {r['n']} | {_ci_txt(ci)} | {u_s} | {r['ties']} | {verdict} |")
    A("")
    widths = [r["ci_width"] for r in (pw.get("rows") or []) if r.get("ci_width") is not None]
    if widths:
        A(f"**CI 宽度 {min(widths):.2f}–{max(widths):.2f}**。n=20 题下属预期结果。"
          f"任何「模型 A 显著优于 B」的表述都必须附上这个区间，且不得外推到本垂类之外。")
        A("")

    # 三、分维
    t = d.get("dimensions")
    if t is not None and not t.empty:
        A("## 三、分维均值（**不是总分**）")
        A("")
        piv = t.pivot_table(index="model", columns="dim", values="mean")
        piv = piv.reindex(columns=[c for c in DIMS if c in piv.columns])
        A("| 模型 | " + " | ".join(piv.columns) + " |")
        A("|---|" + "---|" * len(piv.columns))
        for m, row in piv.iterrows():
            A(f"| {m} | " + " | ".join(f"{v:.2f}" if pd.notna(v) else "—" for v in row) + " |")
        A("")
        sd = t.pivot_table(index="model", columns="dim", values="sd").reindex(columns=piv.columns)
        A("| 模型（SD） | " + " | ".join(sd.columns) + " |")
        A("|---|" + "---|" * len(sd.columns))
        for m, row in sd.iterrows():
            A(f"| {m} | " + " | ".join(f"{v:.2f}" if pd.notna(v) else "—" for v in row) + " |")
        A("")
        if not piv.empty:
            weakest = {}
            for m in piv.index:
                s = piv.loc[m].dropna()
                if not s.empty:
                    weakest[m] = s.idxmin()
            A("**各模型最弱维度**：" + "；".join(
                f"{m} → {k}（{DIM_NAMES_ZH.get(k, k)}）" for m, k in sorted(weakest.items())))
            A("")
            A("> 这一行才是 JD 职责①「定位模型短板」要的输出。它无法从任何加权总分里恢复出来。")
            A("")

    # 四、错误分类学
    f = d.get("rule_findings")
    if f:
        df = pd.DataFrame(f)
        conf = df[df["verdict"] == "confirmed"]
        flag = df[df["verdict"] == "flag_for_review"]
        A("## 四、规则层检出的错误类型")
        A("")
        A(f"错误实例合计 **{len(df)}** 条：`confirmed` **{len(conf)}** 条（可直接采信）、"
          f"`flag_for_review` **{len(flag)}** 条（须人工裁定后才能计入）。")
        A("")
        A("| 错误码 | confirmed | flag | 映射维度 |")
        A("|---|---|---|---|")
        allc = sorted(set(df["code"]), key=lambda c: (len(c), c))
        dim_map = {}
        for r in f:
            dim_map.setdefault(r["code"], r.get("dimension_impact") or [])
        for c in allc:
            A(f"| {c} | {int((conf['code'] == c).sum())} | {int((flag['code'] == c).sum())} | "
              f"{', '.join(dim_map.get(c, []))} |")
        A("")
        A("> `confirmed` 与 `flag` 分开报是 error_taxonomy.md §2.7 的硬要求："
          "**把这两个数混在一起，就是在用不确定性制造虚假的确定性。**")
        A("")
        by_model = conf.groupby("model").size() if "model" in conf.columns else pd.Series(dtype=int)
        if not by_model.empty:
            A("按模型统计的 confirmed 数：" +
              "；".join(f"{m} {n}" for m, n in by_model.sort_values(ascending=False).items()))
            A("")

    # 五、规则层自检
    st = d.get("selftest")
    A("## 五、规则层的阳性/阴性对照自检")
    A("")
    if st:
        A(f"最近一次自检：{st.get('ts', '—')}")
        A("")
        A("| 错误码 | 注入 | 严格 recall | 归码等价 recall | 文档允许的最高 verdict |")
        A("|---|---|---|---|---|")
        for r in st.get("per_code", []):
            A(f"| {r['code']} | {r['injected']} | {r['strict']:.0%} | {r['equiv']:.0%} | {r['allowed']} |")
        A("")
        A(f"- 阴性对照（零缺陷）上的 confirmed 数：**{st.get('clean_confirmed')}**（验收要求 = 0）")
        A(f"- 阴性对照上的 flag 数：{st.get('clean_flag')}")
        A(f"- 越权升级（文档规定只能 flag 却输出 confirmed）：{st.get('escalations')}")
        A(f"- 无阳性对照、因而 recall 不可测的错误码：{st.get('no_positive_control')}")
        A("")
        A("> **只有缺陷注入（阳性）没有零缺陷对照（阴性），规则层会朝着「什么都报」的方向退化，"
          "而这种退化在只看 recall 时完全隐形。** 两条必须同时验收。")
    else:
        A("（尚无自检产物。运行 `python src/rule_check.py --self-test`）")
    A("")

    # 六、一致性
    rel = d.get("reliability") or {}
    jud = d.get("judge") or {}
    A("## 六、评分者一致性与 judge 交叉验证")
    A("")
    if rel.get("available") and rel.get("per_dimension"):
        A(f"**{rel.get('kind', 'intra-rater（重测）')}**　文件：{rel.get('retest_file')}")
        A("")
        A("| 维度 | n | α(ordinal) | κ(quadratic) | 平均偏差 |")
        A("|---|---|---|---|---|")
        for r in rel["per_dimension"]:
            A(f"| {r['dim']} | {r['n']} | {r['alpha_ordinal']} | {r['kappa_quadratic']} | "
              f"{r['mean_bias']:+.3f} |")
        A("")
    else:
        A("**尚无重测或第二评分者数据。** 重测须距首次打分 ≥72 小时，实际间隔天数必须如实记录；")
        A("若被排期压缩，记忆效应会系统性高估 α 与 κ，结论必须打折。")
        A("")
    if jud.get("available") and jud.get("per_dimension"):
        A("**LLM-judge vs 人工**")
        A("")
        A("| 维度 | n | α(ordinal) | κ(quadratic) | judge − 人工 |")
        A("|---|---|---|---|---|")
        for r in jud["per_dimension"]:
            A(f"| {r['dim']} | {r['n']} | {r['alpha_ordinal']} | {r['kappa_quadratic']} | "
              f"{r['judge_minus_human']:+.3f} |")
        A("")
        A("> α 高但有系统偏差 → judge 可作预筛但须校准；α 低且无偏差 → 是随机噪声。"
          "**这两种情形的处置完全不同，只报 α 会把它们混为一谈。**")
        A("")
        pbis = (jud.get("pairwise") or {})
        if pbis:
            A(f"**judge pairwise 位置偏置**：完整题对 {pbis.get('n_pairs')} 组，"
              f"同向 {pbis.get('same')}、反向 {pbis.get('reversed')}、含 tie {pbis.get('tie')}；"
              f"位置敏感率 {pbis.get('position_sensitivity'):.1%}"
              if pbis.get("position_sensitivity") is not None else "")
            A("")
            A("> 反向题对一律记 tie 后才进胜率。位置敏感率若 >20%，judge 的 pairwise 结论"
              "**不能当独立证据**，须在本报告中降级表述。")
            A("")
    else:
        A("**尚无 judge 结果。** 运行 `python src/llm_judge.py --emit pointwise`。")
        A("")

    rvh = d.get("rule_vs_human") or {}
    if rvh.get("available"):
        A("### 规则层 vs 人工")
        A("")
        hr = rvh.get("hit_rate") or {}
        if hr:
            A(f"- 红旗命中率：Pearson r = {hr.get('pearson_r')}，"
              f"规则层平均比人工高 {hr.get('mean_diff'):+.3f}（{hr.get('n_rule_higher')}/{hr.get('n')} 份偏高）")
            A("  规则层只看 `detection_hint` 是否出现、**不判 hit/partial**，"
              "因此系统性偏高是设计使然——它是人工 `hit_rate` 的**上界**，不是替代值。")
        cm = rvh.get("critical_miss") or {}
        if cm:
            A(f"- critical_miss：precision = {cm.get('precision')}，recall = {cm.get('recall')}"
              f"（TP={cm.get('tp')} FP={cm.get('fp')} FN={cm.get('fn')}）")
        A("")

    # 七、长度偏置
    lb = d.get("length") or {}
    if lb.get("rows"):
        A("## 七、长度偏置")
        A("")
        A("rubric D5 设了长度防火墙：字数只记录、不进任何锚点、不参与给分。"
          "长度在统计层作为**协变量**单独处理。")
        A("")
        A("| 模型对 | n(全部) | 未调整胜率 | n(长度匹配) | 匹配后胜率 [CI] | 长度差与胜负的相关 r [CI] |")
        A("|---|---|---|---|---|---|")
        for r in lb["rows"]:
            ci = f"[{r['matched_ci_lo']}, {r['matched_ci_hi']}]" if r.get("matched_ci_lo") is not None else "—"
            rc = (f"{r['len_bias_ci'][0]:+.2f}, {r['len_bias_ci'][1]:+.2f}"
                  if r.get("len_bias_ci") and r["len_bias_ci"][0] is not None else "—")
            warn = " ⚠n<8" if (r.get("n_matched") or 0) < 8 else ""
            A(f"| {r['pair']} | {r['n_all']} | {r['win_all']} | {r['n_matched']}{warn} | "
              f"{r['win_matched']} {ci} | {r['len_bias_r']} [{rc}] |")
        A("")
        if lb.get("flipped"):
            A(f"**长度匹配后排序翻转**：{lb['flipped']}。"
              f"原始排序里混入了冗长偏置，结论应以匹配后为准并说明。")
        else:
            A("长度匹配后未见排序翻转。")
        A("")
        A("> **这只是长度匹配，不是完整的协变量调整**（未做回归，为的是不引入 statsmodels/scipy）。")
        A("> 因此不能把「匹配后结论不变」说成「已排除长度混杂」——这条局限写进正文，不放脚注。")
        A("")

    # 八、质控
    ar = d.get("audit_rows")
    if ar is not None and not ar.empty and "audit_status" in ar.columns:
        A("## 八、打分质控")
        A("")
        c = Counter(ar["audit_status"].dropna())
        A("| 行状态 | 行数 | 处置 |")
        A("|---|---|---|")
        act = {"valid": "计入统计", "review": "计入统计，列出的问题须逐条复核",
               "mismatch": "进 mismatch 队列，**不自动改分**", "invalid": "不计入统计",
               "rejected": "拒收，须重做"}
        for k in ("valid", "review", "mismatch", "invalid", "rejected"):
            if c.get(k):
                A(f"| {k} | {c[k]} | {act[k]} |")
        A("")
        A("> mismatch 的处置原则（rubric §3）：**当人和规则冲突时，优先怀疑规则。**"
          "同一维度同方向反复 mismatch 说明锚点写得有歧义，要改 rubric 而不是改分数。")
        A("> 本项目已用 `score_audit.py --rubric-probe` 穷举中间量组合，查出并修正了 "
          "rubric v1.0 的 3 处锚点缺陷（D1/D5 各有无档可落的组合，D3 有 8 处档位重叠），见 rubric v1.1/v1.2 变更表。")
        A("")

    # 九、图
    if figs:
        A("## 九、图")
        A("")
        for fn in figs:
            A(f"![{fn}](fig/{fn})")
            A("")
        A("> 每张图背后的原始表都在 `results/tables/`。**图会骗人，表不会。**")
        A("")

    # 十、局限
    A("## 十、局限（正文，不是脚注）")
    A("")
    A("1. **单临床评分者**：只有一位评分者，第二评分者子集仅 15 份且其专业水平与培训时长")
    A("   都远低于主评分者。**没有真正的 inter-rater reliability**，所有 α/κ 的 CI 都会很宽。")
    A("2. **n = 20 题**：pairwise 胜率的 bootstrap CI 较宽（见第二节实际数值），")
    A("   任何占优表述都必须附 CI，且不得外推到本垂类之外。")
    A("3. **题库作者与主评分者同一人**：存在「自己出题自己判」的循环。缓解只有两条")
    A("   （第二评分者子集、规则层与 judge 交叉验证），能削弱但消不掉。")
    A("4. **网页版模型不可控**：temperature 与采样参数无法固定，跨模型可比性有天然上限；")
    A("   模型版本会漂移，结论对版本敏感。")
    A("5. **judge 仍有风格偏好**：面板外只削弱了 self-preference bias，没有消除")
    A("   训练语料带来的风格偏好（例如偏爱结构化表达）。")
    A("6. **长度调整不完整**：只做了长度匹配，未做回归调整（见第七节）。")
    A("7. **规则层有 7 类错误码没有阳性对照**（见第五节清单），它们的 recall 无法测，")
    A("   只能测零缺陷对照上的假阳性率。**把「没测」说成「通过」是本报告最不允许的事。**")
    A("8. **E2d 的阳性对照极薄**：纯 `forbid` 型红旗只有 1 条，E2d 的 recall 数字")
    A("   不具备解释力，只能作为存在性证明（见 error_taxonomy.md §1.1）。")
    A("")
    A("---")
    A("")
    A(f"*本报告由 `src/report.py` v{REPORT_VERSION} 生成，全部数字可由 "
      f"`python src/report.py --full` 一键重跑复现（固定 seed）。*")
    out.write_text("\n".join(Ln) + "\n", encoding="utf-8")


# =====================================================================
# 一键重跑
# =====================================================================
def run_full(demo: bool, seed: int) -> None:
    """按依赖顺序跑完整条链。用子进程而不是 import，保证与用户手跑的行为一致。"""
    py = sys.executable
    steps: list[tuple[str, list[str], bool]] = [
        ("规则层阳性/阴性对照自检", [py, "src/rule_check.py", "--self-test"], False),
    ]
    if demo:
        steps += [
            # DEMO 的规则层产物写进 demo 子目录，避免与真实数据的产物混在一起
            ("规则层（DEMO 回答）", [py, "src/rule_check.py", "--source", "demo",
                                      "--out-dir", str(SCORES_DIR / "demo" / "rule")], True),
            ("生成 DEMO 打分表", [py, "src/stats.py", "--make-demo", "--seed", str(seed)], False),
            ("打分质控（DEMO）", [py, "src/score_audit.py", "--scores-dir",
                                  str(SCORES_DIR / "demo")], True),
            ("统计分析（DEMO）", [py, "src/stats.py", "--demo", "--seed", str(seed)], False),
        ]
    else:
        steps += [
            ("规则层（真实/DEMO 回答）", [py, "src/rule_check.py"], False),
            # score_audit 的退出码 1 表示"存在 invalid/rejected 行"，是数据质量信号
            # 而不是流水线故障，因此不算失败
            ("打分质控", [py, "src/score_audit.py"], True),
            ("统计分析", [py, "src/stats.py", "--all", "--seed", str(seed)], True),
        ]
    for name, cmd, allow_fail in steps:
        print(f"\n{'=' * 62}\n[report --full] {name}\n{'=' * 62}")
        r = subprocess.run(cmd, cwd=str(ROOT))
        if r.returncode != 0:
            msg = f"{name} 返回 {r.returncode}"
            if allow_fail:
                print(f"{console.WARN} {msg}（继续渲染，缺的部分会在报告里标注）")
            else:
                print(f"{console.FAIL} {msg}")


def dump_selftest_summary() -> None:
    """把 rule_check --self-test 的关键数字落成 JSON，供 SUMMARY.md 引用。

    直接 import rule_check 并调用其 self_test 会打印一大堆到 stdout；
    这里重算同样的量，避免解析控制台输出这种脆弱做法。
    """
    try:
        import rule_check as RC
    except Exception as e:
        print(f"{console.WARN} 无法导入 rule_check，跳过自检摘要：{e}", file=sys.stderr)
        return
    truth_p = RC.MOCK_TRUTH
    mock_p = RC.MOCK_RESPONSES
    if not truth_p.exists() or not mock_p.exists():
        return
    qs = RC.load_questions()
    recs = [json.loads(l) for l in mock_p.read_text(encoding="utf-8").splitlines() if l.strip()]
    truths = {(t["qid"], t["model"]): t for t in
              (json.loads(l) for l in truth_p.read_text(encoding="utf-8").splitlines() if l.strip())}
    wl = RC.D.reference_whitelist()
    per: dict[str, Counter] = {}
    clean_conf = clean_flag = 0
    escal: list[str] = []
    for r in recs:
        q = qs.get(r["qid"])
        if not q:
            continue
        _, findings = RC.analyze(r["text"], q, wl)
        if r["model"] == "mock_clean":
            clean_conf += sum(1 for f in findings if f["verdict"] == RC.CONFIRMED)
            clean_flag += sum(1 for f in findings if f["verdict"] == RC.FLAG)
            continue
        for f in findings:
            if f["verdict"] == RC.CONFIRMED and RC.CODE_MAX_VERDICT.get(f["code"]) != RC.CONFIRMED:
                escal.append(f"{f['qid']}/{f['code']}")
        det = {f["code"] for f in findings}
        t = truths.get((r["qid"], r["model"]))
        if not t:
            continue
        for inj in t["injected"]:
            if inj.get("achieved", True) is False:
                continue
            c = inj["code"]
            per.setdefault(c, Counter())["injected"] += 1
            if c in det:
                per[c]["strict"] += 1
            if c in det or (RC.EQUIVALENT_CODES.get(c, {c}) & det):
                per[c]["equiv"] += 1
    out = {
        "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="minutes"),
        "per_code": [{"code": c, "injected": v["injected"],
                      "strict": (v["strict"] / v["injected"]) if v["injected"] else None,
                      "equiv": (v["equiv"] / v["injected"]) if v["injected"] else None,
                      "allowed": RC.CODE_MAX_VERDICT.get(c, "?")}
                     for c, v in sorted(per.items())],
        "clean_confirmed": clean_conf, "clean_flag": clean_flag,
        "escalations": len(escal),
        "no_positive_control": sorted(set(RC.CODE_MAX_VERDICT) - set(per) - {"E2c", "E4a"}),
    }
    p = SCORES_DIR / "mock" / "selftest_summary.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


# =====================================================================
# 入口
# =====================================================================
def main(argv: list[str] | None = None) -> int:
    global CJK_OK, FONT_NAME
    ap = argparse.ArgumentParser(description="一键出表出图到 results/")
    ap.add_argument("--full", action="store_true", help="先跑完整条链再渲染")
    ap.add_argument("--demo", action="store_true", help="渲染 DEMO 数据的报告")
    ap.add_argument("--no-figures", action="store_true")
    ap.add_argument("--out", default="")
    ap.add_argument("--seed", type=int, default=20261001)
    args = ap.parse_args(argv)

    out_root = Path(args.out) if args.out else (RESULTS / "demo" if args.demo else RESULTS)
    out_root = out_root.resolve()
    fig_dir = out_root / "fig"
    tab_dir = out_root / "tables"

    if args.full:
        run_full(args.demo, args.seed)
    dump_selftest_summary()

    FONT_NAME, CJK_OK = pick_cjk_font()
    if CJK_OK:
        print(f"[i] 图表中文字体：{FONT_NAME}")
    else:
        print(f"{console.WARN} 未找到可用的中文字体（试过了 {CJK_CANDIDATES[:4]} …），"
              f"图内标签整体切换为英文。")
        print("     SUMMARY.md 仍是中文——文字报告不受字体影响。")

    d = gather(args.demo)
    out_root.mkdir(parents=True, exist_ok=True)
    fig_dir.mkdir(parents=True, exist_ok=True)
    tab_dir.mkdir(parents=True, exist_ok=True)

    figs: list[str] = []
    if not args.no_figures:
        for name, fn in FIGS:
            try:
                ok = fn(d, fig_dir / name)
            except Exception as e:
                print(f"{console.WARN} {name} 渲染失败：{type(e).__name__}: {e}", file=sys.stderr)
                ok = False
            if ok:
                figs.append(name)
                print(f"  {console.OK} {name}")
            else:
                print(f"  -    {name}（数据不足，跳过）")

    # 图背后的表
    copied = 0
    for src in (d["stats_dir"]).glob("*.csv"):
        shutil.copy2(src, tab_dir / src.name)
        copied += 1
    for src in (d["stats_dir"]).glob("*.json"):
        shutil.copy2(src, tab_dir / src.name)
        copied += 1
    audit = (SCORES_DIR / "demo" if args.demo else SCORES_DIR) / "audit_report_pass1.md"
    if audit.exists():
        shutil.copy2(audit, out_root / "audit_report_pass1.md")

    write_summary(d, out_root / "SUMMARY.md", figs)
    print(f"\n{console.OK} 报告 -> {out_root}")
    print(f"     SUMMARY.md + {len(figs)} 张图 + {copied} 份原始表")
    if d["demo"]:
        print(f"     {console.FAIL} 这是 DEMO 报告，所有数字来自合成数据，不得引用。")
    missing = [n for n, _ in FIGS if n not in figs]
    if missing:
        print(f"     {console.WARN} 以下图因数据不足未生成：{missing}")
        print("        真实打分到位后重跑 `python src/report.py --full` 即可补齐。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
