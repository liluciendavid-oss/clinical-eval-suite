#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
schema.py — questions.jsonl 字段校验器

设计目的
--------
题库是整套评测的地基。地基脏了，后面所有统计都是在给噪声做包装。
本脚本在「载入即校验」，任何字段缺失、枚举越界、ID 重复、锚点无法复现
都会在写题的当天被拦住，而不是等到阶段 3 跑统计时才炸。

三条硬性设计约束（对应 docs/rubric.md §5 可复现性自检表）：
  1. required_chain 必填且有序 —— 否则 D2 的 n_gap 无法被两人独立复现。
  2. acceptable_divergence 必填且非套话 —— 否则 D4 没有客观判据。
  3. 正文里残留 VERIFY 标记 → 默认报错。
     未核实的临床数值不允许混进"标准答案"，这是本项目的诚信底线。

用法
----
    python src/schema.py                       # 校验默认路径
    python src/schema.py --strict              # WARN 也算失败
    python src/schema.py --allow-verify        # 允许 VERIFY 残留（仅开发期）
    python src/schema.py --stats               # 额外打印题库配额统计

零第三方依赖：只用标准库。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

try:
    import console
except ImportError:  # 允许从仓库根目录以 -m 方式运行
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import console

console.setup()

SCHEMA_VERSION = "1.1"   # 1.0 -> 1.1：red_flags 增加 hint_polarity（见 error_taxonomy.md §1.1）

# ---------------------------------------------------------------- 枚举定义
DOMAINS = {"SIRT", "TACE", "HAIC", "combo", "complication"}

TASK_TYPES = {
    "indication_screen",
    "dose_reasoning",
    "counterfactual_compare",
    "complication_mgmt",
    "evidence_appraisal",
    "workup_gap",
}

CAPABILITY_TAGS = {
    "staging_indication",
    "contraindication_detection",
    "dosimetry_reasoning",
    "counterfactual_reasoning",
    "evidence_appraisal",
    "complication_management",
    "uncertainty_expression",
    "multimodal_context_use",
    "instruction_following",
}

SEVERITIES = {"critical", "high", "moderate"}

# detection_hint 的极性。同一个字段名承载了两种**相反**的语义，不显式标注就会
# 让规则层的 E2a 判定方向反转（2026-10-07 实测发现，见 docs/deviation_log.md）：
#   mention = hints 是必须提及/必须处理的实体或动作；全部缺席 ⇒ 确定漏掉该红旗
#   forbid  = hints 全是"说出来就算犯错"的断言短语；缺席才是正确，出现才可疑
#   mixed   = 两类混杂或判定不确定 ⇒ 规则层只 flag，永不 confirmed
HINT_POLARITIES = {"mention", "forbid", "mixed"}

CONTAMINATION_LEVELS = {"low", "medium", "high"}

# red_flag 应以何种强度被表述；D3 的 modality_downgraded 依赖此字段
EXPECTED_MODALITIES = {"决策前置条件", "风险提示", "计算过程", "随访要求"}

DIMENSIONS = {"D1", "D2", "D3", "D4", "D5", "D6"}

QID_RE = re.compile(r"^[A-Z]+-\d{3}$")

# 必填的顶层字段
REQUIRED_TOP_LEVEL = [
    "qid", "domain", "task_type", "capability_tags", "difficulty",
    "contamination_risk", "stem", "vignette", "question", "key_points",
    "red_flags", "acceptable_divergence", "required_chain",
    "unacceptable", "reference_basis", "author_note",
]

OPTIONAL_TOP_LEVEL = ["judge_focus", "format_probe", "notes"]

# 套话检测：acceptable_divergence 若只是这些话，等于没写
PLATITUDES = [
    "视情况而定", "个体化", "需结合临床", "因人而异", "具体情况具体分析",
]


# ---------------------------------------------------------------- 工具函数
class Report:
    """收集 ERROR / WARN，最后统一输出。"""

    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def err(self, where: str, msg: str) -> None:
        self.errors.append(f"[ERROR] {where}: {msg}")

    def warn(self, where: str, msg: str) -> None:
        self.warnings.append(f"[WARN ] {where}: {msg}")


def _is_nonempty_str(x: Any) -> bool:
    return isinstance(x, str) and x.strip() != ""


def _check_str(rec: dict, field: str, where: str, rep: Report, min_len: int = 1) -> None:
    v = rec.get(field)
    if not _is_nonempty_str(v):
        rep.err(where, f"字段 `{field}` 缺失或为空字符串")
    elif len(v.strip()) < min_len:
        rep.err(where, f"字段 `{field}` 长度 {len(v.strip())} < {min_len}，疑似占位符")


def _check_verify_markers(rec: dict, where: str, rep: Report, allow: bool) -> None:
    """扫描全字段，检出未核实的临床数值标记。"""
    blob = json.dumps(rec, ensure_ascii=False)
    hits = re.findall(r"VERIFY(?::[^\s\"，。;；]{0,40})?", blob)
    if hits and not allow:
        rep.err(
            where,
            f"残留 {len(hits)} 处未核实标记 {sorted(set(hits))[:3]}……"
            f" 未核实数值不得进入题库；确已核实请删除标记并补 reference_basis 原文引句",
        )
    elif hits:
        rep.warn(where, f"残留 {len(hits)} 处 VERIFY 标记（--allow-verify 已放行，交付前必须清零）")


# ---------------------------------------------------------------- 子结构校验
def _check_key_points(q: dict, where: str, rep: Report) -> None:
    kps = q.get("key_points")
    if not isinstance(kps, list) or not kps:
        rep.err(where, "`key_points` 必须是非空数组")
        return

    seen: set[str] = set()
    total_weight = 0
    n_must = 0
    for i, kp in enumerate(kps):
        w = f"{where}.key_points[{i}]"
        if not isinstance(kp, dict):
            rep.err(w, "必须是对象")
            continue
        kpid = kp.get("kp_id")
        if not _is_nonempty_str(kpid):
            rep.err(w, "`kp_id` 缺失")
        elif kpid in seen:
            rep.err(w, f"`kp_id` 重复：{kpid}")
        else:
            seen.add(kpid)

        wt = kp.get("weight")
        if not isinstance(wt, int) or isinstance(wt, bool) or not (1 <= wt <= 3):
            rep.err(w, f"`weight` 必须是 1–3 的整数，得到 {wt!r}")
        else:
            total_weight += wt

        if not isinstance(kp.get("must_have"), bool):
            rep.err(w, f"`must_have` 必须是布尔值，得到 {kp.get('must_have')!r}")
        elif kp["must_have"]:
            n_must += 1

        txt = kp.get("text")
        if not _is_nonempty_str(txt):
            rep.err(w, "`text` 缺失或为空")
        elif len(txt.strip()) < 15:
            rep.err(w, f"`text` 过短（{len(txt.strip())} 字），无法做二值命中判定，请写完整原子陈述")
        elif "或" in txt and "。" not in txt and len(txt) > 120:
            # 原子性提示：一条 key point 只应被"命中/未命中"二值判定
            rep.warn(w, "`text` 含多个分支且较长，可能违反原子性要求 → 建议拆成两条")

    if len(kps) < 4:
        rep.warn(where, f"key_points 仅 {len(kps)} 条，覆盖度可能不足以支撑 D1/D3 的区分度（建议 ≥5）")
    if n_must == 0:
        rep.warn(where, "没有任何 must_have=true 的 key point → D4 的 n_overhedge 将无法计算")


def _check_red_flags(q: dict, where: str, rep: Report) -> None:
    rfs = q.get("red_flags")
    if not isinstance(rfs, list) or not rfs:
        rep.err(where, "`red_flags` 必须是非空数组")
        return

    seen: set[str] = set()
    sev_counter: Counter[str] = Counter()
    for i, rf in enumerate(rfs):
        w = f"{where}.red_flags[{i}]"
        if not isinstance(rf, dict):
            rep.err(w, "必须是对象")
            continue
        rfid = rf.get("rf_id")
        if not _is_nonempty_str(rfid):
            rep.err(w, "`rf_id` 缺失")
        elif rfid in seen:
            rep.err(w, f"`rf_id` 重复：{rfid}")
        else:
            seen.add(rfid)

        sev = rf.get("severity")
        if sev not in SEVERITIES:
            rep.err(w, f"`severity` 必须是 {sorted(SEVERITIES)} 之一，得到 {sev!r}")
        else:
            sev_counter[sev] += 1

        if not _is_nonempty_str(rf.get("text")):
            rep.err(w, "`text` 缺失或为空")

        mod = rf.get("expected_modality")
        if mod not in EXPECTED_MODALITIES:
            rep.err(w, f"`expected_modality` 必须是 {sorted(EXPECTED_MODALITIES)} 之一，得到 {mod!r}")

        hints = rf.get("detection_hint")
        if not isinstance(hints, list) or not hints:
            rep.err(w, "`detection_hint` 必须是非空数组（规则层靠它做关键词匹配）")
        elif not all(_is_nonempty_str(h) for h in hints):
            rep.err(w, "`detection_hint` 含空元素或非字符串")
        elif len(hints) < 2:
            rep.warn(w, "`detection_hint` 只有 1 个词，规则层召回会很低 → 建议补同义词/英文术语")

        # 极性必须显式标注：缺省会静默改变 E2a 的判定方向，不允许靠默认值蒙过去
        if "hint_polarity" not in rf:
            rep.warn(w, "`hint_polarity` 未显式标注（规则层将按 mention 处理）→ "
                        "该红旗若是「说出即犯错」型，E2a 会反向误报")
        elif rf["hint_polarity"] not in HINT_POLARITIES:
            rep.err(w, f"`hint_polarity` 必须是 {sorted(HINT_POLARITIES)} 之一，"
                       f"得到 {rf['hint_polarity']!r}")

    if sev_counter["critical"] == 0:
        rep.warn(where, "本题没有 critical 级红旗 → 安全否决门控对本题永不触发，考虑是否漏标")


def _check_acceptable_divergence(q: dict, where: str, rep: Report) -> None:
    ad = q.get("acceptable_divergence")
    if not _is_nonempty_str(ad):
        rep.err(where, "`acceptable_divergence` 缺失 → D4 的 both_paths 无客观判据")
        return
    s = ad.strip()
    if len(s) < 30:
        rep.err(where, f"`acceptable_divergence` 仅 {len(s)} 字，不足以界定分歧范围（要求 ≥30 字）")
    if any(p in s for p in PLATITUDES) and len(s) < 60:
        rep.err(where, f"`acceptable_divergence` 是套话（含「{'、'.join(PLATITUDES)}」之类）而未写明哪些具体路径都算对 → 不可复现，必须重写")


def _check_required_chain(q: dict, where: str, rep: Report) -> None:
    rc = q.get("required_chain")
    if not isinstance(rc, list) or not rc:
        rep.err(where, "`required_chain` 缺失 → D2 的 n_gap 无法被两人独立复现")
        return
    if not all(_is_nonempty_str(s) for s in rc):
        rep.err(where, "`required_chain` 含空元素或非字符串")
        return
    if len(rc) < 3:
        rep.err(where, f"`required_chain` 仅 {len(rc)} 步，过短（要求 3–6 步有序推理链）")
    if len(rc) > 8:
        rep.warn(where, f"`required_chain` 有 {len(rc)} 步，过长会让 n_gap 对篇幅敏感 → 建议压到 6 步内")
    if len(set(rc)) != len(rc):
        rep.err(where, "`required_chain` 存在重复步骤")


def _check_vignette(q: dict, where: str, rep: Report) -> None:
    v = q.get("vignette")
    if not isinstance(v, dict) or not v:
        rep.err(where, "`vignette` 必须是非空对象（结构化病例要素，规则层靠它校验模型是否用到了给定数值）")
        return
    core = {"age_sex", "diagnosis_and_stage", "liver_function"}
    missing = core - set(v)
    if missing:
        rep.err(where, f"`vignette` 缺少核心要素 {sorted(missing)}")
    # 合成病例声明：不允许出现可识别信息
    blob = json.dumps(v, ensure_ascii=False)
    for pat, why in [
        (r"\b\d{17}[\dXx]\b", "疑似身份证号"),
        (r"(住院号|门诊号|病案号)", "含病历号字段"),
        (r"\b19\d{2}[-/年]\d{1,2}[-/月]\d{1,2}", "含精确出生日期"),
    ]:
        if re.search(pat, blob):
            rep.err(where, f"`vignette` {why} → 违反「不使用任何真实患者数据」硬约束")


# ---------------------------------------------------------------- 单题校验
def check_question(q: dict, rep: Report, allow_verify: bool = False) -> bool:
    if not isinstance(q, dict):
        rep.err("<line>", "不是 JSON 对象")
        return False

    qid = q.get("qid")
    where = qid if _is_nonempty_str(qid) else "<no-qid>"

    if not _is_nonempty_str(qid):
        rep.err(where, "`qid` 缺失")
    elif not QID_RE.match(qid):
        rep.err(where, f"`qid` 格式应为 DOMAIN-NNN（如 SIRT-007），得到 {qid!r}")

    for f in REQUIRED_TOP_LEVEL:
        if f not in q:
            rep.err(where, f"缺少必填字段 `{f}`")

    unknown = set(q) - set(REQUIRED_TOP_LEVEL) - set(OPTIONAL_TOP_LEVEL)
    if unknown:
        rep.warn(where, f"存在未定义字段 {sorted(unknown)}（拼写错误？schema v{SCHEMA_VERSION}）")

    if q.get("domain") not in DOMAINS:
        rep.err(where, f"`domain` 必须是 {sorted(DOMAINS)} 之一，得到 {q.get('domain')!r}")
    if q.get("task_type") not in TASK_TYPES:
        rep.err(where, f"`task_type` 必须是 {sorted(TASK_TYPES)} 之一，得到 {q.get('task_type')!r}")
    if q.get("contamination_risk") not in CONTAMINATION_LEVELS:
        rep.err(where, f"`contamination_risk` 必须是 {sorted(CONTAMINATION_LEVELS)} 之一")

    d = q.get("difficulty")
    if not isinstance(d, int) or isinstance(d, bool) or not (1 <= d <= 4):
        rep.err(where, f"`difficulty` 必须是 1–4 的整数，得到 {d!r}")

    tags = q.get("capability_tags")
    if not isinstance(tags, list) or not tags:
        rep.err(where, "`capability_tags` 必须是非空数组")
    else:
        bad = set(tags) - CAPABILITY_TAGS
        if bad:
            rep.err(where, f"`capability_tags` 含未定义标签 {sorted(bad)}")
        if not (2 <= len(tags) <= 5):
            rep.warn(where, f"`capability_tags` 有 {len(tags)} 个（建议 2–5）")
        if len(set(tags)) != len(tags):
            rep.err(where, "`capability_tags` 有重复")

    for f, n in (("stem", 60), ("question", 15)):
        _check_str(q, f, where, rep, min_len=n)

    if _is_nonempty_str(q.get("question")):
        qs_ = q["question"]
        # 疑问句或祈使式提问都算真提问；只有纯陈述（无任何提问标记）才告警
        is_question = ("?" in qs_ or "？" in qs_ or qs_.lstrip().startswith(("请", "是否", "如何",
                       "什么", "哪", "若", "该")) or any(k in qs_ for k in ("判断", "给出", "说明", "计算", "比较")))
        if not is_question:
            rep.warn(where, "`question` 不含任何提问标记（？/请/是否/如何/判断/给出…）→ 疑似把背景陈述误粘成了提问")

    ub = q.get("unacceptable")
    if not isinstance(ub, list) or not ub:
        rep.err(where, "`unacceptable` 必须是非空数组（安全否决门控依赖它）")
    elif not all(_is_nonempty_str(x) for x in ub):
        rep.err(where, "`unacceptable` 含空元素")

    rb = q.get("reference_basis")
    if not isinstance(rb, list) or not rb:
        rep.err(where, "`reference_basis` 必须是非空数组（每条答案要点都要有公开来源）")

    jf = q.get("judge_focus")
    if jf is not None:
        if not isinstance(jf, list) or (set(jf) - DIMENSIONS):
            rep.err(where, f"`judge_focus` 必须是 {sorted(DIMENSIONS)} 的子集")

    if not isinstance(q.get("format_probe", False), bool):
        rep.err(where, "`format_probe` 必须是布尔值")

    _check_vignette(q, where, rep)
    _check_key_points(q, where, rep)
    _check_red_flags(q, where, rep)
    _check_acceptable_divergence(q, where, rep)
    _check_required_chain(q, where, rep)
    _check_verify_markers(q, where, rep, allow_verify)

    return True


# ---------------------------------------------------------------- 题库级校验
def check_bank(questions: list[dict], rep: Report) -> None:
    """跨题约束：配额、覆盖度、ID 唯一性。"""
    qids = [q.get("qid") for q in questions if _is_nonempty_str(q.get("qid"))]
    dup = [k for k, c in Counter(qids).items() if c > 1]
    if dup:
        rep.err("<bank>", f"qid 重复：{dup}")

    n = len(questions)
    if n and n < 20:
        rep.warn("<bank>", f"题库仅 {n} 题（目标 20）")

    contam = Counter(q.get("contamination_risk") for q in questions)
    if contam["high"] > 5:
        rep.warn("<bank>", f"contamination_risk=high 有 {contam['high']} 题（上限 5）→ 记忆型题过多会高估模型")

    # 维度覆盖：每个维度至少要有一道题聚焦，否则该维度的模型间差异缺乏区分力
    focused: Counter[str] = Counter()
    for q in questions:
        for d in q.get("judge_focus") or []:
            focused[d] += 1
    for dim in ("D1", "D2", "D3", "D4", "D5"):
        if n >= 8 and focused[dim] == 0:
            rep.warn("<bank>", f"没有任何题的 judge_focus 指向 {dim} → 该维度可能缺乏区分度")

    # 能力标签覆盖
    used: Counter[str] = Counter()
    for q in questions:
        for t in q.get("capability_tags") or []:
            used[t] += 1
    if n >= 8:
        for t in sorted(CAPABILITY_TAGS - set(used)):
            rep.warn("<bank>", f"能力标签 `{t}` 未被任何题覆盖 → 热力图该列会全空")

    # 领域配额
    dom = Counter(q.get("domain") for q in questions)
    if n >= 20 and dom["SIRT"] + dom["combo"] < 8:
        rep.warn("<bank>", f"SIRT/combo 仅 {dom['SIRT'] + dom['combo']} 题，与「纯肝胆介入、SIRT 为差异化主场」的定位不符")


def print_stats(questions: list[dict]) -> None:
    n = len(questions)
    print("\n=== 题库统计 ===")
    print(f"题数：{n}")
    for name, key in (("领域", "domain"), ("题型", "task_type"),
                      ("难度", "difficulty"), ("污染风险", "contamination_risk")):
        c = Counter(q.get(key) for q in questions)
        items = sorted(c.items(), key=lambda kv: str(kv[0]))
        print(f"{name}：" + "，".join(f"{k}={v}" for k, v in items))
    tags = Counter(t for q in questions for t in q.get("capability_tags") or [])
    print("能力标签：" + "，".join(f"{k}={v}" for k, v in tags.most_common()))
    kp = [len(q.get("key_points") or []) for q in questions]
    rf = [len(q.get("red_flags") or []) for q in questions]
    crit = sum(1 for q in questions for r in (q.get("red_flags") or []) if r.get("severity") == "critical")
    if kp:
        print(f"key_points：合计 {sum(kp)}，每题 {min(kp)}–{max(kp)}（均值 {sum(kp)/n:.1f}）")
    if rf:
        print(f"red_flags：合计 {sum(rf)}，其中 critical {crit} 条；每题 {min(rf)}–{max(rf)}")
    pol = Counter(r.get("hint_polarity", "<未标注>")
                  for q in questions for r in (q.get("red_flags") or []))
    print("hint_polarity：" + "，".join(f"{k}={v}" for k, v in pol.most_common())
          + "　（决定 E2a/E2b/E2d 的判定方向，见 error_taxonomy.md §1.1）")
    probe = sum(1 for q in questions if q.get("format_probe"))
    print(f"格式敏感性分析子集：{probe} 题（计划 6）")


# ---------------------------------------------------------------- 入口
def load_questions(path: Path, rep: Report) -> list[dict]:
    if not path.exists():
        rep.err("<file>", f"题库文件不存在：{path}")
        return []
    out: list[dict] = []
    for ln, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as e:
            rep.err(f"line {ln}", f"JSON 解析失败：{e}")
    return out


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser(description="校验 questions.jsonl")
    ap.add_argument("--questions", default=str(root / "data" / "questions.jsonl"))
    ap.add_argument("--strict", action="store_true", help="WARN 也视为失败")
    ap.add_argument("--allow-verify", action="store_true", help="放行 VERIFY 残留（仅开发期）")
    ap.add_argument("--stats", action="store_true", help="打印题库配额统计")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    rep = Report()
    questions = load_questions(Path(args.questions), rep)
    for q in questions:
        check_question(q, rep, allow_verify=args.allow_verify)
    check_bank(questions, rep)

    if args.stats and questions:
        print_stats(questions)

    # 统一走 stdout 并按固定顺序输出，避免与 stderr 交错导致日志乱序
    lines = [f"schema v{SCHEMA_VERSION} | {len(questions)} 题 | "
             f"ERROR {len(rep.errors)} | WARN {len(rep.warnings)}"]
    if rep.errors or rep.warnings:
        lines.append("")
        lines.extend(rep.errors)
        if rep.warnings:
            lines.extend(rep.warnings)
    if not args.quiet:
        print("\n".join(lines))

    failed = bool(rep.errors) or (args.strict and bool(rep.warnings))
    if failed:
        if args.quiet:
            print("\n".join(lines))
        sys.stdout.flush()
        print("校验失败。", file=sys.stderr)
        return 1

    if not args.quiet:
        print(f"\n{console.OK} 校验通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
