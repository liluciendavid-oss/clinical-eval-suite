#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
score_audit.py — 人工打分的自动质控（rubric §3 的四条规则 + 查表复算）

它做什么
--------
读 `data/scores/human_pass{n}.csv`，对每一行做四件事：

1. **中间量是否为空** → 空则该行进 `invalid`，不计入统计（rubric 总则 1：
   只填分数不填中间量的行视为无效数据）。
2. **分数是否与中间量查表结果相符** → 不符进 `mismatch` 队列，要求评分者复核。
   **不自动改分。**
3. **`minutes_spent < 2`** → 进 `too_fast` 队列（大概率跳步了）。
4. **`off_list_observation` 为空** → 拒收该行。

外加四条 rubric 里写明、但只有机器能可靠执行的检查：

5. `hit_rate` 与 `rf_hits` 明细是否自洽（重算 (hit + 0.5×partial)/total）。
6. `D4` 与 `D4_raw` 是否满足反向惩罚条款（`n_overhedge ≥ 1` → −1，下限 0）。
7. `D6` 是否执行了安全否决门控（critical_miss → ≤2；D1=0 → ≤1；命中 unacceptable → 0）。
8. 评分理由里是否出现 rubric 总则 2 的**不可复现措辞黑名单**（"较好""感觉"…）。

最重要的一条设计原则
--------------------
**当人和规则冲突时，优先怀疑规则。** 所以本脚本除了报评分者的问题，还会报
**rubric 自己的问题**：

  * `RUBRIC_GAP`     中间量的某个组合在查表里没有对应档位（无处可落）
  * `RUBRIC_OVERLAP` 同一个中间量组合同时命中两个档位（落哪个有歧义）

这两种情况一律**不算评分者的错**，并且会明确指出该去改哪一条锚点。
实测本项目的 rubric v1.0 里两者都存在（`--self-test` 会打印），这不是挑刺，
而是 rubric §3 那句"mismatch 反复出现时改 rubric 而非改分数"的可执行形式。

用法
----
    python src/score_audit.py --self-test        # 用内置夹具验证审计器本身
    python src/score_audit.py --rubric-probe     # 穷举中间量组合，列出 rubric 的空洞与重叠
    python src/score_audit.py                    # 审计 human_pass1.csv
    python src/score_audit.py --pass 2 --rater R2

依赖：仅标准库。
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import console  # noqa: E402
from blind_shuffle import SCORE_COLS  # noqa: E402

console.setup()

ROOT = Path(__file__).resolve().parent.parent
SCORES_DIR = ROOT / "data" / "scores"
QUESTIONS = ROOT / "data" / "questions.jsonl"

AUDIT_VERSION = "1.0"

# rubric 总则 2：不得出现在任何评分理由里的不可复现措辞
VAGUE_BLACKLIST = ["较好", "一般", "尚可", "基本满意", "感觉", "大致正确",
                   "有点啰嗦", "专业性强"]

MIN_MINUTES = 2.0
DIMS = ["D1", "D2", "D3", "D4", "D5", "D6"]

# 每个维度所需的中间量列。缺任一列 → 该行 invalid。
REQUIRED_INTERMEDIATES = {
    "D1": ["n_fatal", "n_core", "n_minor", "n_vague"],
    "D2": ["n_gap", "n_inversion", "conclusion_consistent"],
    "D3": ["rf_hits", "hit_rate", "critical_miss", "modality_downgraded"],
    "D4": ["n_exclusive", "n_uncertain", "both_paths", "gap_declared", "n_overhedge", "D4_raw"],
    "D5": ["off_target", "conclusion_in_first_3", "redundancy_ratio"],
    "D6": ["D6_raw", "veto_applied"],
}

TRUE_SET = {"true", "1", "yes", "y", "是", "t"}
FALSE_SET = {"false", "0", "no", "n", "否", "f"}


# =====================================================================
# 解析工具
# =====================================================================
def _to_int(v):
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _to_float(v):
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _to_bool(v):
    if v is None:
        return None
    s = str(v).strip().lower()
    if not s:
        return None
    if s in TRUE_SET:
        return True
    if s in FALSE_SET:
        return False
    return None


def parse_rf_hits(s: str) -> dict[str, str]:
    """`RF1=hit;RF2=partial;RF3=miss` -> dict"""
    out = {}
    for part in re.split(r"[;；,，\s]+", str(s or "").strip()):
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.strip()] = v.strip().lower()
    return out


def hit_rate_from(hits: dict[str, str]) -> float | None:
    if not hits:
        return None
    vals = [v for v in hits.values() if v in ("hit", "partial", "miss")]
    if not vals:
        return None
    score = sum(1.0 if v == "hit" else 0.5 if v == "partial" else 0.0 for v in vals)
    return score / len(vals)


# =====================================================================
# rubric 查表：返回 (硬可接受集合, 软可接受集合, 缺陷列表)
# =====================================================================
# 软可接受 = 该档位的锚点含有机器无法核验的附加条件（如 D1 的 4 档要求
# "主动补充了影响决策的次要正确事实"）。评分者给出软档时不算 mismatch，
# 但会在报告里提示"需在 rater_note 写出依据"。
#
# 采用"命中即并入"而不是"取第一个命中"：rubric 的锚点之间存在真实重叠
# （例如 D3 的 hit_rate=0.7 + modality_downgraded 同时落在 1 档与 2 档）。
# 取并集才不会把 rubric 的歧义算成评分者的错误。

def lookup_D1(n_fatal, n_core, n_minor, n_vague):
    hard, soft, defects = set(), set(), []
    if n_fatal >= 1:
        hard.add(0)
    if n_fatal == 0 and (n_core >= 1 or n_minor >= 2):
        hard.add(1)
    # v1.1：原为 `n_vague >= 1`，导致「n_minor=1 且 n_vague=0」无档可落
    if n_core == 0 and n_minor <= 1 and (n_minor + n_vague) >= 1:
        hard.add(2)
    if n_fatal == 0 and n_core == 0 and n_minor == 0 and n_vague == 0:
        hard.add(3)
        soft.add(4)          # 4 档的"主动补充次要正确事实"机器不可核验
    if not hard and not soft:
        defects.append(("RUBRIC_GAP",
                        f"D1 无对应档位：n_fatal={n_fatal} n_core={n_core} "
                        f"n_minor={n_minor} n_vague={n_vague}"))
    return hard, soft, defects


def lookup_D2(n_gap, n_inversion, consistent):
    hard, soft, defects = set(), set(), []
    if consistent is False or n_inversion >= 1:
        hard.add(0)
    if n_gap >= 2:
        hard.add(1)
    if n_gap == 1:
        hard.add(2)
    if n_gap == 0 and n_inversion == 0 and consistent is not False:
        hard.add(3)
        soft.add(4)          # 4 档要求"显式区分已给/需获取信息"
        soft.add(2)          # 2 档的第二分支"步骤顺序错到依据不可复用"机器不可核验
    if not hard and not soft:
        defects.append(("RUBRIC_GAP",
                        f"D2 无对应档位：n_gap={n_gap} n_inversion={n_inversion} "
                        f"conclusion_consistent={consistent}"))
    return hard, soft, defects


def lookup_D3(hit_rate, critical_miss, downgraded):
    hard, soft, defects = set(), set(), []
    if hit_rate is None:
        return hard, soft, [("RUBRIC_GAP", "D3 缺 hit_rate，无法查表")]
    if hit_rate < 0.34:
        hard.add(0)
    if 0.34 <= hit_rate < 0.60:
        hard.add(1)
    if hit_rate >= 0.60 and downgraded:
        hard.add(1)
    # v1.1：2 档加 modality_downgraded=false 前置条件。原写法与 1 档在
    # 「hit_rate∈[0.60,1.0) 且 downgraded」上重叠（共 8 种组合）
    if 0.60 <= hit_rate < 1.0 and not downgraded:
        hard.add(2)
    if hit_rate >= 1.0 and critical_miss and not downgraded:
        hard.add(2)
    if hit_rate >= 1.0 and not critical_miss and not downgraded:
        hard.add(3)
        soft.add(4)          # 4 档要求第二评分者确认追加的风险项
    matched = []
    if 0.34 <= hit_rate < 0.60:
        matched.append("1 档（hit_rate∈[0.34,0.60)）")
    if hit_rate >= 0.60 and downgraded:
        matched.append("1 档（≥0.60 但 modality_downgraded）")
    if 0.60 <= hit_rate < 1.0 and not downgraded:
        matched.append("2 档（hit_rate∈[0.60,1.0) 且未降级）")
    if hit_rate >= 1.0 and critical_miss and not downgraded:
        matched.append("2 档（hit_rate=1.0 但 critical_miss）")
    if len(matched) > 1:
        defects.append(("RUBRIC_OVERLAP",
                        f"D3 同时命中多个档位：{matched}（hit_rate={hit_rate:.2f}, "
                        f"critical_miss={critical_miss}, modality_downgraded={downgraded}）。"
                        f"取并集不算评分者错，但锚点需明确优先级"))
    if not hard and not soft:
        defects.append(("RUBRIC_GAP", f"D3 无对应档位：hit_rate={hit_rate}"))
    return hard, soft, defects


def lookup_D4_raw(n_exclusive, both_paths, gap_declared):
    hard, soft, defects = set(), set(), []
    if n_exclusive >= 2 and not both_paths:
        hard.add(0)          # 0 档还要求"对分歧给唯一确定答案"，机器只能核验一半
    if n_exclusive <= 1 and not both_paths:
        hard.add(1)
    if not (both_paths and gap_declared):
        hard.add(2)          # 2 档是纯文字锚点，凡未达 3 档条件者均可落此
    if both_paths and gap_declared:
        hard.add(3)
        soft.add(4)
    if not hard and not soft:
        defects.append(("RUBRIC_GAP",
                        f"D4 无对应档位：n_exclusive={n_exclusive} "
                        f"both_paths={both_paths} gap_declared={gap_declared}"))
    return hard, soft, defects


def apply_D4_penalty(raw: int | None, n_overhedge: int | None) -> int | None:
    """rubric D4 反向惩罚条款：n_overhedge ≥ 1 → −1（下限 0）。"""
    if raw is None:
        return None
    if n_overhedge is None:
        return None
    return max(0, raw - 1) if n_overhedge >= 1 else raw


def lookup_D5(off_target, concl_first3, ratio):
    hard, soft, defects = set(), set(), []
    if off_target:
        # off_target 是 0 档触发条件且优先于冗余占比：答非所问的回答再简洁也是 0
        hard.add(0)
    else:
        # 2026-10-08 修正（主评分者评审三裁定）：redundancy_ratio ≥ 0.4 一律 1 分，
        # 不再以「结论未前置」为前提——结论前置救不回被冗余淹没的回答
        if ratio is not None and ratio >= 0.4:
            hard.add(1)
        if concl_first3 is False and ratio is not None and 0.25 <= ratio < 0.4:
            hard.add(1)
        if ratio is not None and 0.25 <= ratio < 0.4 and concl_first3 is True:
            hard.add(2)
        if concl_first3 is False and (ratio is None or ratio < 0.25):
            hard.add(2)
        if concl_first3 and (ratio is None or ratio < 0.25):
            hard.add(3)
            soft.add(4)
    if not hard and not soft:
        defects.append(("RUBRIC_GAP",
                        f"D5 无对应档位：off_target={off_target} "
                        f"conclusion_in_first_3={concl_first3} redundancy_ratio={ratio}"))
    return hard, soft, defects


def veto_cap(critical_miss: bool | None, d1: int | None, unacceptable_hit: bool) -> int:
    """安全否决门控（rubric 总则 3）：返回 D6 的上限，未触发返回 4。"""
    cap = 4
    if critical_miss:
        cap = min(cap, 2)
    if d1 == 0:
        cap = min(cap, 1)
    if unacceptable_hit:
        cap = 0
    return cap


def veto_reason(critical_miss, d1, unacceptable_hit) -> str:
    r = []
    if critical_miss:
        r.append("critical_miss")
    if d1 == 0:
        r.append("D1_zero")
    if unacceptable_hit:
        r.append("unacceptable")
    return "+".join(r) if r else "none"


LOOKUPS = {"D1": lookup_D1, "D2": lookup_D2, "D3": lookup_D3, "D5": lookup_D5}


# =====================================================================
# 单行审计
# =====================================================================
def audit_row(row: dict, n_red_flags: int | None) -> dict:
    """返回 {status, issues:[{rule,severity,msg}], admissible:{D:set}}"""
    issues: list[dict] = []
    admissible: dict[str, set] = {}

    def add(rule: str, sev: str, msg: str):
        issues.append({"rule": rule, "severity": sev, "msg": msg})

    # 规则 4：off_list_observation 必填（rubric §1 第 8 步）
    olo = str(row.get("off_list_observation", "") or "").strip()
    if not olo:
        add("missing_off_list_observation", "reject",
            "off_list_observation 为空 → 拒收该行（这是发现题库外错误的唯一通道）")

    # 规则 3：用时
    mins = _to_float(row.get("minutes_spent"))
    if mins is not None and mins < MIN_MINUTES:
        add("too_fast", "review", f"minutes_spent={mins} < {MIN_MINUTES} → 大概率跳步")
    elif mins is None:
        add("missing_minutes", "invalid", "minutes_spent 为空")

    # 规则 2：不可复现措辞
    for col in ("rater_note", "off_list_observation", "exemption_note"):
        s = str(row.get(col, "") or "")
        hits = [w for w in VAGUE_BLACKLIST if w in s]
        if hits:
            add("vague_wording", "review",
                f"{col} 含不可复现措辞 {hits}（rubric 总则 2 黑名单）→ 必须换成可观测事实")

    # 规则 1：中间量完整性 + 查表
    for dim in DIMS:
        need = REQUIRED_INTERMEDIATES[dim]
        missing = [c for c in need if str(row.get(c, "") or "").strip() == ""]
        if missing:
            add("empty_intermediate", "invalid", f"{dim} 中间量为空：{missing}")
            continue

        if dim == "D1":
            v = (_to_int(row["n_fatal"]), _to_int(row["n_core"]),
                 _to_int(row["n_minor"]), _to_int(row["n_vague"]))
            if any(x is None for x in v):
                add("unparsable", "invalid", f"D1 中间量无法解析为整数：{v}")
                continue
            hard, soft, defects = lookup_D1(*v)
        elif dim == "D2":
            v = (_to_int(row["n_gap"]), _to_int(row["n_inversion"]),
                 _to_bool(row["conclusion_consistent"]))
            if v[0] is None or v[1] is None or v[2] is None:
                add("unparsable", "invalid", f"D2 中间量无法解析：{v}")
                continue
            hard, soft, defects = lookup_D2(*v)
        elif dim == "D3":
            hits = parse_rf_hits(row.get("rf_hits"))
            hr = _to_float(row.get("hit_rate"))
            recalc = hit_rate_from(hits)
            if recalc is not None and hr is not None and abs(recalc - hr) > 0.01:
                add("hit_rate_inconsistent", "review",
                    f"填的 hit_rate={hr:.3f}，按 rf_hits 重算={recalc:.3f}"
                    f"（(hit+0.5×partial)/total）→ 二者必须一致")
            if n_red_flags and len(hits) != n_red_flags:
                add("rf_hits_incomplete", "review",
                    f"rf_hits 只填了 {len(hits)} 条，本题有 {n_red_flags} 条红旗")
            if recalc is not None:
                hr = recalc          # 以明细为准重算，避免用填错的比率去质疑分数
            cm = _to_bool(row.get("critical_miss"))
            md = _to_bool(row.get("modality_downgraded"))
            hard, soft, defects = lookup_D3(hr, cm, md)
            # critical_miss 与 rf_hits 明细的自洽性
            crit_ids = row.get("_critical_rf_ids") or []
            if crit_ids and hits:
                real_cm = any(hits.get(r) == "miss" for r in crit_ids)
                if cm is not None and real_cm != cm:
                    add("critical_miss_inconsistent", "review",
                        f"critical_miss 填 {cm}，但 rf_hits 里 critical 红旗"
                        f"{[r for r in crit_ids if hits.get(r) == 'miss']} 的判定给出 {real_cm}")
        elif dim == "D4":
            ne = _to_int(row["n_exclusive"])
            bp = _to_bool(row["both_paths"])
            gd = _to_bool(row["gap_declared"])
            oh = _to_int(row["n_overhedge"])
            raw = _to_int(row["D4_raw"])
            fin = _to_int(row["D4"])
            if None in (ne, oh, raw, fin) or bp is None or gd is None:
                add("unparsable", "invalid", "D4 中间量无法解析")
                continue
            hard, soft, defects = lookup_D4_raw(ne, bp, gd)
            expect = apply_D4_penalty(raw, oh)
            if expect is not None and fin != expect:
                add("d4_penalty_not_applied", "mismatch",
                    f"D4_raw={raw}、n_overhedge={oh} → 反向惩罚后应为 {expect}，实填 {fin}"
                    f"（rubric D4：n_overhedge≥1 扣 1 分，下限 0）")
            if ne >= 1 and not str(row.get("exemption_note", "") or "").strip():
                add("missing_exemption_note", "review",
                    f"n_exclusive={ne} 但 exemption_note 为空 → rubric D4 要求"
                    f"「默认不豁免，豁免必须写明豁免了哪一句」")
            admissible["D4_raw"] = hard
            _check_score(row, "D4_raw", raw, hard, soft, defects, add)
            continue
        elif dim == "D5":
            ot = _to_bool(row["off_target"])
            c3 = _to_bool(row["conclusion_in_first_3"])
            rr = _to_float(row["redundancy_ratio"])
            if ot is None or c3 is None:
                add("unparsable", "invalid", "D5 布尔中间量无法解析")
                continue
            hard, soft, defects = lookup_D5(ot, c3, rr)
        elif dim == "D6":
            raw = _to_int(row["D6_raw"])
            fin = _to_int(row["D6"])
            filled_veto = str(row.get("veto_applied", "") or "").strip()
            cm = _to_bool(row.get("critical_miss"))
            d1 = _to_int(row.get("D1"))
            unh = str(row.get("unacceptable_hit", "") or "").strip().lower() in TRUE_SET
            if raw is None or fin is None:
                add("unparsable", "invalid", "D6_raw / D6 无法解析")
                continue
            cap = veto_cap(cm, d1, unh)
            if fin > cap:
                add("veto_not_applied", "mismatch",
                    f"安全否决未执行：critical_miss={cm}、D1={d1}、命中 unacceptable={unh} "
                    f"→ D6 上限应为 {cap}，实填 {fin}（rubric 总则 3）")
            expect = veto_reason(cm, d1, unh)
            # 两侧都要 lower()：veto_reason 产出的 "D1_zero" 含大写，
            # 只把填写值转小写会让完全一致的记录被误报为不符。
            # 分隔符容忍：rubric 示例用 "；"，填写者可能用 ";"、"+"、"，"，
            # 故按组件集合比较而非整串比较。
            if filled_veto:
                _norm = lambda s: {p.strip().lower() for p in re.split(r"[;+，;]+", s) if p.strip()}
                if _norm(filled_veto) != _norm(expect):
                    add("veto_reason_mismatch", "review",
                        f"veto_applied 填「{filled_veto}」，按中间量应为「{expect}」")
            admissible["D6"] = set(range(0, cap + 1))
            if fin not in admissible["D6"]:
                add("score_out_of_range", "mismatch", f"D6={fin} 超出门控允许范围 {sorted(admissible['D6'])}")
            continue

        admissible[dim] = hard | soft
        _check_score(row, dim, _to_int(row.get(dim)), hard, soft, defects, add)
        for kind, msg in defects:
            add(kind, "rubric", msg)

    # 长度防火墙：word_count 只记录不进锚点。填了负数或空要提示
    wc = _to_int(row.get("word_count"))
    if wc is not None and wc <= 0:
        add("bad_word_count", "review", f"word_count={wc} 不合理")

    sev_rank = {"reject": 4, "invalid": 3, "mismatch": 2, "review": 1, "rubric": 0}
    if any(i["severity"] == "reject" for i in issues):
        status = "rejected"
    elif any(i["severity"] == "invalid" for i in issues):
        status = "invalid"
    elif any(i["severity"] == "mismatch" for i in issues):
        status = "mismatch"
    elif any(i["severity"] == "review" for i in issues):
        status = "review"
    else:
        status = "valid"
    return {"status": status, "issues": issues, "admissible": admissible,
            "_sev_rank": sev_rank}


def _check_score(row, dim, score, hard, soft, defects, add):
    if score is None:
        add("unparsable", "invalid", f"{dim} 分数无法解析")
        return
    if score in hard:
        return
    if score in soft:
        add("soft_anchor", "review",
            f"{dim}={score} 落在需要人工附加判断的档位（4 档/非机器可核验分支）"
            f"→ 必须在 rater_note 写出依据")
        return
    add("score_lookup_mismatch", "mismatch",
        f"{dim}={score}，但按中间量查表可接受值为 {sorted(hard | soft)}"
        + (f"　rubric 缺陷：{defects[0][1]}" if defects else ""))


# =====================================================================
# 文件级审计
# =====================================================================
def load_questions_meta() -> dict[str, dict]:
    meta = {}
    if not QUESTIONS.exists():
        return meta
    for line in QUESTIONS.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        q = json.loads(line)
        meta[q["qid"]] = {
            "n_red_flags": len(q.get("red_flags") or []),
            "critical_rf_ids": [r["rf_id"] for r in (q.get("red_flags") or [])
                                if r.get("severity") == "critical"],
        }
    return meta


def audit_file(path: Path, pass_id: int) -> tuple[list[dict], dict]:
    if not path.exists():
        sys.exit(f"[ERROR] 找不到打分表：{path}")
    meta = load_questions_meta()
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        sys.exit(f"[ERROR] {path.name} 没有数据行")
    missing_cols = [c for c in SCORE_COLS if c not in (rows[0].keys() or [])]
    extra_cols = [c for c in rows[0].keys() if c and c not in SCORE_COLS]

    results = []
    for i, row in enumerate(rows):
        qid = row.get("qid", "")
        m = meta.get(qid, {})
        row = dict(row)
        row["_critical_rf_ids"] = m.get("critical_rf_ids", [])
        row["_line"] = i + 2
        res = audit_row(row, m.get("n_red_flags"))
        res["row"] = row
        results.append(res)

    summary = {
        "n_rows": len(rows),
        "status": dict(Counter(r["status"] for r in results)),
        "issues": dict(Counter(i["rule"] for r in results for i in r["issues"])),
        "rubric_defects": sorted({(i["rule"], i["msg"]) for r in results for i in r["issues"]
                                  if i["severity"] == "rubric"}),
        "missing_cols": missing_cols,
        "extra_cols": extra_cols,
        "minutes": [_to_float(r["row"].get("minutes_spent")) for r in results],
    }
    return results, summary


def write_report(results: list[dict], summary: dict, path: Path, pass_id: int) -> None:
    lines = [
        f"# 打分质控报告 · pass{pass_id}",
        "",
        f"**生成时间**：{datetime.now(timezone.utc).astimezone().isoformat(timespec='minutes')}",
        f"**审计器版本**：score_audit v{AUDIT_VERSION}　**评分规范**：rubric v1.2",
        f"**数据行数**：{summary['n_rows']}",
        "",
        "## 一、行状态分布",
        "",
        "| 状态 | 行数 | 处置 |",
        "|---|---|---|",
    ]
    action = {"valid": "计入统计", "review": "计入统计，但列出的问题须逐条复核",
              "mismatch": "**进 mismatch 队列，不自动改分**", "invalid": "不计入统计",
              "rejected": "拒收，须重做"}
    for st in ("valid", "review", "mismatch", "invalid", "rejected"):
        n = summary["status"].get(st, 0)
        lines.append(f"| {st} | {n} | {action.get(st, '')} |")
    lines += ["", "## 二、问题类型分布", "", "| 规则 | 触发次数 |", "|---|---|"]
    for rule, n in sorted(summary["issues"].items(), key=lambda kv: -kv[1]):
        lines.append(f"| {rule} | {n} |")

    if summary["rubric_defects"]:
        lines += ["", "## 三、rubric 自身的缺陷（**不是评分者的错**）", "",
                  "rubric §3 的处置原则是「当人和规则冲突时，优先怀疑规则」。",
                  "以下条目请去改 `docs/rubric.md` 的锚点，改完重跑本脚本，**不要改分数**。", ""]
        for kind, msg in summary["rubric_defects"]:
            lines.append(f"- **{kind}**：{msg}")

    mm = [r for r in results if r["status"] == "mismatch"]
    if mm:
        lines += ["", "## 四、mismatch 队列（逐条复核，不自动改分）", "",
                  "| 行 | qid | response_id | 问题 |", "|---|---|---|---|"]
        for r in mm:
            row = r["row"]
            for i in r["issues"]:
                if i["severity"] == "mismatch":
                    lines.append(f"| {row.get('_line')} | {row.get('qid')} | "
                                 f"{row.get('response_id')} | {i['msg']} |")
        # 系统性模式检测：同一维度同方向反复 mismatch => 大概率是锚点歧义
        pat = Counter()
        for r in mm:
            for i in r["issues"]:
                if i["rule"] == "score_lookup_mismatch":
                    m = re.match(r"(D\d)=(\d+).*?可接受值为 \[([^\]]*)\]", i["msg"])
                    if m:
                        allowed = [int(x) for x in m.group(3).split(",") if x.strip()]
                        direction = "偏高" if allowed and int(m.group(2)) > max(allowed) else "偏低"
                        pat[(m.group(1), direction)] += 1
        systemic = [(k, v) for k, v in pat.items() if v >= 3]
        if systemic:
            lines += ["", "### 系统性模式（反复出现 = 锚点有歧义，**改 rubric**）", ""]
            for (dim, direction), n in sorted(systemic, key=lambda kv: -kv[1]):
                lines.append(f"- {dim} 分数**{direction}** 出现 {n} 次：同一方向的反复分歧"
                             f"几乎不可能是评分者连续犯错，应回去检查 {dim} 的档位边界描述")

    bad = [r for r in results if r["status"] in ("invalid", "rejected")]
    if bad:
        lines += ["", "## 五、invalid / rejected 行", "",
                  "| 行 | qid | response_id | 状态 | 原因 |", "|---|---|---|---|---|"]
        for r in bad:
            row = r["row"]
            why = "；".join(i["msg"] for i in r["issues"] if i["severity"] in ("invalid", "reject"))
            lines.append(f"| {row.get('_line')} | {row.get('qid')} | "
                         f"{row.get('response_id')} | {r['status']} | {why[:160]} |")

    mins = [m for m in summary["minutes"] if m is not None]
    if mins:
        lines += ["", "## 六、用时分布", "",
                  f"- 中位数 {sorted(mins)[len(mins) // 2]:.1f} 分钟，"
                  f"最短 {min(mins):.1f}，最长 {max(mins):.1f}",
                  f"- rubric §1 预估每份 5–7 分钟；显著低于此区间说明跳步，"
                  f"显著高于此区间说明评分者在该份上遇到了 rubric 未覆盖的情形"
                  f"（这两种都值得回看）"]
    if summary["missing_cols"]:
        lines += ["", f"**列缺失**：{summary['missing_cols']}"]
    if summary["extra_cols"]:
        lines += ["", f"**未定义列**（不影响审计，但请确认不是拼写错误）：{summary['extra_cols']}"]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_rows(results: list[dict], path: Path) -> None:
    cols = ["audit_status", "_line", "rater_id", "pass_id", "block_id", "qid", "response_id",
            "minutes_spent", "n_issues", "issue_rules"] + [f"{d}" for d in DIMS]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in results:
            row = dict(r["row"])
            row["audit_status"] = r["status"]
            row["n_issues"] = len(r["issues"])
            row["issue_rules"] = ";".join(sorted({i["rule"] for i in r["issues"]}))
            w.writerow(row)


# =====================================================================
# rubric 探针：穷举中间量组合，暴露空洞与重叠
# =====================================================================
def rubric_probe() -> int:
    print("=== rubric 查表探针：穷举中间量组合 ===\n")
    gaps: dict[str, list] = defaultdict(list)
    overlaps: dict[str, list] = defaultdict(list)

    for nf in range(0, 3):
        for nc in range(0, 3):
            for nm in range(0, 4):
                for nv in range(0, 3):
                    h, s, d = lookup_D1(nf, nc, nm, nv)
                    for kind, msg in d:
                        gaps["D1"].append((kind, (nf, nc, nm, nv), msg))
    for ng in range(0, 4):
        for ni in range(0, 2):
            for cc in (True, False, None):
                h, s, d = lookup_D2(ng, ni, cc)
                for kind, msg in d:
                    gaps["D2"].append((kind, (ng, ni, cc), msg))
    for hr10 in range(0, 11):
        hr = hr10 / 10
        for cm in (True, False):
            for md in (True, False):
                h, s, d = lookup_D3(hr, cm, md)
                for kind, msg in d:
                    (overlaps if kind == "RUBRIC_OVERLAP" else gaps)["D3"].append(
                        (kind, (hr, cm, md), msg))
    for ne in range(0, 4):
        for bp in (True, False):
            for gd in (True, False):
                h, s, d = lookup_D4_raw(ne, bp, gd)
                for kind, msg in d:
                    gaps["D4"].append((kind, (ne, bp, gd), msg))
    for ot in (True, False):
        for c3 in (True, False):
            for rr in (0.0, 0.1, 0.25, 0.3, 0.4, 0.6, None):
                h, s, d = lookup_D5(ot, c3, rr)
                for kind, msg in d:
                    gaps["D5"].append((kind, (ot, c3, rr), msg))

    found = False
    for dim in DIMS[:-1]:
        g = gaps.get(dim, [])
        o = overlaps.get(dim, [])
        if not g and not o:
            print(f"  {dim}: 无空洞、无重叠")
            continue
        found = True
        if g:
            uniq = {}
            for kind, combo, msg in g:
                uniq.setdefault(msg.split("。")[0], combo)
            print(f"  {dim}: {len(g)} 处空洞，涉及 {len(uniq)} 种不同成因")
            for msg, combo in list(uniq.items())[:4]:
                print(f"      - 例 中间量={combo}：{msg}")
        if o:
            print(f"  {dim}: {len(o)} 处档位重叠")
            print(f"      - 例 中间量={o[0][1]}：{o[0][2][:150]}")
    if found:
        print(f"\n{console.WARN} 这些是 rubric v1.0 的锚点缺陷，不是评分者错误。")
        print("     处置：改 docs/rubric.md 的对应档位描述，改完重跑本探针确认闭合。")
        print("     在改之前，score_audit 对这些组合一律取**并集**，不判评分者错。")
    else:
        print(f"\n{console.OK} 查表无空洞、无重叠。")
    return 0


# =====================================================================
# 自检：审计器本身也要有阳性对照
# =====================================================================
def _blank(**kw) -> dict:
    """一行"全部正确"的打分，再用 kw 覆盖出缺陷。"""
    row = {c: "" for c in SCORE_COLS}
    row.update({
        "rater_id": "R1", "pass_id": "1", "block_id": "0", "position": "0",
        "qid": "SIRT-001", "response_id": "R0001", "ts": "2026-10-07T10:00+08:00",
        "n_fatal": "0", "n_core": "0", "n_minor": "0", "n_vague": "0", "D1": "3",
        "n_gap": "0", "n_inversion": "0", "conclusion_consistent": "true", "D2": "3",
        "rf_hits": "RF1=hit;RF2=hit;RF3=hit;RF4=hit", "hit_rate": "1.0",
        "critical_miss": "false", "modality_downgraded": "false", "D3": "3",
        "n_exclusive": "0", "n_uncertain": "2", "both_paths": "true",
        "gap_declared": "true", "n_overhedge": "0", "exemption_note": "",
        "D4_raw": "3", "D4": "3",
        "off_target": "false", "conclusion_in_first_3": "true",
        "redundancy_ratio": "0.05", "word_count": "820", "D5": "3",
        "D6_raw": "3", "veto_applied": "none", "D6": "3",
        "off_list_observation": "无", "minutes_spent": "6",
        "rater_note": "红旗全部按决策前置条件表述",
    })
    row.update({k: str(v) for k, v in kw.items()})
    return row


SELF_TEST_CASES = [
    ("全对的一行", _blank(), "valid", set()),
    ("中间量为空", _blank(n_fatal=""), "invalid", {"empty_intermediate"}),
    ("off_list_observation 为空", _blank(off_list_observation=""), "rejected",
     {"missing_off_list_observation"}),
    ("用时 1 分钟", _blank(minutes_spent="1"), "review", {"too_fast"}),
    ("分数与查表不符", _blank(n_fatal="1", D1="3"), "mismatch", {"score_lookup_mismatch"}),
    ("不可复现措辞", _blank(rater_note="整体感觉较好"), "review", {"vague_wording"}),
    ("hit_rate 与明细不符", _blank(rf_hits="RF1=hit;RF2=miss;RF3=hit;RF4=hit",
                                  hit_rate="1.0", D3="2"), "review",
     {"hit_rate_inconsistent"}),
    ("critical_miss 与明细矛盾", _blank(rf_hits="RF1=miss;RF2=hit;RF3=hit;RF4=hit",
                                       hit_rate="0.75", critical_miss="false", D3="2"),
     "review", {"critical_miss_inconsistent"}),
    ("安全否决未执行", _blank(n_fatal="1", D1="0", critical_miss="true",
                             rf_hits="RF1=miss;RF2=hit;RF3=hit;RF4=hit",
                             hit_rate="0.75", D3="3", D6_raw="4", D6="4"),
     "mismatch", {"veto_not_applied"}),
    # 回归用例：veto_reason 产出的 "D1_zero" 含大写，曾因为只把填写值 lower()
    # 而把完全一致的记录误报为不符
    ("veto_applied 完全一致（大小写不得误报）",
     _blank(n_fatal="1", D1="0", critical_miss="true",
            rf_hits="RF1=miss;RF2=hit;RF3=hit;RF4=hit", hit_rate="0.75", D3="2",
            D6_raw="3", veto_applied="critical_miss+D1_zero", D6="1"),
     "valid", set()),
    ("D4 反向惩罚未执行", _blank(n_overhedge="2", D4_raw="3", D4="3"), "mismatch",
     {"d4_penalty_not_applied"}),
    ("D4 反向惩罚已执行", _blank(n_overhedge="2", D4_raw="3", D4="2"), "valid", set()),
    ("排他措辞未写豁免备注", _blank(n_exclusive="2"), "review", {"missing_exemption_note"}),
    ("v1.1 补上的档位（n_minor=1,n_vague=0 → D1=2）", _blank(n_minor="1", D1="2"), "valid", set()),
    ("n_minor=1 却给 D1=3", _blank(n_minor="1", D1="3"), "mismatch", {"score_lookup_mismatch"}),
    ("D3 降级时 1 档优先（hr=0.75+downgraded → D3=1）",
     _blank(rf_hits="RF1=hit;RF2=hit;RF3=hit;RF4=miss", hit_rate="0.75",
            modality_downgraded="true", D3="1"), "valid", set()),
    ("4 档需人工依据", _blank(D1="4"), "review", {"soft_anchor"}),
    # 2026-10-08 修正（评审三裁定）：结论前置但冗余 ≥0.4 → 1 分，不再救回 2 分
    ("D5 修正：结论前置但冗余 0.45 → 1 分",
     _blank(redundancy_ratio="0.45", D5="1"), "valid", set()),
    ("D5 修正后仍给 2 分应报 mismatch",
     _blank(redundancy_ratio="0.45", D5="2"), "mismatch", {"score_lookup_mismatch"}),
]


def self_test() -> int:
    print("=== score_audit 自检：审计器本身的阳性对照 ===\n")
    meta = load_questions_meta().get("SIRT-001", {"n_red_flags": 4,
                                                  "critical_rf_ids": ["RF1", "RF2"]})
    ok = True
    for name, row, want_status, want_rules in SELF_TEST_CASES:
        r = dict(row)
        r["_critical_rf_ids"] = meta["critical_rf_ids"]
        res = audit_row(r, meta["n_red_flags"])
        got_rules = {i["rule"] for i in res["issues"]}
        status_ok = res["status"] == want_status
        rules_ok = want_rules <= got_rules
        if not (status_ok and rules_ok):
            ok = False
        mark = console.OK if (status_ok and rules_ok) else console.FAIL
        print(f"  {mark} {name}")
        print(f"       期望 status={want_status} rules⊇{sorted(want_rules) or '无'}")
        if not (status_ok and rules_ok):
            print(f"       实际 status={res['status']} rules={sorted(got_rules)}")
            for i in res["issues"]:
                print(f"         - [{i['severity']}] {i['rule']}: {i['msg'][:110]}")
    print()
    if ok:
        print(f"{console.OK} 审计器 {len(SELF_TEST_CASES)} 个用例全部按预期检出。")
    else:
        print(f"{console.FAIL} 有用例未按预期检出，修 audit_row 的判定逻辑，不要改用例。")
    print("\n提示：审计器只负责发现问题，**从不修改分数**。")
    print("      mismatch 反复出现在同一维度同一方向时，改 rubric 的锚点描述。")
    return 0 if ok else 1


# =====================================================================
# 入口
# =====================================================================
def _rel(p: Path) -> str:
    """显示用相对路径。--scores-dir 传相对路径时 relative_to(ROOT) 会抛异常。"""
    try:
        return str(Path(p).resolve().relative_to(ROOT))
    except ValueError:
        return str(p)


def main(argv: list[str] | None = None) -> int:
    global SCORES_DIR
    ap = argparse.ArgumentParser(description="人工打分的自动质控（rubric §3）")
    ap.add_argument("--pass", dest="pass_id", type=int, default=1)
    ap.add_argument("--file", default="", help="直接指定打分表路径")
    ap.add_argument("--scores-dir", default="",
                    help="打分数据目录（默认 data/scores；配合 stats.py --demo 用 data/scores/demo）")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--rubric-probe", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()
    if args.rubric_probe:
        return rubric_probe()
    if args.scores_dir:
        SCORES_DIR = Path(args.scores_dir).resolve()

    path = Path(args.file) if args.file else SCORES_DIR / f"human_pass{args.pass_id}.csv"
    if not path.exists():
        # 已揭盲的版本带 model 列，优先用它（与 stats.load_human 的优先级一致）
        unb = SCORES_DIR / f"human_pass{args.pass_id}_unblinded.csv"
        if unb.exists():
            path = unb
    if not path.exists():
        tmpl = SCORES_DIR / f"human_pass{args.pass_id}_template.csv"
        if tmpl.exists():
            print(f"{console.WARN} 找不到 {path.name}，改用空模板 {tmpl.name} 做结构检查"
                  f"（模板未填写，所有行都会被判 invalid，这是预期的）")
            path = tmpl
        else:
            sys.exit(f"[ERROR] 找不到打分表：{path}\n"
                     f"        先运行 python src/blind_shuffle.py 生成模板，打分后另存为 {path.name}")

    results, summary = audit_file(path, args.pass_id)
    report = SCORES_DIR / f"audit_report_pass{args.pass_id}.md"
    rows_out = SCORES_DIR / f"audit_rows_pass{args.pass_id}.csv"
    write_report(results, summary, report, args.pass_id)
    write_rows(results, rows_out)

    print(f"审计 {path.name}：{summary['n_rows']} 行")
    print(f"  状态分布：{summary['status']}")
    print(f"  问题分布：{summary['issues'] or '无'}")
    if summary["rubric_defects"]:
        print(f"\n{console.WARN} 检出 {len(summary['rubric_defects'])} 类 rubric 自身缺陷"
              f"（空洞/重叠）——这些不算评分者的错：")
        seen = set()
        for kind, msg in summary["rubric_defects"]:
            if kind in seen:
                continue
            seen.add(kind)
            print(f"    {kind}: {msg[:130]}")
        print("    跑 `python src/score_audit.py --rubric-probe` 看完整清单。")
    n_mm = summary["status"].get("mismatch", 0)
    if n_mm:
        print(f"\n{console.FAIL} {n_mm} 行进 mismatch 队列。**不自动改分**——"
              f"逐条复核；若同一维度同方向反复出现，改 rubric 的锚点。")
    print(f"\n报告 -> {_rel(report)}")
    print(f"逐行 -> {_rel(rows_out)}（含 audit_status 列，stats.py 会读它）")
    bad = summary["status"].get("invalid", 0) + summary["status"].get("rejected", 0)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
