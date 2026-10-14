#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stats.py — 统计层（rubric §4.2 一致性指标 + §D6 pairwise 胜率 + 长度偏置调整）

四条不可让步的原则
------------------
1. **不输出任何加权总分**。rubric §0 总则 3 已说明理由：把 6 个维度压成一个数字
   会销毁"哪个能力维度是短板"这一最有价值的信息。本文件只产出分维比较与
   D6 派生的 pairwise 胜率。
2. **门控前与门控后两套排序都要给**。若两者不同，那本身就是结论。
3. **固定 seed**。bootstrap 的 CI 必须可复现，否则"重跑一次数字变了"无从排查。
4. **CI 宽度如实报告**。n=20 题的 bootstrap CI 会很宽；任何"A 显著优于 B"的
   表述都必须附 CI。CI 宽到跨过 0.5 时，本文件会直接打印"不足以支撑该表述"，
   而不是把数字交给别人去误读。

不依赖 scipy / statsmodels / sklearn。序数 Krippendorff's α 与 quadratic weighted
κ 都按定义手写实现——这两个公式短到不需要引入一个重依赖，而引入依赖会让
"clone 下来就能跑"这个作品集属性打折。

用法
----
    python src/stats.py --all                 # 全部分析，产物写 data/stats/
    python src/stats.py --pairwise            # 只跑 D6 pairwise 胜率
    python src/stats.py --reliability         # 只跑 intra/inter-rater 一致性
    python src/stats.py --judge               # judge vs 人工
    python src/stats.py --rule                # 规则层 vs 人工
    python src/stats.py --length              # 长度偏置诊断与长度匹配胜率
    python src/stats.py --heatmap             # 能力标签 × 模型 × 维度
    python src/stats.py --self-test           # 用已知答案的夹具验证公式实现

依赖：numpy、pandas（绘图在 report.py 里用 matplotlib）。
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import console  # noqa: E402

console.setup()

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SCORES_DIR = ROOT / "data" / "scores"
JUDGE_DIR = SCORES_DIR / "judge"
RULE_DIR = SCORES_DIR / "rule"
KEY_FILE = ROOT / "data" / "responses" / "key.jsonl"
QUESTIONS = ROOT / "data" / "questions.jsonl"
OUT_DIR = ROOT / "data" / "stats"

DEFAULT_SEED = 20261001
N_BOOT = 10000
DIMS = ["D1", "D2", "D3", "D4", "D5", "D6"]
SCORE_LEVELS = [0, 1, 2, 3, 4]
# CI 宽度超过此值就明确打印"不足以支撑 A 优于 B 的表述"
CI_WIDTH_WARN = 0.30


# =====================================================================
# 一致性指标：按定义手写
# =====================================================================
def quadratic_weighted_kappa(a: list[int], b: list[int], k: int = 5) -> float:
    """Cohen's quadratic weighted κ。

    权重 w_ij = 1 − (i−j)²/(k−1)²，即相差 1 档只算小分歧、相差 4 档几乎算完全不一致。
    用二次权重而不是未加权 κ，因为 0–4 是**序数**量表：把 3 分打成 2 分与
    把 3 分打成 0 分不是同一种错误。
    """
    if len(a) != len(b) or not a:
        return float("nan")
    O = np.zeros((k, k))
    for x, y in zip(a, b):
        if 0 <= x < k and 0 <= y < k:
            O[x, y] += 1
    n = O.sum()
    if n == 0:
        return float("nan")
    w = np.zeros((k, k))
    for i in range(k):
        for j in range(k):
            w[i, j] = 1.0 - ((i - j) ** 2) / ((k - 1) ** 2)
    ha = O.sum(axis=1) / n
    hb = O.sum(axis=0) / n
    E = np.outer(ha, hb) * n
    obs = (w * O).sum() / n
    exp = (w * E).sum() / n
    if abs(1 - exp) < 1e-12:
        return float("nan")
    return float((obs - exp) / (1 - exp))


def ordinal_distance_matrix(counts: np.ndarray) -> np.ndarray:
    """Krippendorff 序数度量的 δ² 矩阵。

    δ²(c,k) = ( Σ_{g=c}^{k} n_g − (n_c + n_k)/2 − 1 )²
    n_g 是**全部编码值**的边际频数（不是某一编码者的），这是序数 α 与
    名义 α 的唯一区别，也是最容易实现错的地方。
    """
    k = len(counts)
    csum = np.cumsum(counts)
    d2 = np.zeros((k, k))
    for c in range(k):
        for kk in range(k):
            if c == kk:
                continue
            lo, hi = (c, kk) if c < kk else (kk, c)
            s = csum[hi] - (csum[lo - 1] if lo > 0 else 0)
            v = s - (counts[lo] + counts[hi]) / 2.0 - 1.0
            d2[c, kk] = v * v
    return d2


def krippendorff_alpha_ordinal(pairs: list[tuple[int, int]], k: int = 5) -> float:
    """两位编码者、无缺失值的序数 Krippendorff's α。

    pairs = [(编码者1的值, 编码者2的值), ...]，每个元素是一个分析单元。
    α = 1 − D_o/D_e；α ≥ 0.80 通常视为可靠，0.667–0.80 为可接受的探索性水平。
    """
    if len(pairs) < 2:
        return float("nan")
    o = np.zeros((k, k))
    for x, y in pairs:
        if not (0 <= x < k and 0 <= y < k):
            continue
        o[x, y] += 1
        o[y, x] += 1          # 有序对：每个单元贡献 m(m−1)=2 次
    n_c = o.sum(axis=1)
    n = n_c.sum()
    if n < 2:
        return float("nan")
    d2 = ordinal_distance_matrix(n_c)
    D_o = float((o * d2).sum() / n)
    outer = np.outer(n_c, n_c)
    np.fill_diagonal(outer, 0.0)
    D_e = float((outer * d2).sum() / (n * (n - 1)))
    if D_e <= 1e-12:
        return float("nan")     # 所有值都相同，α 无定义
    return float(1.0 - D_o / D_e)


def alpha_interpretation(a: float) -> str:
    if math.isnan(a):
        return "无定义（方差为 0 或样本不足）"
    if a >= 0.80:
        return "可靠（≥0.80）"
    if a >= 0.667:
        return "探索性可接受（0.667–0.80）"
    return "不可靠（<0.667），不得据此下结论"


# =====================================================================
# pairwise 胜率与 bootstrap CI
# =====================================================================
def win_rate(outcomes: list[int]) -> float:
    """outcomes: +1=A胜, -1=B胜, 0=tie。tie 记半分。"""
    if not outcomes:
        return float("nan")
    return (sum(1 for o in outcomes if o > 0) + 0.5 * sum(1 for o in outcomes if o == 0)) / len(outcomes)


def bootstrap_ci(outcomes: list[int], seed: int | None = None,
                 n_boot: int | None = None, alpha: float = 0.05) -> dict:
    """对**题目**重采样（不是对回答重采样）：题目才是独立单元。

    同一题的 3 份回答由同一评分者在不同 block 打出，题目内部存在相关性；
    按回答重采样会低估方差、把 CI 做得假窄。

    seed / n_boot 默认在**调用时**读模块全局，而不是在函数定义时绑定——
    否则 `--seed` 与 `--n-boot` 改了也不生效，而"固定 seed"是本文件的硬要求之一。
    """
    seed = DEFAULT_SEED if seed is None else seed
    n_boot = N_BOOT if n_boot is None else n_boot
    rng = np.random.default_rng(seed)
    arr = np.asarray(outcomes, dtype=float)
    n = len(arr)
    if n == 0:
        return {"lo": float("nan"), "hi": float("nan"), "width": float("nan"), "n": 0}
    point = win_rate(outcomes)
    if n < 3:
        return {"lo": float("nan"), "hi": float("nan"), "width": float("nan"),
                "n": n, "point": point}
    idx = rng.integers(0, n, size=(n_boot, n))
    boots = (arr[idx] > 0).sum(axis=1) + 0.5 * (arr[idx] == 0).sum(axis=1)
    rates = boots / n
    lo, hi = np.percentile(rates, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return {"point": float(point), "lo": float(lo), "hi": float(hi),
            "width": float(hi - lo), "n": n, "n_boot": n_boot, "seed": seed}


def pairwise_from_scores(df: pd.DataFrame, score_col: str = "D6") -> dict:
    """从长表（qid, model, 分数）派生所有模型对的逐题胜负。"""
    out: dict[str, dict] = {}
    models = sorted(df["model"].dropna().unique())
    by_qm = {(r.qid, r.model): getattr(r, score_col) for r in df.itertuples()
             if pd.notna(getattr(r, score_col, np.nan))}
    qids = sorted({q for (q, m) in by_qm})
    for a, b in combinations(models, 2):
        outcomes, detail = [], []
        for q in qids:
            sa, sb = by_qm.get((q, a)), by_qm.get((q, b))
            if sa is None or sb is None:
                continue
            o = 0 if sa == sb else (1 if sa > sb else -1)
            outcomes.append(o)
            detail.append({"qid": q, "a": a, "b": b, "score_a": sa, "score_b": sb,
                           "outcome": o})
        key = f"{a}__vs__{b}"
        out[key] = {"model_a": a, "model_b": b, "n": len(outcomes),
                    "outcomes": outcomes, "detail": detail,
                    "ci": bootstrap_ci(outcomes)}
    return out


# =====================================================================
# 数据载入
# =====================================================================
def load_human(pass_id: int = 1, require_model: bool = True) -> pd.DataFrame:
    """载入人工打分表并贴上模型名。

    优先读 human_pass{n}_unblinded.csv；否则用 key.jsonl 连接。
    两者都没有 → 说明还没揭盲，此时**拒绝**做模型间比较（那等于破盲）。
    """
    unb = SCORES_DIR / f"human_pass{pass_id}_unblinded.csv"
    plain = SCORES_DIR / f"human_pass{pass_id}.csv"
    src = unb if unb.exists() else plain
    if not src.exists():
        sys.exit(f"[ERROR] 找不到人工打分表：{unb.name} 或 {plain.name}")
    df = pd.read_csv(src, encoding="utf-8-sig", dtype=str)
    if df.empty:
        sys.exit(f"[ERROR] {src.name} 没有数据行")

    if "model" not in df.columns or df["model"].isna().all():
        if not KEY_FILE.exists():
            if require_model:
                sys.exit(f"[ERROR] 打分表无 model 列且找不到 {KEY_FILE.name}。\n"
                         f"        尚未揭盲时不做模型间比较——那是匿名化的全部意义。\n"
                         f"        打分完成后运行：python src/blind_shuffle.py --unblind")
        else:
            keys = {}
            for line in KEY_FILE.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    k = json.loads(line)
                    keys[k["response_id"]] = k
            df["model"] = df["response_id"].map(lambda r: keys.get(r, {}).get("model"))
            df["char_count"] = df["response_id"].map(
                lambda r: keys.get(r, {}).get("char_count"))
            df["panel"] = df["response_id"].map(lambda r: keys.get(r, {}).get("panel"))
            miss = df["model"].isna().sum()
            if miss:
                print(f"{console.WARN} {miss} 行在映射表里找不到 response_id，已丢弃",
                      file=sys.stderr)
                df = df[df["model"].notna()]

    for c in DIMS + ["D4_raw", "D6_raw", "n_fatal", "n_core", "n_minor", "n_vague",
                     "n_gap", "n_inversion", "n_exclusive", "n_uncertain", "n_overhedge",
                     "hit_rate", "redundancy_ratio", "word_count", "char_count",
                     "minutes_spent"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    for c in ["critical_miss", "modality_downgraded", "both_paths", "gap_declared",
              "off_target", "conclusion_in_first_3", "conclusion_consistent"]:
        if c in df.columns:
            df[c] = df[c].map(_as_bool)
    return df


def _as_bool(v):
    if pd.isna(v):
        return None
    s = str(v).strip().lower()
    if s in ("true", "1", "yes", "y", "是", "t"):
        return True
    if s in ("false", "0", "no", "n", "否", "f"):
        return False
    return None


def apply_audit_filter(df: pd.DataFrame, pass_id: int) -> tuple[pd.DataFrame, dict]:
    """按 score_audit 的行状态过滤：invalid / rejected 不进统计。"""
    ap = SCORES_DIR / f"audit_rows_pass{pass_id}.csv"
    info = {"audit_file": None, "dropped": 0, "status": {}}
    if not ap.exists():
        print(f"{console.WARN} 未找到 {ap.name}，无法按质控状态过滤。"
              f"先运行 python src/score_audit.py", file=sys.stderr)
        return df, info
    aud = pd.read_csv(ap, encoding="utf-8-sig", dtype=str)
    info["audit_file"] = str(ap.relative_to(ROOT))
    info["status"] = dict(Counter(aud.get("audit_status", pd.Series(dtype=str)).dropna()))
    bad = aud[aud["audit_status"].isin(["invalid", "rejected"])]
    if bad.empty:
        return df, info
    bad_keys = set(zip(bad.get("qid", ""), bad.get("response_id", ""), bad.get("pass_id", "")))
    mask = [ (q, r, str(p)) not in bad_keys
             for q, r, p in zip(df.get("qid", ""), df.get("response_id", ""),
                                df.get("pass_id", ""))]
    info["dropped"] = int((~np.asarray(mask)).sum())
    return df[np.asarray(mask)].copy(), info


def load_questions() -> dict[str, dict]:
    out = {}
    if not QUESTIONS.exists():
        return out
    for line in QUESTIONS.read_text(encoding="utf-8").splitlines():
        if line.strip():
            q = json.loads(line)
            out[q["qid"]] = q
    return out


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def _save(name: str, obj) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    p = OUT_DIR / name
    if isinstance(obj, (dict, list)):
        p.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=_jsonable),
                     encoding="utf-8")
    else:
        obj.to_csv(p, index=False, encoding="utf-8-sig")
    return p


def _jsonable(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return None if math.isnan(float(o)) else float(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, float) and math.isnan(o):
        return None
    return str(o)


def _fmt_ci(ci: dict) -> str:
    if not ci or math.isnan(ci.get("lo", float("nan"))):
        return "CI 无法计算（n<3）"
    return f"{ci['point']:.2f} [{ci['lo']:.2f}, {ci['hi']:.2f}] 宽 {ci['width']:.2f}"


def _ci_verdict(ci: dict) -> str:
    if not ci or math.isnan(ci.get("lo", float("nan"))):
        return ""
    if ci["lo"] > 0.5:
        return "CI 下界 > 0.5，可表述为占优"
    if ci["hi"] < 0.5:
        return "CI 上界 < 0.5，可表述为处于劣势"
    return f"{console.WARN} CI 跨过 0.5，**不足以支撑任何一方占优的表述**"


# =====================================================================
# 分析：pairwise（门控前 / 门控后）
# =====================================================================
def analyze_pairwise(df: pd.DataFrame) -> dict:
    gated = pairwise_from_scores(df, "D6")
    ungated = pairwise_from_scores(df, "D6_raw") if "D6_raw" in df.columns else {}
    rows = []
    for key, v in sorted(gated.items()):
        u = ungated.get(key, {})
        rows.append({
            "pair": f"{v['model_a']} vs {v['model_b']}", "n": v["n"],
            "win_rate_gated": v["ci"].get("point"),
            "ci_lo": v["ci"].get("lo"), "ci_hi": v["ci"].get("hi"),
            "ci_width": v["ci"].get("width"),
            "win_rate_ungated": (u.get("ci") or {}).get("point"),
            "ties": sum(1 for o in v["outcomes"] if o == 0),
            "verdict": _ci_verdict(v["ci"]),
        })
    print("\n=== D6 pairwise 胜率（对题目做 bootstrap，tie 记半分）===")
    print(f"{'模型对':<28}{'n':>4}  {'门控后胜率 [95% CI]':<34}{'门控前':>8}{'tie':>5}")
    for r in rows:
        a, b = r["pair"].split(" vs ")
        ci = gated[f"{a}__vs__{b}"]["ci"]
        u = r["win_rate_ungated"]
        u_s = f"{u:.2f}" if u is not None else "—"
        print(f"{r['pair']:<28}{r['n']:>4}  {_fmt_ci(ci):<34}{u_s:>8}{r['ties']:>5}")
    print()
    for r in rows:
        print(f"  {r['pair']}: {r['verdict']}")

    # 排序：门控前 vs 门控后（rubric §0 总则 3 要求两套都给）
    def ranking(col):
        if col not in df.columns:
            return []
        g = df.groupby("model")[col].mean().sort_values(ascending=False)
        return [(m, round(float(v), 3)) for m, v in g.items() if pd.notna(v)]
    rk_g, rk_u = ranking("D6"), ranking("D6_raw")
    print(f"\n=== 整体可采纳度排序（rubric 总则 3 要求两套都报）===")
    print(f"  门控后 D6    ：{' > '.join(f'{m}({v})' for m, v in rk_g)}")
    print(f"  门控前 D6_raw：{' > '.join(f'{m}({v})' for m, v in rk_u)}")
    if [m for m, _ in rk_g] != [m for m, _ in rk_u]:
        print(f"  {console.FAIL} 两套排序**不一致**——安全否决门控改变了模型排名。"
              f"这本身就是一个必须写进 README 的发现：")
        print("        说明有模型靠流畅表达把总分拉了回来，而门控把它的致命缺陷暴露了出来。")
    else:
        print(f"  {console.OK} 两套排序一致：门控未改变排名（但仍须同时报告，"
              f"否则读者无法判断门控是否起了作用）")

    widths = [r["ci_width"] for r in rows if r["ci_width"] is not None]
    if widths:
        print(f"\n=== CI 宽度的诚实交代 ===")
        print(f"  n={rows[0]['n']} 题、bootstrap {N_BOOT} 次、seed={DEFAULT_SEED}")
        print(f"  CI 宽度范围 {min(widths):.2f}–{max(widths):.2f}"
              f"（预警线 {CI_WIDTH_WARN}）")
        if max(widths) > CI_WIDTH_WARN:
            print(f"  {console.WARN} 最宽 CI 达 {max(widths):.2f}，"
                  f"在 n=20 的题量下属预期结果。**任何'模型 A 显著优于 B'的表述都必须附 CI，"
                  f"且不得外推到本垂类之外。**")
    _save("pairwise.json", {"gated": {k: {kk: vv for kk, vv in v.items() if kk != "detail"}
                                      for k, v in gated.items()},
                            "ungated": {k: {kk: vv for kk, vv in v.items() if kk != "detail"}
                                        for k, v in ungated.items()},
                            "ranking_gated": rk_g, "ranking_ungated": rk_u,
                            "rows": rows})
    _save("pairwise_detail.csv",
          pd.DataFrame([d for v in gated.values() for d in v["detail"]]))
    return {"gated": gated, "ungated": ungated, "rows": rows,
            "ranking_gated": rk_g, "ranking_ungated": rk_u}


# =====================================================================
# 分析：分维描述 + 能力标签热力图
# =====================================================================
def analyze_dimensions(df: pd.DataFrame) -> dict:
    qs = load_questions()
    print("\n=== 分维均值（±SD）===")
    rows = []
    for m, g in df.groupby("model"):
        for d in DIMS:
            if d not in g.columns:
                continue
            s = g[d].dropna()
            if s.empty:
                continue
            rows.append({"model": m, "dim": d, "n": len(s),
                         "mean": round(float(s.mean()), 3),
                         "sd": round(float(s.std(ddof=1)), 3) if len(s) > 1 else None,
                         "min": int(s.min()), "max": int(s.max())})
    t = pd.DataFrame(rows)
    if not t.empty:
        piv = t.pivot(index="model", columns="dim", values="mean")
        print(piv.round(2).to_string())
        print(f"\n  {console.WARN} 上表是**分维**均值，不是总分。rubric §0 明确不输出加权总分："
              f"压成一个数字会销毁「哪个能力维度是短板」这一最有价值的信息。")
    _save("dimensions.csv", t)

    # 能力标签热力图：model × capability_tag × 维度均值
    heat = []
    for qid, q in qs.items():
        for tag in q.get("capability_tags") or []:
            sub = df[df["qid"] == qid]
            for m, g in sub.groupby("model"):
                for d in DIMS:
                    if d in g.columns and g[d].notna().any():
                        heat.append({"model": m, "tag": tag, "dim": d, "qid": qid,
                                     "value": float(g[d].mean())})
    h = pd.DataFrame(heat)
    if not h.empty:
        agg = h.groupby(["tag", "model", "dim"])["value"].mean().reset_index()
        agg["value"] = agg["value"].round(3)
        _save("capability_heatmap.csv", agg)
        d6 = agg[agg["dim"] == "D6"].pivot(index="tag", columns="model", values="value")
        print("\n=== 能力标签 × 模型（D6 均值）===")
        print(d6.round(2).to_string())
        cells = int(agg[agg["dim"] == "D6"]["value"].notna().sum())
        n_tag = agg["tag"].nunique()
        n_model = agg["model"].nunique()
        print(f"\n  {console.WARN} 热力图每格只有 {cells / max(1, n_tag * n_model):.1f} 题的均值"
              f"（{n_tag} 标签 × {n_model} 模型）。**格子级的差异不具备推断力**，"
              f"只能用于生成假设，不能用于下结论。")
    else:
        print(f"\n{console.WARN} 能力标签热力图为空：题库或打分表未对齐")
    return {"dimensions": rows}


# =====================================================================
# 分析：一致性（intra / inter / judge / rule）
# =====================================================================
def _pairs_from(df1: pd.DataFrame, df2: pd.DataFrame, col: str,
                join_on=("qid", "response_id")) -> list[tuple[int, int]]:
    a = df1.set_index(list(join_on))[col]
    b = df2.set_index(list(join_on))[col]
    common = a.index.intersection(b.index)
    out = []
    for k in common:
        x, y = a.loc[k], b.loc[k]
        if pd.notna(x) and pd.notna(y):
            out.append((int(x), int(y)))
    return out


def analyze_reliability(pass_id: int = 1, second: str = "") -> dict:
    df = load_human(pass_id)
    df, ainfo = apply_audit_filter(df, pass_id)
    res: dict = {"audit": ainfo}
    print("\n=== 评分者一致性 ===")

    df2 = None
    p2 = SCORES_DIR / f"human_pass{pass_id + 1}_unblinded.csv"
    p2b = SCORES_DIR / f"human_pass{pass_id + 1}.csv"
    for cand in (p2, p2b):
        if cand.exists():
            df2 = load_human(pass_id + 1)
            df2, _ = apply_audit_filter(df2, pass_id + 1)
            res["retest_file"] = cand.name
            break
    if second:
        df2 = pd.read_csv(second, encoding="utf-8-sig", dtype=str)
        for c in DIMS:
            if c in df2.columns:
                df2[c] = pd.to_numeric(df2[c], errors="coerce")
        res["retest_file"] = second
    if df2 is None or df2.empty:
        print(f"  {console.WARN} 没有第二次打分（pass{pass_id + 1}）或第二评分者文件，"
              f"跳过一致性分析。重测须距首次 ≥72 小时。")
        res["available"] = False
        _save("reliability.json", res)
        return res

    res["available"] = True
    kind = "inter-rater（第二评分者，探索性）" if second else "intra-rater（重测）"
    rows = []
    print(f"  对比类型：{kind}　文件：{res['retest_file']}")
    print(f"\n  {'维度':<6}{'n':>5}{'α(ordinal)':>12}{'κ(quadratic)':>14}{'平均偏差':>10}  解释")
    for d in DIMS:
        if d not in df.columns or d not in df2.columns:
            continue
        pairs = _pairs_from(df, df2, d)
        if len(pairs) < 2:
            continue
        a = krippendorff_alpha_ordinal(pairs)
        k = quadratic_weighted_kappa([p[0] for p in pairs], [p[1] for p in pairs])
        bias = float(np.mean([p[1] - p[0] for p in pairs]))
        rows.append({"dim": d, "n": len(pairs), "alpha_ordinal": _r(a),
                     "kappa_quadratic": _r(k), "mean_bias": round(bias, 3)})
        print(f"  {d:<6}{len(pairs):>5}{a:>12.3f}{k:>14.3f}{bias:>10.3f}  {alpha_interpretation(a)}")
    if not second:
        print(f"\n  {console.WARN} 重测一致性的解释前提是**实际间隔 ≥72 小时**。"
              f"若被排期压缩，记忆效应会系统性高估 α 与 κ，实际间隔天数必须如实记录在 README。")
    else:
        print(f"\n  {console.WARN} 这是**探索性** inter-rater：n 很小，且第二评分者的专业水平"
              f"与培训时长都远低于主评分者。α/κ 的置信区间会很宽，**不做推断性结论**。")
    res["per_dimension"] = rows
    _save("reliability.json", res)
    return res


def _r(x):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), 4)


def analyze_judge(pass_id: int = 1, drop_unsupported: bool = False) -> dict:
    pw = load_jsonl(JUDGE_DIR / "judge_pointwise.jsonl")
    pr = load_jsonl(JUDGE_DIR / "judge_pairwise.jsonl")
    print("\n=== LLM-judge vs 人工 ===")
    if not pw and not pr:
        print(f"  {console.WARN} 尚无 judge 结果（data/scores/judge/ 为空）。"
              f"先运行 python src/llm_judge.py --emit pointwise")
        return {"available": False}
    df = load_human(pass_id)
    df, _ = apply_audit_filter(df, pass_id)

    res: dict = {"available": True, "n_pointwise": len(pw), "n_pairwise": len(pr)}
    if drop_unsupported:
        before = len(pw)
        pw = [r for r in pw if not r.get("unsupported_dims")]
        print(f"  --drop-unsupported：剔除 {before - len(pw)} 行无证据支撑的 judge 结果")
        res["dropped_unsupported"] = before - len(pw)

    jd = pd.DataFrame([{ "qid": r["qid"], "model": r["model"], "response_id": r.get("response_id"),
                         **{d: r.get(f"{d}_score") for d in DIMS}} for r in pw])
    if not jd.empty and not df.empty:
        rows = []
        print(f"\n  {'维度':<6}{'n':>5}{'α(ordinal)':>12}{'κ(quadratic)':>14}"
              f"{'judge−人工':>12}  解释")
        for d in DIMS:
            if d not in jd.columns or d not in df.columns:
                continue
            m = jd.merge(df[["qid", "model", d]], on=["qid", "model"],
                         suffixes=("_j", "_h"))
            pairs = [(int(a), int(b)) for a, b in zip(m[f"{d}_j"], m[f"{d}_h"])
                     if pd.notna(a) and pd.notna(b)]
            if len(pairs) < 2:
                continue
            a = krippendorff_alpha_ordinal(pairs)
            k = quadratic_weighted_kappa([p[0] for p in pairs], [p[1] for p in pairs])
            bias = float(np.mean([p[0] - p[1] for p in pairs]))
            rows.append({"dim": d, "n": len(pairs), "alpha_ordinal": _r(a),
                         "kappa_quadratic": _r(k), "judge_minus_human": round(bias, 3)})
            print(f"  {d:<6}{len(pairs):>5}{a:>12.3f}{k:>14.3f}{bias:>12.3f}"
                  f"  {alpha_interpretation(a)}")
        res["per_dimension"] = rows
        sys_bias = [r for r in rows if abs(r["judge_minus_human"]) >= 0.5]
        if sys_bias:
            print(f"\n  {console.FAIL} judge 在 {[r['dim'] for r in sys_bias]} 上存在"
                  f"≥0.5 档的系统偏差。α 高但有系统偏差 → judge 可用作预筛但必须校准；"
                  f"α 低且无偏差 → 是随机噪声。这两种情形的处置完全不同，不能只报 α。")
    if pr:
        pos = _pairwise_position_bias(pr)
        res["pairwise"] = pos
        print(f"\n  === judge pairwise 位置偏置 ===")
        for line in pos["lines"]:
            print(f"  {line}")
    _save("judge_agreement.json", res)
    return res


def _pairwise_position_bias(pr: list[dict]) -> dict:
    by = defaultdict(dict)
    for r in pr:
        by[(r["qid"], r["pair_id"])][r["order"]] = r
    same = rev = tie = 0
    eff: dict[tuple, str] = {}
    for k, d in by.items():
        if 1 not in d or 2 not in d:
            continue
        w1, w2 = d[1]["winner_model"], d[2]["winner_model"]
        if w1 == "tie" or w2 == "tie":
            tie += 1
            eff[k] = "tie"
        elif w1 == w2:
            same += 1
            eff[k] = w1
        else:
            rev += 1
            eff[k] = "tie"       # 反向一律记 tie（prompts/judge_pairwise.md 的规定）
    total = same + rev + tie
    lines = [f"完整题对 {total} 组：同向 {same}、反向 {rev}、含 tie {tie}",
             f"位置敏感率 {rev / total:.1%}" if total else "无完整题对"]
    if total and rev / total > 0.20:
        lines.append(f"{console.WARN} 位置敏感率 >20%：judge 的 pairwise 结论**不能当独立证据**，"
                     f"README 须相应降级表述")
    return {"n_pairs": total, "same": same, "reversed": rev, "tie": tie,
            "position_sensitivity": (rev / total) if total else None,
            "effective_winner": {f"{q}|{p}": w for (q, p), w in eff.items()},
            "lines": lines}


def analyze_rule(pass_id: int = 1) -> dict:
    """规则层 vs 人工：一致率 + 规则层对人工判定的 precision/recall。"""
    met = load_jsonl(RULE_DIR / "rule_metrics.jsonl")
    find = load_jsonl(RULE_DIR / "rule_findings.jsonl")
    print("\n=== 规则层 vs 人工 ===")
    if not met:
        print(f"  {console.WARN} 尚无规则层产物（data/scores/rule/ 为空）。"
              f"先运行 python src/rule_check.py")
        return {"available": False}
    df = load_human(pass_id)
    df, _ = apply_audit_filter(df, pass_id)
    rm = pd.DataFrame(met)
    res: dict = {"available": True, "n_rule": len(rm)}

    if "hit_rate" in df.columns and "rf_hint_rate" in rm.columns:
        m = rm.merge(df[["qid", "model", "hit_rate", "critical_miss"]],
                     on=["qid", "model"], suffixes=("_rule", "_h"))
        if not m.empty:
            both = m[m["hit_rate"].notna() & m["rf_hint_rate"].notna()]
            if not both.empty:
                corr = float(np.corrcoef(both["rf_hint_rate"], both["hit_rate"])[0, 1])
                diff = both["rf_hint_rate"] - both["hit_rate"]
                print(f"  红旗命中率：规则层 rf_hint_rate vs 人工 hit_rate（n={len(both)}）")
                print(f"    Pearson r = {corr:.3f}　平均差 = {diff.mean():+.3f}"
                      f"（规则层偏高 {int((diff > 0).sum())} 份 / 相等 {int((diff == 0).sum())} 份）")
                print(f"    {console.WARN} 规则层只看 detection_hint 是否出现，**不判 hit/partial**，"
                      f"因此系统性偏高是设计使然。它是人工 hit_rate 的**上界**，不是替代值。")
                res["hit_rate"] = {"n": len(both), "pearson_r": _r(corr),
                                   "mean_diff": _r(float(diff.mean())),
                                   "n_rule_higher": int((diff > 0).sum())}
            cm = m[m["critical_miss"].notna() & m["critical_miss_rule"].notna()]
            if not cm.empty:
                tp = int(((cm["critical_miss_rule"] == True) & (cm["critical_miss"] == True)).sum())
                fp = int(((cm["critical_miss_rule"] == True) & (cm["critical_miss"] == False)).sum())
                fn = int(((cm["critical_miss_rule"] == False) & (cm["critical_miss"] == True)).sum())
                prec = tp / (tp + fp) if tp + fp else float("nan")
                rec = tp / (tp + fn) if tp + fn else float("nan")
                print(f"  critical_miss：规则层 vs 人工（n={len(cm)}）　"
                      f"precision={prec:.2f} recall={rec:.2f}　TP={tp} FP={fp} FN={fn}")
                res["critical_miss"] = {"n": len(cm), "precision": _r(prec),
                                        "recall": _r(rec), "tp": tp, "fp": fp, "fn": fn}
    if find:
        f = pd.DataFrame(find)
        print(f"\n  规则层产出 {len(f)} 条错误实例："
              f"confirmed {int((f['verdict'] == 'confirmed').sum())}、"
              f"flag_for_review {int((f['verdict'] == 'flag_for_review').sum())}")
        conf = f[f["verdict"] == "confirmed"]
        if not conf.empty:
            print("  confirmed 分布：" + str(dict(Counter(conf["code"]).most_common())))
        print(f"  {console.WARN} 这两个数**必须分开报**（error_taxonomy.md §2.7）："
              f"把 confirmed 与 flag 混在一起，就是在用不确定性制造虚假的确定性。")
        res["findings"] = {"n": len(f),
                           "confirmed": int((f["verdict"] == "confirmed").sum()),
                           "flag": int((f["verdict"] == "flag_for_review").sum()),
                           "confirmed_by_code": dict(Counter(conf["code"]).most_common())}
    _save("rule_vs_human.json", res)
    return res


# =====================================================================
# 分析：长度偏置
# =====================================================================
def analyze_length(df: pd.DataFrame) -> dict:
    """rubric D5 长度防火墙的统计侧：长度只作协变量，不进任何锚点。

    做三件事：
      1. 未调整胜率（已在 analyze_pairwise 里给出，此处并列）
      2. **长度匹配胜率**：只保留长度比在容差内的题对，看结论是否翻转
      3. 长度差与胜负的点二列相关，附 bootstrap CI——量化"冗长偏置"的大小
    若 (2) 与 (1) 的排序不同，说明原始排序里混入了冗长偏置，必须写进 README。
    """
    print("\n=== 长度偏置诊断 ===")
    if "char_count" not in df.columns or df["char_count"].isna().all():
        print(f"  {console.WARN} 打分表无 char_count 列，无法做长度调整。"
              f"揭盲时 blind_shuffle --unblind 会自动带上。")
        return {"available": False}
    d = df[df["char_count"].notna() & df["D6"].notna()]
    if d.empty:
        print(f"  {console.WARN} 无可用数据")
        return {"available": False}
    cc = {(r.qid, r.model): float(r.char_count) for r in d.itertuples()}
    s6 = {(r.qid, r.model): float(r.D6) for r in d.itertuples()}
    models = sorted(d["model"].unique())
    TOL = 1.4      # 长度比容差：只保留 len_A/len_B ≤ 1.4 的题对
    MATCH_MIN_N = 8   # 低于此样本量，长度匹配后的胜率不具解释力
    rows = []
    for a, b in combinations(models, 2):
        all_o, matched_o, lr = [], [], []
        for q in sorted({k[0] for k in s6}):
            sa, sb = s6.get((q, a)), s6.get((q, b))
            la, lb = cc.get((q, a)), cc.get((q, b))
            if sa is None or sb is None:
                continue
            o = 0 if sa == sb else (1 if sa > sb else -1)
            all_o.append(o)
            if la and lb:
                r = math.log(la / lb)
                lr.append((r, o))
                if abs(r) <= math.log(TOL):
                    matched_o.append(o)
        ci_all = bootstrap_ci(all_o)
        ci_m = bootstrap_ci(matched_o)
        # 点二列相关：长度差（对数）与胜负方向
        if len(lr) >= 3:
            x = np.array([v[0] for v in lr])
            y = np.array([v[1] for v in lr], dtype=float)
            r_pb = float(np.corrcoef(x, y)[0, 1]) if x.std() > 0 and y.std() > 0 else float("nan")
            rng = np.random.default_rng(DEFAULT_SEED)
            bs = []
            for _ in range(2000):
                i = rng.integers(0, len(x), len(x))
                if x[i].std() > 0 and y[i].std() > 0:
                    bs.append(np.corrcoef(x[i], y[i])[0, 1])
            r_lo, r_hi = (np.percentile(bs, [2.5, 97.5]) if bs else (float("nan"),) * 2)
        else:
            r_pb = r_lo = r_hi = float("nan")
        rows.append({"pair": f"{a} vs {b}", "n_all": len(all_o), "n_matched": len(matched_o),
                     "win_all": _r(ci_all.get("point")), "win_matched": _r(ci_m.get("point")),
                     "matched_ci_lo": _r(ci_m.get("lo")), "matched_ci_hi": _r(ci_m.get("hi")),
                     "len_bias_r": _r(r_pb), "len_bias_ci": [_r(r_lo), _r(r_hi)]})
        print(f"  {a} vs {b}")
        print(f"    未调整胜率 {ci_all.get('point', float('nan')):.2f}（n={len(all_o)}）　"
              f"长度匹配后 {_fmt_ci(ci_m)}（n={len(matched_o)}，只保留长度比 ≤{TOL} 的题对）")
        if len(matched_o) < MATCH_MIN_N:
            print(f"    {console.FAIL} 长度匹配后仅剩 {len(matched_o)} 对（<{MATCH_MIN_N}），"
                  f"这个胜率与 CI **不具解释力**，不得写进结论。"
                  f"长度匹配本身会大幅削减样本，这是它的固有代价，不是可以藏起来的技术细节。")
        print(f"    长度差 vs 胜负方向 点二列相关 r={r_pb:+.3f} [{r_lo:+.2f}, {r_hi:+.2f}]"
              f"　（r>0 = 更长的一方更容易赢 = 存在冗长偏置）")
    flipped = [r for r in rows
               if r["win_all"] is not None and r["win_matched"] is not None
               and ((r["win_all"] - 0.5) * (r["win_matched"] - 0.5) < 0)]
    if flipped:
        print(f"\n  {console.FAIL} 长度匹配后**排序翻转**：{[r['pair'] for r in flipped]}"
              f" → 原始排序里混入了冗长偏置，README 必须以此为准并说明。")
    else:
        print(f"\n  {console.OK} 长度匹配后未见排序翻转（但 r 的 CI 仍需一并报告）")
    print(f"\n  {console.WARN} 这只是**长度匹配**，不是完整的协变量调整。"
          f"没做回归调整是因为不引入 statsmodels/scipy；这一局限必须写进 README，"
          f"不能把'长度匹配后结论不变'说成'已排除长度混杂'。")
    res = {"available": True, "tolerance": TOL, "rows": rows,
           "flipped": [r["pair"] for r in flipped]}
    _save("length_bias.json", res)
    return res


# =====================================================================
# 自检：公式实现必须有已知答案的夹具
# =====================================================================
def self_test() -> int:
    """用可以手算/文献对照的夹具验证 α 与 κ 的实现。

    统计公式写错是静默的：数字照样出来，只是错的。所以这两个实现必须
    在已知答案上验过，才有资格去算真实数据。
    """
    print("=== stats 自检：一致性指标实现的已知答案夹具 ===\n")
    ok = True

    def chk(name, got, want, tol=1e-6):
        nonlocal ok
        good = (got is not None and not math.isnan(got)
                and abs(got - want) <= tol) if want is not None else math.isnan(got)
        ok &= good
        print(f"  {console.OK if good else console.FAIL} {name}: 得到 {got}，期望 {want}")

    # 1) 完全一致 → κ = 1，α = 1
    a = [0, 1, 2, 3, 4, 1, 2, 3]
    chk("完全一致 κ_quadratic", quadratic_weighted_kappa(a, a), 1.0)
    chk("完全一致 α_ordinal", krippendorff_alpha_ordinal(list(zip(a, a))), 1.0)

    # 2) 完全反向 → κ < 0
    r = quadratic_weighted_kappa([0, 1, 2, 3, 4], [4, 3, 2, 1, 0])
    good = r < 0
    ok &= good
    print(f"  {console.OK if good else console.FAIL} 完全反向 κ_quadratic={r:.3f}（应 <0）")

    # 3) 相差恒为 1 档：quadratic 权重下应远高于未加权一致性
    x = [0, 1, 2, 3, 4, 0, 1, 2, 3, 4]
    y = [1, 2, 3, 4, 4, 1, 2, 3, 4, 3]
    k = quadratic_weighted_kappa(x, y)
    good = 0.3 < k < 0.95
    ok &= good
    print(f"  {console.OK if good else console.FAIL} 恒差 1 档 κ_quadratic={k:.3f}"
          f"（未加权 κ 会是负数或接近 0，二次权重应显著更高）")

    # 4) α 与 κ 在双方边际分布相同时应接近（不相等是正常的，两者期望项定义不同）
    al = krippendorff_alpha_ordinal(list(zip(x, y)))
    print(f"  [i] 同一组数据 α_ordinal={al:.3f} vs κ_quadratic={k:.3f}"
          f"　（两者不必相等；差异来自期望不一致度的定义不同）")

    # 5) 序数 δ² 矩阵的性质：δ²(0,4) > δ²(0,1)，且对角为 0
    counts = np.array([3.0, 4.0, 5.0, 4.0, 3.0])
    d2 = ordinal_distance_matrix(counts)
    good = d2[0, 0] == 0 and d2[0, 4] > d2[0, 1] and abs(d2[0, 1] - d2[1, 0]) < 1e-9
    ok &= good
    print(f"  {console.OK if good else console.FAIL} 序数 δ² 矩阵：对角=0、对称、"
          f"δ²(0,4)={d2[0,4]:.1f} > δ²(0,1)={d2[0,1]:.1f}")

    # 6) bootstrap：常数序列的 CI 宽度应为 0
    ci = bootstrap_ci([1] * 10, n_boot=500)
    good = abs(ci["width"]) < 1e-9 and abs(ci["point"] - 1.0) < 1e-9
    ok &= good
    print(f"  {console.OK if good else console.FAIL} bootstrap 常数序列："
          f"point={ci['point']} width={ci['width']}")

    # 7) bootstrap 可复现：同 seed 两次结果一致
    o = [1, -1, 0, 1, 1, -1, 0, 1, -1, 1]
    c1, c2 = bootstrap_ci(o, n_boot=500), bootstrap_ci(o, n_boot=500)
    good = c1["lo"] == c2["lo"] and c1["hi"] == c2["hi"]
    ok &= good
    print(f"  {console.OK if good else console.FAIL} bootstrap 同 seed 可复现："
          f"[{c1['lo']:.4f},{c1['hi']:.4f}] == [{c2['lo']:.4f},{c2['hi']:.4f}]")

    # 8) tie 记半分
    chk("胜率 tie 记半分", win_rate([1, 0, -1, 0]), 0.5)

    print()
    if ok:
        print(f"{console.OK} 统计实现自检通过。")
    else:
        print(f"{console.FAIL} 有夹具未通过，修实现而不是改夹具。")
    return 0 if ok else 1


# =====================================================================
# DEMO：在真实打分之前把整条统计流水线跑通
# =====================================================================
DEMO_DIR = SCORES_DIR / "demo"


def set_scores_dir(p: Path, out: Path | None = None) -> None:
    """把 SCORES_DIR / JUDGE_DIR / RULE_DIR（以及可选的 OUT_DIR）一起指向别处。"""
    global SCORES_DIR, JUDGE_DIR, RULE_DIR, OUT_DIR
    SCORES_DIR = Path(p)
    JUDGE_DIR = SCORES_DIR / "judge"
    RULE_DIR = SCORES_DIR / "rule"
    if out is not None:
        OUT_DIR = Path(out)


def make_demo(seed: int = DEFAULT_SEED) -> Path:
    """生成一份**明确标注为 DEMO** 的人工打分表，用来验证统计与报告代码。

    刻意造出三个真实数据里必然出现、但容易被代码漏掉的情形：

      1. **门控前后排序翻转**：gpt5 的 D6_raw 最高，但在多道题上漏了 critical
         红旗，门控后被压到第二。若统计代码只报一套排序，这个翻转就看不见。
      2. **长度与质量正相关**：gpt5 的回答最长且 D5 最高，制造出可被检出的冗长偏置。
      3. **一行质控不合格**：故意留一行 minutes_spent=1，验证 audit 过滤链路。

    中间量先生成、分数再由 score_audit 的查表函数推出，因此除那一行故意做坏的，
    其余行都能通过 score_audit 的查表复核——否则 DEMO 会顺手把审计器测"通过"。
    """
    import score_audit as SA

    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    qs = load_questions()
    qids = sorted(qs) or [f"SIRT-{i:03d}" for i in range(1, 21)]
    models = ["deepseek", "gpt5", "claude"]
    profile = {                       # 每个模型的中间量倾向
        "deepseek": {"err": 0.10, "gap": 0.6, "rf": 0.94, "excl": 0.8, "hedge": 0.6,
                     "ratio": 0.12, "len": 1500, "crit_miss": 0.05, "raw": 3.2},
        "gpt5":     {"err": 0.22, "gap": 1.1, "rf": 0.72, "excl": 1.4, "hedge": 0.9,
                     "ratio": 0.31, "len": 2600, "crit_miss": 0.35, "raw": 3.5},
        "claude":   {"err": 0.16, "gap": 0.9, "rf": 0.85, "excl": 0.5, "hedge": 1.3,
                     "ratio": 0.18, "len": 1800, "crit_miss": 0.15, "raw": 3.0},
    }
    rows = []
    for qi, qid in enumerate(qids):
        n_rf = len(qs.get(qid, {}).get("red_flags") or []) or 4
        crit = [r["rf_id"] for r in (qs.get(qid, {}).get("red_flags") or [])
                if r.get("severity") == "critical"] or ["RF1"]
        for mi, m in enumerate(models):
            p = profile[m]
            r = rng.random
            n_fatal = 1 if r() < p["err"] * 0.3 else 0
            n_core = int(rng.poisson(p["err"] * 1.6))
            n_minor = int(rng.poisson(0.6))
            n_vague = int(rng.poisson(0.4))
            hard, soft, _ = SA.lookup_D1(n_fatal, n_core, n_minor, n_vague)
            D1 = max(hard) if hard else min(soft)

            n_gap = int(rng.poisson(p["gap"]))
            n_inv = 1 if r() < 0.06 else 0
            consistent = not (r() < 0.05)
            hard, soft, _ = SA.lookup_D2(n_gap, n_inv, consistent)
            D2 = max(hard) if hard else min(soft)

            crit_miss = r() < p["crit_miss"]
            hits = {}
            for i in range(1, n_rf + 1):
                rfid = f"RF{i}"
                if crit_miss and rfid == crit[0]:
                    hits[rfid] = "miss"
                else:
                    hits[rfid] = ("hit", "hit", "hit", "partial", "miss")[
                        min(4, int(rng.integers(0, 5 if p["rf"] < 0.8 else 4)))]
            hr = SA.hit_rate_from(hits)
            downgraded = bool(r() < 0.18)
            hard, soft, _ = SA.lookup_D3(hr, any(hits.get(c) == "miss" for c in crit), downgraded)
            D3 = max(hard) if hard else min(soft)

            n_excl = int(rng.poisson(p["excl"]))
            n_unc = int(rng.poisson(p["hedge"] * 3))
            both = bool(r() < (0.75 if m != "gpt5" else 0.45))
            gapd = bool(r() < 0.7)
            n_over = 1 if (r() < 0.2 and p["hedge"] > 1.0) else 0
            hard, soft, _ = SA.lookup_D4_raw(n_excl, both, gapd)
            D4_raw = max(hard) if hard else min(soft)
            D4 = SA.apply_D4_penalty(D4_raw, n_over)

            off_target = bool(r() < 0.03)
            first3 = bool(r() < (0.55 if m == "gpt5" else 0.85))
            ratio = round(float(np.clip(rng.normal(p["ratio"], 0.07), 0.0, 0.7)), 3)
            hard, soft, _ = SA.lookup_D5(off_target, first3, ratio)
            D5 = max(hard) if hard else min(soft)

            D6_raw = int(np.clip(round(rng.normal(p["raw"], 0.6)), 0, 4))
            cap = SA.veto_cap(any(hits.get(c) == "miss" for c in crit), D1, False)
            D6 = min(D6_raw, cap)
            clen = int(rng.normal(p["len"], 320))

            row = {c: "" for c in _DEMO_COLS}
            row.update({
                "rater_id": "R1", "pass_id": "1", "block_id": str((qi + mi) % 6),
                "position": str(mi), "qid": qid,
                "response_id": f"R{qi * 3 + mi + 1:04d}",
                "ts": "2026-10-07T10:00+08:00",
                "model": m, "panel": "core", "char_count": str(clen),
                "model_version": "DEMO", "is_mock": "True",
                "n_fatal": n_fatal, "n_core": n_core, "n_minor": n_minor,
                "n_vague": n_vague, "D1": D1,
                "n_gap": n_gap, "n_inversion": n_inv,
                "conclusion_consistent": str(consistent).lower(), "D2": D2,
                "rf_hits": ";".join(f"{k}={v}" for k, v in hits.items()),
                "hit_rate": f"{hr:.3f}",
                "critical_miss": str(any(hits.get(c) == 'miss' for c in crit)).lower(),
                "modality_downgraded": str(downgraded).lower(), "D3": D3,
                "n_exclusive": n_excl, "n_uncertain": n_unc,
                "both_paths": str(both).lower(), "gap_declared": str(gapd).lower(),
                "n_overhedge": n_over,
                "exemption_note": "豁免「绝对禁忌」这一术语用法" if n_excl else "",
                "D4_raw": D4_raw, "D4": D4,
                "off_target": str(off_target).lower(),
                "conclusion_in_first_3": str(first3).lower(),
                "redundancy_ratio": ratio, "word_count": clen, "D5": D5,
                "D6_raw": D6_raw,
                "veto_applied": SA.veto_reason(any(hits.get(c) == 'miss' for c in crit),
                                               D1, False),
                "D6": D6,
                "off_list_observation": "DEMO 占位：真实打分时这里必须写实际观察或「无」",
                "minutes_spent": "6",
                "rater_note": "DEMO 数据，不得用于任何结论",
            })
            rows.append(row)

    # 故意做坏一行，用来验证 audit 过滤链路
    rows[3]["minutes_spent"] = "1"
    rows[3]["off_list_observation"] = ""

    out = DEMO_DIR / "human_pass1_unblinded.csv"
    with out.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_DEMO_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"{console.WARN} DEMO 打分表 -> {out.relative_to(ROOT)}（{len(rows)} 行）")
    print("     这是合成数据，只为验证统计与报告代码。任何基于它的数字都无意义。")
    print("     刻意内置三个情形：门控前后排序翻转、长度与 D5 正相关、1 行质控不合格。")
    return out


_DEMO_COLS = ["model", "panel", "char_count", "model_version", "is_mock",
              "rater_id", "pass_id", "block_id", "position", "qid", "response_id", "ts",
              "n_fatal", "n_core", "n_minor", "n_vague", "D1",
              "n_gap", "n_inversion", "conclusion_consistent", "D2",
              "rf_hits", "hit_rate", "critical_miss", "modality_downgraded", "D3",
              "n_exclusive", "n_uncertain", "both_paths", "gap_declared", "n_overhedge",
              "exemption_note", "D4_raw", "D4",
              "off_target", "conclusion_in_first_3", "redundancy_ratio", "word_count", "D5",
              "D6_raw", "veto_applied", "D6",
              "off_list_observation", "minutes_spent", "rater_note"]


# =====================================================================
# 入口
# =====================================================================
def main(argv: list[str] | None = None) -> int:
    global DEFAULT_SEED, N_BOOT
    ap = argparse.ArgumentParser(description="统计层（不输出任何加权总分）")
    ap.add_argument("--pass", dest="pass_id", type=int, default=1)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--pairwise", action="store_true")
    ap.add_argument("--dimensions", action="store_true")
    ap.add_argument("--reliability", action="store_true")
    ap.add_argument("--judge", action="store_true")
    ap.add_argument("--rule", action="store_true")
    ap.add_argument("--length", action="store_true")
    ap.add_argument("--heatmap", action="store_true")
    ap.add_argument("--second-rater", default="", help="第二评分者的 csv 路径（探索性 inter-rater）")
    ap.add_argument("--drop-unsupported", action="store_true",
                    help="剔除 judge 结果中无有效证据支撑的行")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--n-boot", type=int, default=N_BOOT)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--make-demo", action="store_true",
                    help="生成 DEMO 合成打分表（仅用于验证统计与报告代码）")
    ap.add_argument("--demo", action="store_true",
                    help="在 data/scores/demo/ 上运行（数据无意义，只为跑通流水线）")
    ap.add_argument("--scores-dir", default="", help="打分数据目录（默认 data/scores）")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()
    if args.make_demo:
        make_demo(args.seed)
        return 0
    if args.demo:
        set_scores_dir(DEMO_DIR, ROOT / "data" / "stats" / "demo")
        print(f"{console.WARN} DEMO 模式：读 {DEMO_DIR.relative_to(ROOT)}/，"
              f"所有数字都来自合成数据，无意义。")
        if not (DEMO_DIR / "human_pass1_unblinded.csv").exists():
            make_demo(args.seed)
    elif args.scores_dir:
        set_scores_dir(Path(args.scores_dir))
        print(f"[i] 打分数据目录：{SCORES_DIR}")

    DEFAULT_SEED, N_BOOT = args.seed, args.n_boot

    flags = [args.pairwise, args.dimensions, args.reliability, args.judge,
             args.rule, args.length, args.heatmap]
    do_all = args.all or not any(flags)

    print(f"stats.py　seed={DEFAULT_SEED}　bootstrap={N_BOOT} 次　"
          f"{datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')}")
    print(f"{console.WARN} 本层**不输出任何加权总分**（rubric §0 总则 3）。")

    if args.reliability or do_all:
        analyze_reliability(args.pass_id, args.second_rater)
    if args.judge or do_all:
        analyze_judge(args.pass_id, args.drop_unsupported)
    if args.rule or do_all:
        analyze_rule(args.pass_id)

    need_df = args.pairwise or args.dimensions or args.length or args.heatmap or do_all
    if need_df:
        df = load_human(args.pass_id)
        df, ainfo = apply_audit_filter(df, args.pass_id)
        print(f"\n载入人工打分 {len(df)} 行"
              + (f"（质控剔除 {ainfo['dropped']} 行：invalid/rejected）" if ainfo["dropped"] else ""))
        if df.empty:
            print(f"{console.FAIL} 没有可用的人工打分数据，模型间比较无法进行。")
            return 1
        if args.pairwise or do_all:
            analyze_pairwise(df)
        if args.dimensions or args.heatmap or do_all:
            analyze_dimensions(df)
        if args.length or do_all:
            analyze_length(df)

    print(f"\n产物 -> {OUT_DIR.relative_to(ROOT)}/")
    print(f"{console.WARN} data/stats/ 下的文件含明文模型名，盲测期间不得查看。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
