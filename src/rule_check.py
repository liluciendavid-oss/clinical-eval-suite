#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rule_check.py — 规则层检测器（error_taxonomy.md §1 的三层分工中精度最高的一层）

它做什么
--------
对每份回答跑 16 个检测器，产出两类东西：

  1. `rule_findings.jsonl`  错误实例，格式见 error_taxonomy.md §3
     每条带 `verdict`：`confirmed`（可直接采信）或 `flag_for_review`（必须人工裁定）
  2. `rule_metrics.jsonl`   每份回答的可数中间量（红旗命中率、加权要点覆盖率、
     排他措辞计数、冗余占比、结论前置……）

第 2 类同样重要：rubric §0 总则 1 要求「先产出可数的中间量，再查表映射分数」。
规则层算出的中间量与人工填的中间量可以互相核对——`score_audit.py` 会把两者的
分歧列出来，那是发现 rubric 锚点歧义的最快通道。

为什么 confirmed 的门槛这么高
----------------------------
只有假阳性率在直觉上接近 0 的检测才允许 confirmed（error_taxonomy.md §0 原则 1）。
本文件的每个 confirmed 分支都配了一个守卫，守卫的实测依据记录在
`dictionaries.py` 顶部与 error_taxonomy.md §2.8.1。核心事实是：

  * 朴素版 E5 会在 **14/20 份零缺陷回答**上误报（「绝对禁忌」被当成「绝对」）
  * 朴素版 E7 会在 **7/20 份零缺陷回答**上误报（剂量计算题的提问本身不含术式名）

所以 `--self-test` 同时跑阳性对照（mock_defective，注入 59 个已知缺陷）与
阴性对照（mock_clean，零缺陷）。**阴性对照上 confirmed 必须为 0**，否则规则层
不合格——一个只会多报的检测器在只看 recall 时是隐形的。

用法
----
    python src/rule_check.py --self-test          # 阳性+阴性对照，给出 precision/recall
    python src/rule_check.py --source demo        # 在 DEMO 数据上跑通（结果无意义）
    python src/rule_check.py                      # 真实回答（到位后）
    python src/rule_check.py --only SIRT-007      # 只看某几题
    python src/rule_check.py --show SIRT-010      # 打印某题某模型的全部 finding

依赖：仅标准库。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import console  # noqa: E402
import dictionaries as D  # noqa: E402

console.setup()

ROOT = Path(__file__).resolve().parent.parent
QUESTIONS = ROOT / "data" / "questions.jsonl"
RAW_DIR = ROOT / "data" / "responses" / "raw"
RESPONSES = RAW_DIR / "responses.jsonl"
DEMO_RESPONSES = RAW_DIR / "demo_responses.jsonl"
MOCK_RESPONSES = RAW_DIR / "mock_responses.jsonl"
MOCK_TRUTH = ROOT / "data" / "responses" / "mock_ground_truth.jsonl"
KEY_FILE = ROOT / "data" / "responses" / "key.jsonl"
SCORES_DIR = ROOT / "data" / "scores"
# 规则层产物**含明文模型名**，盲测期间评分者看到即等于破盲，因此与人工打分表
# 分目录存放：data/scores/ 只放人工数据，data/scores/rule/ 是揭盲后才可看的区域。
RULE_DIR = SCORES_DIR / "rule"
MOCK_SCORES_DIR = SCORES_DIR / "mock"

RULE_VERSION = "1.0"

# error_taxonomy.md §1 总表的「映射维度」列——代码与文档必须一致
CODE_DIMENSION: dict[str, list[str]] = {
    "E1a": ["D1"], "E1b": ["D1"], "E1c": ["D1"],
    "E2a": ["D3"], "E2b": ["D3"], "E2c": ["D3"], "E2d": ["D3", "D6"],
    "E3": ["D1"], "E4a": ["D2"], "E4b": ["D2"],
    "E5": ["D4"], "E6": ["D4"], "E7": ["D5"], "E8": ["D5"],
    "E9": ["D1"], "E10": ["D2"], "E11": ["D5"], "E12": ["D1", "D2"],
}

# 文档 §1 规定的检测层。规则层不得输出超出此表所允许的最高 verdict。
CODE_MAX_VERDICT: dict[str, str] = {
    "E1a": "flag_for_review",   # 规则 flag → 人工确认
    "E1b": "confirmed",
    "E1c": "flag_for_review",   # 一律 flag
    "E2a": "confirmed",
    "E2b": "confirmed",
    "E2c": "judge_only",        # LLM-judge 层，规则层不产出
    "E2d": "flag_for_review",   # 否定可以出现在任意远处，正则无法可靠确认
    "E3": "flag_for_review",    # 永远只能是 flag
    "E4a": "judge_only",
    "E4b": "flag_for_review",   # 规则辅助 + LLM-judge
    "E5": "confirmed",
    "E6": "flag_for_review",    # 规则 flag → 人工确认
    "E7": "confirmed",
    "E8": "confirmed",
    "E9": "confirmed",
    "E10": "flag_for_review",   # 规则只管显式否定词冲突，语义级归 judge
    "E11": "confirmed",
    "E12": "confirmed",
}

CONFIRMED = "confirmed"
FLAG = "flag_for_review"

# 归码等价表。一个数值既「落在公认区间之外」(E1a) 又「超出 §2.5 物理可能区间」(E9)
# 时，按 §0 原则 2「就高不就低」归 E9——数量级荒谬是更根本的失效。
# 注入器把它标成 E1a，所以自检必须承认这一等价，但**分开报**严格与等价两套 recall：
# 悄悄放宽验收标准，等于把阳性对照变成装饰品。
EQUIVALENT_CODES: dict[str, set[str]] = {
    "E1a": {"E1a", "E9"},
    "E9": {"E9", "E1a"},
}

# 漏报型错误码：finding 的 char_span 恒为 [0,0]，注入的证据文本已被删除，
# 因此"证据级 recall"在结构上不可能成立。对它们计算该指标只会产出误导性的 0%。
OMISSION_CODES: set[str] = {"E2a", "E2b", "E4b"}


# =====================================================================
# 通用工具
# =====================================================================
def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="minutes")


_SENT_SPLIT = re.compile(r"(?<=[。！？；!?;])")


def sentences(text: str) -> list[str]:
    """按中文句末标点切句。保留分隔符，便于回算字符位置。"""
    return [s for s in _SENT_SPLIT.split(text) if s.strip()]


def lead_window(text: str, n: int | None = None) -> str:
    """回答的开头窗口。E7 用它判「结论是否前置」。"""
    return re.sub(r"[*#>\-\s]+", " ", text[: n or D.LEAD_WINDOW_CHARS])


def bigrams(s: str) -> set[str]:
    s = re.sub(r"\s+", "", s)
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else {s} if s else set()


def coverage(needle: str, hay_bigrams: set[str]) -> float:
    """needle 的字符二元组有多大比例出现在 haystack 里。"""
    nb = bigrams(needle)
    if not nb:
        return 0.0
    return len(nb & hay_bigrams) / len(nb)


def window(text: str, start: int, end: int, pad: int = 40) -> str:
    return text[max(0, start - pad): min(len(text), end + pad)]


def snippet(text: str, start: int, end: int, width: int = 90) -> str:
    """取证据片段：以命中处为中心，截到句子边界附近，避免证据里塞进整段。"""
    lo = max(0, start - width // 3)
    hi = min(len(text), end + width)
    return text[lo:hi].strip().replace("\n", " ")


def negated(span: str) -> bool:
    """命中片段内含否定守卫 → 原文是在否定该错误断言，属正确表述。"""
    return any(g in span for g in D.NEGATION_GUARD)


class Finding(dict):
    """error_taxonomy.md §3 的错误实例格式。

    `span_kind` 是本实现补的字段：漏报型错误（E2a/E2b/E4b）在回答里**没有**
    对应文本，char_span 无意义，只能填 [0,0]。若不显式标出，下游会把
    [0,0] 当成"位于开头的证据"，这是会静默传播的脏数据。
    """


def mk(code: str, verdict: str, evidence: str, span: tuple[int, int],
       qid: str, model: str, rule: str, note: str = "",
       span_kind: str = "match", extra: dict | None = None) -> Finding:
    allowed = CODE_MAX_VERDICT.get(code, FLAG)
    if allowed == "judge_only":
        raise ValueError(f"{code} 属 LLM-judge 层，规则层不得输出")
    if verdict == CONFIRMED and allowed != CONFIRMED:
        # 硬约束：文档规定只能 flag 的错误码，代码不允许升级为 confirmed
        verdict = FLAG
    f = Finding({
        "qid": qid,
        "response_id": "",
        "model": model,
        "code": code,
        "layer": "rule",
        "verdict": verdict,
        "evidence": evidence,
        "char_span": [int(span[0]), int(span[1])],
        "span_kind": span_kind,
        "dimension_impact": CODE_DIMENSION.get(code, []),
        "rule": rule,
        "rater_note": note,
        "rule_version": RULE_VERSION,
        "ts": now_iso(),
    })
    if extra:
        f.update(extra)
    return f


# =====================================================================
# 检测器：D3 红旗（E2a / E2b）
# =====================================================================
def _framed_as_prohibition(text: str, pos: int) -> bool:
    """命中处是否处在「列举出来以便否定」的框架内。

    两类框架都算：所在行自带禁止词，或上方最近的小节标题/引导句是禁止性的
    （如「**必须优先排除的安全项**：」下面列出的每一条都是要排除的错误）。
    """
    line_start = text.rfind("\n", 0, pos) + 1
    line_end = text.find("\n", pos)
    line = text[line_start: line_end if line_end > 0 else len(text)]
    if any(f in line for f in D.PROHIBITION_FRAMING):
        return True
    head = text[max(0, line_start - 1500):line_start]
    for ln in reversed(head.splitlines()):
        s = ln.strip()
        if not s:
            continue
        if s.startswith(("#", "**", "【")) or s.endswith(("：", ":")):
            return any(f in s for f in D.PROHIBITION_FRAMING)
    return False


def _negated_near(text: str, pos: int, reach: int = 4) -> bool:
    pre = text[max(0, pos - reach):pos]
    return any(n in pre for n in D.NEGATION_PREFIX)


def det_red_flags(text: str, q: dict, out: list[Finding]) -> dict:
    """按 `hint_polarity` 分流判定，见 error_taxonomy.md §1.1。

    规则层只看 detection_hint 是否出现，**不判** hit/partial：partial 的定义是
    「提到实体但未作出正确处置结论」，那是语义判断，属人工/judge。所以
    `rf_hint_rate` 是人工 `hit_rate` 的**上界**——hint 出现不等于正确处置。
    这个区别必须在报告里写明，否则规则层数字会被当成人工数字用。
    """
    rfs = q.get("red_flags") or []
    low = text.lower()
    hits: dict[str, bool] = {}
    crit_miss: list[str] = []
    violations: list[str] = []
    for rf in rfs:
        rfid = rf.get("rf_id", "?")
        sev = rf.get("severity")
        pol = rf.get("hint_polarity", "mention")
        hints = [h for h in (rf.get("detection_hint") or []) if h]
        found = [(h, low.find(h.lower())) for h in hints if h.lower() in low]
        hits[rfid] = bool(found)
        omit_code = "E2a" if sev == "critical" else "E2b"

        if pol == "forbid":
            # hint 缺席 = 正确，不产 omission finding；出现且未被否定 → E2d flag
            for h, pos in found:
                if pos < 0 or _negated_near(text, pos) or _framed_as_prohibition(text, pos):
                    continue
                violations.append(rfid)
                out.append(mk(
                    "E2d", FLAG,
                    f"红旗 {rfid}（{sev}，forbid 型）被违反：出现未被否定的断言「{h}」。"
                    f"红旗原文：{rf.get('text', '')[:70]}　上下文：…{snippet(text, pos, pos + len(h))}…",
                    (pos, pos + len(h)), q["qid"], "", f"red_flag_violated/{rfid}",
                    note="否定可能出现在任意远处，正则无法可靠确认 → 必须人工裁定；"
                         "若同时命中 unacceptable 清单，将触发 rubric §0 的 D6=0 否决",
                    extra={"rf_id": rfid, "severity": sev, "matched_hint": h},
                ))
            continue

        if found:
            continue

        # hint 全部缺席
        if sev == "critical":
            crit_miss.append(rfid)
        verdict = CONFIRMED if pol == "mention" else FLAG
        out.append(mk(
            omit_code, verdict,
            f"红旗 {rfid}（severity={sev}, polarity={pol}）未被提及："
            f"detection_hint={hints} 全部未在回答中出现。红旗原文：{rf.get('text', '')[:60]}",
            (0, 0), q["qid"], "", f"red_flags/{rfid}",
            note="漏报型错误，回答中无对应文本，故 char_span=[0,0]"
                 + ("" if pol == "mention" else "；mixed 极性的 hint 表混杂禁句与主题词，降为 flag"),
            span_kind="omission",
            extra={"severity": sev, "rf_id": rfid, "hint_polarity": pol},
        ))
    total = len(rfs) or 1
    n_mention = sum(1 for r in rfs if r.get("hint_polarity", "mention") == "mention")
    return {
        "n_red_flags": len(rfs),
        "n_red_flags_mention": n_mention,
        "rf_hint_rate": round(sum(hits.values()) / total, 4),
        "rf_hits_rule": ";".join(f"{k}={'hit' if v else 'miss'}" for k, v in hits.items()),
        "critical_miss_rule": bool(crit_miss),
        "critical_missed_ids": crit_miss,
        "rf_violations": violations,
    }


# =====================================================================
# 检测器：key_points 加权覆盖率（只记中间量，不产错误码）
# =====================================================================
def det_key_points(text: str, q: dict, out: list[Finding]) -> dict:
    """漏要点不产错误码：归 D1/D3 由人工裁定，规则层不越权判错。

    这里只产出 rubric 需要的可数中间量。重叠率用字符二元组，因为 key_points
    没有 detection_hint 字段（schema v1.0 只给 red_flags 配了），关键词精确
    匹配对改写型回答的召回会是 0。
    """
    hay = bigrams(text)
    kps = q.get("key_points") or []
    tw = sum(int(k.get("weight", 1)) for k in kps) or 1
    hit_w = 0
    per_kp: dict[str, float] = {}
    missed_must: list[str] = []
    for k in kps:
        cov = coverage(k.get("text", ""), hay)
        per_kp[k.get("kp_id", "?")] = round(cov, 3)
        if cov >= D.CHAIN_OVERLAP_THRESHOLD:
            hit_w += int(k.get("weight", 1))
        elif k.get("must_have"):
            missed_must.append(k.get("kp_id", "?"))
    return {
        "kp_coverage_weighted": round(hit_w / tw, 4),
        "kp_coverage_rule": per_kp,
        "kp_must_have_missed": missed_must,
    }


# =====================================================================
# 检测器：required_chain 缺步（E4b，规则辅助 → flag）
# =====================================================================
def det_required_chain(text: str, q: dict, out: list[Finding]) -> dict:
    hay = bigrams(text)
    chain = q.get("required_chain") or []
    gaps: list[str] = []
    for i, step in enumerate(chain, 1):
        cov = coverage(step, hay)
        if cov < D.CHAIN_OVERLAP_THRESHOLD:
            gaps.append(step)
            out.append(mk(
                "E4b", FLAG,
                f"required_chain 第 {i} 步「{step}」在回答中缺失"
                f"（字符二元组重叠率 {cov:.2f} < {D.CHAIN_OVERLAP_THRESHOLD}）",
                (0, 0), q["qid"], "", f"required_chain/{i}",
                note="规则层只做辅助判定；改写但正确的推理步会被误报，故一律 flag",
                span_kind="omission",
                extra={"chain_step": step, "overlap": round(cov, 3)},
            ))
    return {"n_chain": len(chain), "n_gap_rule": len(gaps), "chain_gaps": gaps}


# =====================================================================
# 检测器：术语混淆（E1b）
# =====================================================================
def _line_spans(text: str) -> list[tuple[int, str]]:
    """以**行**为命题单元返回 (起始位置, 行)。

    术语共现（E1b）不能用「句号切分」的句子做单元：临床回答大量使用项目符号与
    编号列表，按句号切会把整块列表当成一句。实测这造成两个错误——注入的等价替换句
    的 span 从「**推理链**：」开始，禁止性框架守卫顺着往上爬到「必须优先排除的
    安全项」标题，把真缺陷误判成"被列举否定的引用"；同时红旗原文里的正确对比表述
    也被拉进同一个超大单元。行是读者实际比对两个术语的最小单元。
    """
    out: list[tuple[int, str]] = []
    pos = 0
    for ln in text.split("\n"):
        if ln.strip():
            out.append((pos, ln))
        pos += len(ln) + 1
    return out


def det_terms(text: str, q: dict, out: list[Finding]) -> dict:
    """同句共现 → **只记中间量，不产 finding**；同句共现 + 未否定的替换标记 → confirmed。

    两条收紧理由（error_taxonomy.md §2.8.1 第 2 条）：
      1. 「不要用 RECIST 1.1，应改用 mRECIST」同时含两个术语，且完全正确。
      2. 把"裸共现"也报成 flag 会造成警报疲劳：实测 20 份零缺陷回答产出 17 条
         这类 flag，全部是正确的对比表述。130 份真实回答会产出上千条，人工裁定
         通道会被淹掉，而**一个被忽略的 flag 通道比没有这个通道更危险**。
         裸共现因此降级为中间量 `n_term_pairs_cooccur`，供统计层做相关性分析。
    """
    n_conf = n_cooccur = 0
    for pair in D.TERM_CONFUSION:
        a, b = pair["correct"], pair["confused"]
        # 术语条目本身带括号注解（如「cTACE（碘油+化疗药）」），取括号前的主名
        a_key = re.split(r"[（(]", a)[0].strip()
        b_key = re.split(r"[（(]", b)[0].strip()
        if not a_key or not b_key:
            continue
        for s_start, sent in _line_spans(text):
            if a_key not in sent or b_key not in sent:
                continue
            n_cooccur += 1
            markers = [m for m in D.SUBSTITUTION_MARKERS if m in sent]
            if not markers:
                continue
            hit = None
            for m in markers:
                p = sent.find(m)
                if p < 0:
                    continue
                pre = sent[max(0, p - 2):p]
                # 「不通用」「并非等同」这类否定式恰恰是正确表述
                if any(x in pre for x in ("不", "非", "未", "无", "别")):
                    continue
                if negated(sent[max(0, p - 12):p + len(m) + 12]):
                    continue
                hit = m
                break
            if not hit or _framed_as_prohibition(text, s_start):
                continue
            n_conf += 1
            out.append(mk(
                "E1b", CONFIRMED,
                f"把「{a_key}」与「{b_key}」作等价替换（标记词「{hit}」）：{sent.strip()[:80]}"
                f"　混淆后果：{pair['why']}",
                (s_start, s_start + len(sent)), q["qid"], "",
                f"term_confusion/{a_key}|{b_key}",
                extra={"correct_term": a_key, "confused_term": b_key, "marker": hit},
            ))
    return {"n_term_confusion_confirmed": n_conf, "n_term_pairs_cooccur": n_cooccur}


def _sentence_spans(text: str) -> list[tuple[int, str]]:
    """返回 (起始字符位置, 句子)，供 char_span 使用。"""
    out: list[tuple[int, str]] = []
    pos = 0
    for s in _SENT_SPLIT.split(text):
        if not s:
            continue
        idx = text.find(s, pos)
        if idx < 0:
            idx = pos
        if s.strip():
            out.append((idx, s))
        pos = idx + len(s)
    return out


# =====================================================================
# 检测器：时间敏感断言（E1c，一律 flag）
# =====================================================================
def det_time_sensitive(text: str, q: dict, out: list[Finding]) -> dict:
    n = 0
    for d in D.TIME_SENSITIVE:
        for m in re.finditer(d["pattern"], text):
            span = m.group(0)
            # 前后各 20 字查否定/纠正词；再查是否处在「列举出来以便否定」的框架内
            if negated(span) or negated(window(text, m.start(), m.end(), 20)):
                continue
            if _framed_as_prohibition(text, m.start()):
                continue
            n += 1
            out.append(mk(
                "E1c", FLAG,
                f"命中时间敏感断言「{d['assertion']}」：…{snippet(text, m.start(), m.end())}…",
                (m.start(), m.end()), q["qid"], "", "time_sensitive",
                note=f"为何过时：{d['why']}　核实依据：{d['basis']}"
                     f"　词典最后核对 {D.DOC_LAST_CHECKED}，需人工确认当前证据状态",
                extra={"assertion": d["assertion"]},
            ))
    return {"n_time_sensitive": n}


# =====================================================================
# 检测器：数量级与单位（E9 confirmed / E1a flag）
# =====================================================================
def det_magnitude(text: str, q: dict, out: list[Finding]) -> dict:
    """白名单只取「各口径并集的上界 + 一个明显荒谬的判错线」。

    文档 §2.5 明确要求「不用单一数字判错」——非肿瘤肝限值在文献里真实冲突
    （树脂 40 vs 50–70 Gy；玻璃 70 vs 88 vs <90 vs <120 Gy）。因此：
      超出荒谬线 → confirmed；落在冲突区间 → flag；两者都不算 → 不报。
    """
    n_conf = n_flag = 0
    seen: set[tuple] = set()
    for entry in D.MAGNITUDE_WHITELIST:
        for m in re.finditer(entry["number_re"], text):
            try:
                val = float(m.group(1))
            except (ValueError, IndexError):
                continue
            unit = m.group(2) if m.re.groups >= 2 and m.lastindex and m.lastindex >= 2 else entry.get("unit", "")
            unit = (unit or "").strip()
            # 归因只看数字**之前**的窄窗口：中文里量的名称在数值之前。
            # 对称宽窗口会把下一句的另一个量名错当成当前数值的语境（实测 SIRT-013）。
            pre = text[max(0, m.start() - D.CONTEXT_ATTRIB_CHARS): m.start()]
            w = window(text, m.start(), m.end(), 25)   # 单次/累积这类修饰语可前可后
            if entry["context_required"] and not any(c in pre for c in entry["context_required"]):
                continue
            if any(c in pre for c in entry["context_excluded"]):
                continue
            key = (m.start(), m.end(), entry["quantity"].split("（")[0])
            if key in seen:
                continue

            fa = entry.get("fail_above")
            fb = entry.get("fail_below")
            fa_lim = fa.get(unit) if isinstance(fa, dict) else fa
            verdict = None
            why = ""
            if fb is not None and val < fb:
                verdict, why = CONFIRMED, f"{val} 低于定义域下限 {fb}"
            elif fa_lim is not None and val > fa_lim:
                verdict, why = CONFIRMED, f"{val} {unit} 超出荒谬判错线 {fa_lim} {unit}"
            else:
                sd = entry.get("single_dose_line")
                if sd:
                    sd_lim = sd.get(unit) if isinstance(sd, dict) else sd
                    if sd_lim is not None and val > sd_lim and \
                       any(k in w for k in entry.get("single_markers", [])):
                        verdict = CONFIRMED
                        why = f"声称单次安全上限 {val} {unit} > {sd_lim} {unit}"
                fl = entry.get("flag_above")
                if verdict is None and fl:
                    fl_lim = fl.get(unit) if isinstance(fl, dict) else fl
                    if fl_lim is not None and val > fl_lim:
                        verdict = FLAG
                        why = (f"{val} {unit} 落在文献冲突区间（>{fl_lim} 但未达荒谬线 "
                               f"{fa_lim} {unit}），可能引用了另一套合法口径")
                allowed = entry.get("allowed_set")
                if verdict is None and allowed:
                    tol = entry.get("allowed_tol_pct", 2.0) / 100.0
                    if not any(abs(val - a) <= a * tol for a in allowed):
                        verdict = FLAG
                        why = f"{val} 不在权威取值集合 {allowed}（容差 ±{tol:.0%}）内"
            if verdict is None:
                continue

            code = entry["verdict_confirmed"] if verdict == CONFIRMED else entry.get("verdict_flag")
            if not code:
                continue
            seen.add(key)
            if verdict == CONFIRMED:
                n_conf += 1
            else:
                n_flag += 1
            out.append(mk(
                code, verdict,
                f"「{entry['quantity']}」测得 {val} {unit}：{why}。"
                f"　可接受区间：{entry['acceptable']}　判错线：{entry['fail_rule']}",
                (m.start(), m.end()), q["qid"], "",
                f"magnitude/{entry['quantity']}",
                note=f"核实依据：{entry['basis']}",
                extra={"quantity": entry["quantity"], "value": val, "unit": unit},
            ))
    return {"n_magnitude_confirmed": n_conf, "n_magnitude_flag": n_flag}


def det_unit_context(text: str, q: dict, out: list[Finding]) -> dict:
    """量纲错配：活度用 Gy 表述、吸收剂量用 GBq 表述。

    这是 GBq↔mCi / Gy↔cGy 之外最常见的数量级失效形态（27 倍与 100 倍差）。
    只 flag：语境归因不够确定，且「120 Gy 处方」与「3 GBq 活度」常在同一段并列。
    """
    n = 0
    pats = [
        (r"(活度|activity)[^。；\n]{0,14}(\d+(?:\.\d+)?)\s*(Gy|cGy)", "活度", "Gy"),
        (r"(吸收剂量|受量|靶剂量|剂量限值|肺剂量)[^。；\n]{0,14}(\d+(?:\.\d+)?)\s*(GBq|mCi)",
         "吸收剂量", "GBq"),
    ]
    for pat, expect, wrong in pats:
        for m in re.finditer(pat, text):
            n += 1
            out.append(mk(
                "E9", FLAG,
                f"量纲错配：{expect}应以{'GBq/mCi' if expect == '活度' else 'Gy'}表述，"
                f"回答写成「{m.group(0).strip()[:50]}」（{wrong}）",
                (m.start(), m.end()), q["qid"], "", "unit_context",
                note="语境归因不确定 → flag；确认后可升级为 E9 confirmed",
            ))
    return {"n_unit_mismatch": n}


def det_unverified(text: str, q: dict, out: list[Finding]) -> dict:
    """文档 §2.5 明确「仍未核实、因此仍未启用 E9」的量：只 flag，永不 confirmed。"""
    n = 0
    for entry in D.UNVERIFIED_QUANTITIES:
        for m in re.finditer(entry["pattern"], text):
            n += 1
            out.append(mk(
                entry["verdict_flag"], FLAG,
                f"「{entry['quantity']}」出现具体数值「{m.group(0).strip()[:50]}」，"
                f"但该阈值{entry['reason']}",
                (m.start(), m.end()), q["qid"], "", "unverified_quantity",
                note="未核实的量不得进入标准答案，也不得由规则层判错",
                extra={"quantity": entry["quantity"]},
            ))
    return {"n_unverified_quantity": n}


# =====================================================================
# 检测器：引用白名单（E3，永远只能 flag）
# =====================================================================
def det_citations(text: str, q: dict, out: list[Finding], wl: set[str]) -> dict:
    n_flag = n_ok = 0
    for name, pat in D.CITATION_PATTERNS.items():
        for m in re.finditer(pat, text):
            tok = m.group(0).strip()
            if _in_whitelist(tok, wl):
                n_ok += 1
                continue
            n_flag += 1
            out.append(mk(
                "E3", FLAG,
                f"引用「{tok}」（{name}）未命中白名单",
                (m.start(), m.end()), q["qid"], "", f"citation/{name}",
                note="白名单不可能穷尽真实文献，E3 永远只是 flag；"
                     "报告中必须把「确认幻觉数」与「无法裁定数」分开列",
                extra={"citation": tok, "pattern": name},
            ))
    return {"n_citation_flag": n_flag, "n_citation_ok": n_ok}


def _in_whitelist(tok: str, wl: set[str]) -> bool:
    """双向子串匹配：白名单条目与抽取到的引用常一方带年份、一方不带。"""
    t = D.norm(tok).strip("《》")
    if not t:
        return True
    if t in wl:
        return True
    return any(len(w) >= 4 and (t in w or w in t) for w in wl)


# =====================================================================
# 检测器：安全冗余（E8）
# =====================================================================
def det_redundancy(text: str, q: dict, out: list[Finding]) -> dict:
    """占比而非计数：结尾一句免责是合理的，占掉四分之一篇幅才该罚。"""
    total = len(re.sub(r"\s+", "", text)) or 1
    red_chars = 0
    red_sents: list[str] = []
    for s in sentences(text):
        if any(t in s for t in D.DISCLAIMER_TEMPLATES) or \
           any(c in s for c in D.BACKGROUND_EDU_CUES):
            red_chars += len(re.sub(r"\s+", "", s))
            red_sents.append(s.strip())
    ratio = red_chars / total
    if ratio >= 0.25:
        out.append(mk(
            "E8", CONFIRMED,
            f"免责声明+疾病科普占全文 {ratio:.1%}（≥25%），冗余 {red_chars} 字／全文 {total} 字。"
            f"被计为冗余的段落首句：「{red_sents[0][:50] if red_sents else ''}」",
            (text.find(red_sents[0]) if red_sents else 0,
             (text.find(red_sents[0]) + len(red_sents[0])) if red_sents else 0),
            q["qid"], "", "redundancy_ratio",
            note="rubric D5 要求评分者写出被计为冗余的是哪一段，规则层同样给出首句",
            extra={"redundancy_ratio": round(ratio, 4), "n_redundant_sentences": len(red_sents)},
        ))
    return {"redundancy_ratio": round(ratio, 4), "n_redundant_sentences": len(red_sents)}


# =====================================================================
# 检测器：答非所问 / 结论未前置（E7）
# =====================================================================
def det_offtarget(text: str, q: dict, out: list[Finding]) -> dict:
    """双条件判定，理由见 error_taxonomy.md §2.8.1 第 3 条。

    单看「前 3 句无术式名」会在 7/20 份零缺陷回答上误报——剂量计算题的提问
    本身就不含术式名。单看「开头无结论标记」又会漏掉答非所问但结论前置的回答。
    两个条件都满足才 confirmed；核心实体无法确定为**特异**术式词时降为 flag。

    两处实测修正：
      * 结论标记匹配前先剔除免责声明模板句。否则「不构成医疗**建议**」里的
        「建议」会冒充结论标记，让开头全是免责段落的答案静默通过。
      * 核心实体只用特异词。通用词（消融/系统治疗/剂量…）会出现在任何泛泛
        铺垫里，实测学科史填充段就含「消融、介入、放疗、系统治疗」。
    """
    lead = lead_window(text)
    lead_no_disc = lead
    for t in D.DISCLAIMER_TEMPLATES:
        lead_no_disc = lead_no_disc.replace(t, " ")
    first3 = "".join(sentences(text)[:3])

    spec = [p for p in D.PROCEDURE_ENTITIES_SPECIFIC if p in q.get("question", "")]
    fallback = False
    if not spec:
        spec = [p for p in D.PROCEDURE_ENTITIES_SPECIFIC if p in q.get("stem", "")]
        fallback = True
    ents = spec or [p for p in D.PROCEDURE_ENTITIES if p in q.get("question", "")] or \
        [p for p in D.PROCEDURE_ENTITIES if p in q.get("stem", "")]

    no_entity = bool(ents) and not any(e in lead for e in ents)
    no_conclusion = not any(m in lead_no_disc for m in D.CONCLUSION_MARKERS)
    conclusion_in_first_3 = any(m in first3 for m in D.CONCLUSION_MARKERS)

    if no_entity and no_conclusion:
        if not spec:
            out.append(mk(
                "E7", FLAG,
                f"开头 {D.LEAD_WINDOW_CHARS} 字内既无结论标记（已剔除免责声明）、"
                f"也无任何术式/决策实体：「{lead[:60]}…」",
                (0, min(len(text), D.LEAD_WINDOW_CHARS)), q["qid"], "", "offtarget",
                note="题干核心实体无法确定为特异术式词 → 不给 confirmed",
            ))
        else:
            out.append(mk(
                "E7", CONFIRMED,
                f"答非所问：开头 {D.LEAD_WINDOW_CHARS} 字内未出现题干核心实体 {spec}"
                f"（fallback={fallback}），且剔除免责声明后无任何结论标记。"
                f"开头原文：「{lead[:60]}…」",
                (0, min(len(text), D.LEAD_WINDOW_CHARS)), q["qid"], "", "offtarget",
                extra={"missing_entities": spec},
            ))
    return {
        "off_target_rule": bool(no_entity and no_conclusion),
        "conclusion_in_first_3": conclusion_in_first_3,
        "no_conclusion_in_lead": no_conclusion,
        "q_entities": ents,
        "q_entities_specific": spec,
    }


# =====================================================================
# 检测器：过度自信（E5）与过度对冲（E6）
# =====================================================================
def _count_exclusive(text: str) -> tuple[int, int, list[tuple[str, int]]]:
    """返回 (计入数, 豁免数, [(词, 位置)])。豁免规则见 dictionaries.EXCLUSIVE_EXEMPTIONS。"""
    kept: list[tuple[str, int]] = []
    n_exempt = 0
    for w in D.EXCLUSIVE_WORDING:
        start = 0
        while True:
            p = text.find(w, start)
            if p < 0:
                break
            start = p + 1
            ex = [e for e in D.EXCLUSIVE_EXEMPTIONS if e.startswith(w)
                  and text[p:p + len(e)] == e]
            if ex:
                n_exempt += 1
                continue
            kept.append((w, p))
    return len(kept), n_exempt, kept


def _densest_hit(hits: list[tuple[str, int]], radius: int = 100) -> tuple[str, int]:
    """取排他措辞最密集处作为证据位置，而不是全文第一处。

    第一处往往是无关的术语用法，指向它会让证据失去定位价值——char_span 的意义
    就是让人一眼跳到该看的地方。
    """
    best = hits[0]
    best_n = -1
    for w, p in hits:
        cnt = sum(1 for _, p2 in hits if abs(p2 - p) <= radius)
        if cnt > best_n:
            best, best_n = (w, p), cnt
    return best


def det_exclusive(text: str, q: dict, out: list[Finding]) -> dict:
    n, n_exempt, hits = _count_exclusive(text)
    divergence = any(m in text for m in D.DIVERGENCE_MARKERS)
    ad = q.get("acceptable_divergence", "")
    if n >= 2:
        anchor_w, anchor_p = _densest_hit(hits)
        near = sorted((p for _, p in hits if abs(p - anchor_p) <= 100))
        ev = "；".join(f"「{w}」@{p}" for w, p in sorted(hits, key=lambda x: abs(x[1] - anchor_p))[:5])
        if not divergence:
            out.append(mk(
                "E5", CONFIRMED,
                f"排他性措辞 {n} 处（已扣除 {n_exempt} 处术语豁免），且全文无任何争议双向呈现标记；"
                f"其中 {len(near)} 处集中在 {anchor_p} 附近 100 字内。"
                f"命中：{ev}　本题公认存在分歧：{ad[:60]}…",
                (anchor_p, anchor_p + len(anchor_w)), q["qid"], "", "exclusive_wording",
                extra={"n_exclusive": n, "n_exempted": n_exempt},
            ))
        else:
            out.append(mk(
                "E5", FLAG,
                f"排他性措辞 {n} 处（另有 {n_exempt} 处术语豁免已扣除），但回答确实呈现了争议。"
                f"需人工按 rubric D4 判断这些措辞是否落在 acceptable_divergence 覆盖的争议点上。"
                f"命中：{ev}",
                (anchor_p, anchor_p + len(anchor_w)), q["qid"], "", "exclusive_wording",
                note="rubric D4 的豁免须由评分者写备注；规则层默认不豁免、只降级为 flag",
                extra={"n_exclusive": n, "n_exempted": n_exempt},
            ))
    return {
        "n_exclusive": n,
        "n_exclusive_exempted": n_exempt,
        "divergence_presented": divergence,
    }


def det_hedge(text: str, q: dict, out: list[Finding]) -> dict:
    """E6 不数密度：只看模糊措辞是否落在 must_have 要点上（文档 §2.3）。"""
    n_uncertain = sum(text.count(w) for w in D.HEDGE_WORDING)
    spans = _sentence_spans(text)
    hay = bigrams(text)
    flagged = 0
    for k in q.get("key_points") or []:
        if not k.get("must_have"):
            continue
        best, best_cov = None, 0.0
        for s_start, s in spans:
            c = coverage(k.get("text", ""), bigrams(s))
            if c > best_cov:
                best, best_cov = (s_start, s), c
        if not best or best_cov < D.CHAIN_OVERLAP_THRESHOLD:
            continue
        s_start, s = best
        hedges = [w for w in D.HEDGE_WORDING if w in s]
        if not hedges:
            continue
        flagged += 1
        out.append(mk(
            "E6", FLAG,
            f"必答要点 {k.get('kp_id')} 的对应句含模糊措辞 {hedges}（重叠率 {best_cov:.2f}）：{s.strip()[:70]}",
            (s_start, s_start + len(s)), q["qid"], "", f"overhedge/{k.get('kp_id')}",
            note="规则层无法判断该要点是否真有唯一答案 → 一律 flag，由人工按 must_have 裁定",
            extra={"kp_id": k.get("kp_id"), "hedges": hedges},
        ))
    return {"n_uncertain": n_uncertain, "n_overhedge_flag": flagged}


# =====================================================================
# 检测器：自相矛盾（E10，显式否定词冲突 → flag）
# =====================================================================
_PROCS = ["Y90", "Y-90", "SIRT", "TACE", "DEB-TACE", "cTACE", "HAIC",
          "肝移植", "消融", "系统治疗", "放射栓塞"]


def det_contradiction(text: str, q: dict, out: list[Finding]) -> dict:
    n = 0
    for proc in _PROCS:
        start = 0
        while True:
            p = text.find(proc, start)
            if p < 0:
                break
            start = p + len(proc)
            w = text[max(0, p - 14): p + len(proc) + 14]
            rec = [m for m in D.RECOMMEND_MARKERS if m in w]
            pro = [m for m in D.PROHIBIT_MARKERS if m in w]
            if rec and pro:
                n += 1
                out.append(mk(
                    "E10", FLAG,
                    f"同一术式「{proc}」在 {len(w)} 字窗口内同时被推荐 {rec} 与禁止 {pro}："
                    f"「{w.strip()}」",
                    (max(0, p - 14), p + len(proc) + 14), q["qid"], "",
                    f"contradiction/{proc}",
                    note="显式否定词冲突只是线索；语义级矛盾（文档 §1 E10）由 LLM-judge 判",
                    extra={"procedure": proc},
                ))
                break  # 同一术式只报一次，避免一处矛盾刷出多条
    return {"n_contradiction_flag": n}


# =====================================================================
# 检测器：拒答（E11）
# =====================================================================
def det_refusal(text: str, q: dict, metrics: dict, out: list[Finding]) -> dict:
    marks = [m for m in D.REFUSAL_MARKERS if m in text]
    cov = metrics.get("kp_coverage_weighted", 0.0)
    refused = bool(marks) and cov < 0.15
    if refused:
        p = text.find(marks[0])
        out.append(mk(
            "E11", CONFIRMED,
            f"未给出可评估的临床内容：出现拒答标记 {marks[:3]}，且 key_points 加权覆盖率仅 {cov:.0%}",
            (p, p + len(marks[0])), q["qid"], "", "refusal",
            extra={"refusal_markers": marks, "kp_coverage_weighted": cov},
        ))
    return {"refusal_rule": refused, "refusal_markers": marks}


# =====================================================================
# 检测器：虚构题干信息（E12）
# =====================================================================
def _allowed_lab_values(q: dict) -> dict[str, list[float]]:
    """允许值集合 = vignette.labs 结构化值 ∪ stem 散文里抽出的值。

    必须并入 stem：实测 20 题中 10 题的 stem 含检验值，而 SIRT-004、COMBO-003
    的 vignette.labs 为空。只比结构化字段，这两题的正确答案会被全数误判为虚构。
    baseline 字段也要并入：复查题的正确答案必然同时引用基线与当前值。
    """
    v = q.get("vignette") or {}
    labs = v.get("labs") or {}
    allowed: dict[str, list[float]] = {}
    for name, spec in D.LAB_ENTITIES.items():
        vals: list[float] = []
        for k in spec["vignette_keys"]:
            x = labs.get(k)
            if isinstance(x, (int, float)) and not isinstance(x, bool):
                vals.append(float(x))
        stem = q.get("stem", "")
        for alias in spec["aliases"]:
            for m in re.finditer(rf"{re.escape(alias)}[^。；\n0-9]{{0,8}}(\d+(?:\.\d+)?)", stem):
                vals.append(float(m.group(1)))
        if vals:
            allowed[name] = vals
    return allowed


def _close(a: float, pool: list[float]) -> bool:
    return any(abs(a - b) <= max(0.3, abs(b) * 0.05) for b in pool)


def det_fabricated_labs(text: str, q: dict, out: list[Finding]) -> dict:
    """E12 是本项目专门加的一类，也是幻觉在临床场景里最危险的形态。

    三个必要条件缺一不可，否则「胆红素 <1.1 mg/dL 时禁忌」这类**正确**的指南
    语句会被判成虚构：
      1) 命中检验实体别名；2) 单位可换算到该实体的规范单位；
      3) 数值前无阈值比较符，且有患者指代或数值与允许值差异 ≥2 倍。
    """
    allowed = _allowed_lab_values(q)
    vignette_labs = (q.get("vignette") or {}).get("labs") or {}
    n = 0
    for name, spec in D.LAB_ENTITIES.items():
        units = sorted(spec["conv"], key=len, reverse=True)
        unit_alt = "|".join(re.escape(u) for u in units if u)
        alias_alt = "|".join(re.escape(a) for a in sorted(spec["aliases"], key=len, reverse=True))
        pat = (rf"(?P<pre>[^。；\n]{{0,14}})(?P<alias>{alias_alt})"
               rf"(?P<gap>[^。；\n0-9]{{0,8}})(?P<num>\d+(?:\.\d+)?)"
               rf"(?:\s*(?P<unit>{unit_alt}))?")
        for m in re.finditer(pat, text):
            gap = m.group("gap")
            num_s = m.group("num")
            # 条件 3a：阈值比较符 → 这是指南阈值，不是本例检验值
            if any(c in gap or c in num_s for c in D.THRESHOLD_COMPARATORS):
                continue
            # 条件 3b：差值断言不在 E12 覆盖范围内（能力边界，见 dictionaries.DELTA_MARKERS）
            if any(dm in gap for dm in D.DELTA_MARKERS) or \
               any(dm in text[max(0, m.start("num") - 3):m.start("num")] for dm in D.DELTA_MARKERS):
                continue
            if re.search(r"[/×x]\s*10|10\^", gap + num_s):
                continue
            raw = float(num_s)
            unit = (m.group("unit") or "").strip()
            # 条件 2：单位必须可换算到规范单位。无单位时只在实体唯一可解释时继续
            if unit:
                factor = spec["conv"].get(unit)
                if factor is None:
                    continue
                val = raw * factor
            elif spec["unit"]:
                val = raw
            else:
                val = raw
            pool = allowed.get(name, [])
            if pool and _close(val, pool):
                continue
            pre = m.group("pre")
            has_patient = any(pm in pre for pm in D.PATIENT_MARKERS)
            ratio_bad = bool(pool) and any(val >= b * 2 or (b and val <= b * 0.5) for b in pool)
            unknown_entity = not pool and name not in {
                k for k, s in D.LAB_ENTITIES.items()
                if any(kk in vignette_labs for kk in s["vignette_keys"])}
            if not (has_patient or ratio_bad or unknown_entity):
                continue
            if not (pool or unknown_entity):
                continue
            n += 1
            ambiguous = spec.get("ambiguous") or (unit and unit != spec["unit"])
            verdict = FLAG if ambiguous else CONFIRMED
            detail = (f"vignette 中 {name} 的允许值为 {pool}" if pool
                      else f"vignette 与 stem 均未提供 {name} 的任何数值")
            out.append(mk(
                "E12", verdict,
                f"虚构题干信息：回答断言「{m.group(0).strip()[:40]}」（{name}={val} {spec['unit'] or '无单位'}），"
                f"而{detail}。　患者指代={has_patient}　差异≥2倍={ratio_bad}",
                (m.start("alias"), m.end()), q["qid"], "", f"fabricated_lab/{name}",
                note="PT 与跨单位换算一律降为 flag：vignette 记的是延长秒数而回答常写绝对秒数"
                     if ambiguous else "推理链可能完全自洽，只有与 vignette 逐字段比对才能抓到",
                extra={"lab": name, "asserted_value": val, "allowed_values": pool,
                       "unit_in_text": unit},
            ))
    return {"n_fabricated_lab": n}


# =====================================================================
# 汇总
# =====================================================================
def analyze(text: str, q: dict, wl: set[str]) -> tuple[dict, list[Finding]]:
    out: list[Finding] = []
    metrics: dict = {"char_count": len(text), "word_count_rule": len(re.sub(r"\s+", "", text))}
    metrics.update(det_red_flags(text, q, out))
    metrics.update(det_key_points(text, q, out))
    metrics.update(det_required_chain(text, q, out))
    metrics.update(det_terms(text, q, out))
    metrics.update(det_time_sensitive(text, q, out))
    metrics.update(det_magnitude(text, q, out))
    metrics.update(det_unit_context(text, q, out))
    metrics.update(det_unverified(text, q, out))
    metrics.update(det_citations(text, q, out, wl))
    metrics.update(det_redundancy(text, q, out))
    metrics.update(det_offtarget(text, q, out))
    metrics.update(det_exclusive(text, q, out))
    metrics.update(det_hedge(text, q, out))
    metrics.update(det_contradiction(text, q, out))
    metrics.update(det_refusal(text, q, metrics, out))
    metrics.update(det_fabricated_labs(text, q, out))
    metrics["n_confirmed"] = sum(1 for f in out if f["verdict"] == CONFIRMED)
    metrics["n_flag"] = sum(1 for f in out if f["verdict"] == FLAG)
    metrics["codes_confirmed"] = sorted({f["code"] for f in out if f["verdict"] == CONFIRMED})
    metrics["codes_flag"] = sorted({f["code"] for f in out if f["verdict"] == FLAG})
    return metrics, out


def load_questions(path: Path = QUESTIONS) -> dict[str, dict]:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            q = json.loads(line)
            out[q["qid"]] = q
    return out


def load_responses(source: str, only: set[str] | None) -> tuple[list[dict], str]:
    if source == "mock":
        path, note = MOCK_RESPONSES, "缺陷注入的阳性/阴性对照，非真实模型输出"
    elif source == "demo":
        path, note = DEMO_RESPONSES, "DEMO 假数据，任何基于它的结果都无意义"
    else:
        path, note = RESPONSES, "真实回答"
        if not path.exists():
            if DEMO_RESPONSES.exists():
                path, note = DEMO_RESPONSES, "未找到真实回答，回退到 DEMO 假数据（结果无意义）"
            elif MOCK_RESPONSES.exists():
                path, note = MOCK_RESPONSES, "未找到真实回答，回退到 mock 对照数据（结果无意义）"
    if not path.exists():
        sys.exit(f"[ERROR] 找不到回答文件：{path}\n"
                 f"        真实回答请用 tools/paste_recorder.html 取回；"
                 f"跑通流程用 `python src/mock_models.py --clean`")
    recs = []
    for ln, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError as e:
            sys.exit(f"[ERROR] {path.name} 第 {ln} 行 JSON 解析失败：{e}")
        if not r.get("qid") or not r.get("text"):
            sys.exit(f"[ERROR] {path.name} 第 {ln} 行缺 qid 或 text")
        if only and r["qid"] not in only:
            continue
        recs.append(r)
    return recs, note


def _load_key() -> dict[tuple[str, str], str]:
    """(qid, model) -> 匿名 response_id。盲测期间不该看，故仅 --with-key 时载入。"""
    if not KEY_FILE.exists():
        return {}
    m = {}
    for line in KEY_FILE.read_text(encoding="utf-8").splitlines():
        if line.strip():
            k = json.loads(line)
            m[(k["qid"], k["model"])] = k["response_id"]
    return m


def run(source: str, only: set[str] | None, out_dir: Path, with_key: bool) -> tuple[list[dict], list[Finding]]:
    qs = load_questions()
    recs, note = load_responses(source, only)
    wl = D.reference_whitelist()
    keymap = _load_key() if with_key else {}

    all_metrics: list[dict] = []
    all_findings: list[Finding] = []
    unknown = sorted({r["qid"] for r in recs} - set(qs))
    if unknown:
        print(f"{console.WARN} {len(unknown)} 个 qid 不在题库中，跳过：{unknown[:5]}", file=sys.stderr)

    for r in recs:
        q = qs.get(r["qid"])
        if not q:
            continue
        metrics, findings = analyze(r["text"], q, wl)
        rid = keymap.get((r["qid"], r["model"]), "")
        base = {"qid": r["qid"], "model": r["model"], "response_id": rid,
                "is_mock": bool(r.get("is_mock")), "source": source}
        all_metrics.append(base | metrics)
        for f in findings:
            f["model"] = r["model"]
            f["response_id"] = rid
            f["is_mock"] = bool(r.get("is_mock"))
            all_findings.append(f)

    out_dir.mkdir(parents=True, exist_ok=True)
    fp = out_dir / "rule_findings.jsonl"
    mp = out_dir / "rule_metrics.jsonl"
    fp.write_text("\n".join(json.dumps(f, ensure_ascii=False) for f in all_findings) + "\n",
                  encoding="utf-8")
    mp.write_text("\n".join(json.dumps(m, ensure_ascii=False) for m in all_metrics) + "\n",
                  encoding="utf-8")
    return all_metrics, all_findings


# =====================================================================
# 阳性 + 阴性对照自检
# =====================================================================
def self_test(only: set[str] | None, verbose: bool) -> int:
    """阶段 3 的验收门（error_taxonomy.md §5）：

      1. 对每一类被注入的缺陷，规则层 recall > 0
      2. mock_clean 上的 confirmed 数**必须为 0**
      3. 文档规定只能 flag 的错误码，代码不得输出 confirmed

    第 2 条是硬门。第 3 条防止规则层悄悄给自己提权。
    """
    if not MOCK_RESPONSES.exists() or not MOCK_TRUTH.exists():
        sys.exit("[ERROR] 缺 mock 数据，先运行：python src/mock_models.py --clean")

    qs = load_questions()
    recs = [json.loads(l) for l in MOCK_RESPONSES.read_text(encoding="utf-8").splitlines() if l.strip()]
    if only:
        recs = [r for r in recs if r["qid"] in only]
    truths = [json.loads(l) for l in MOCK_TRUTH.read_text(encoding="utf-8").splitlines() if l.strip()]
    tmap = {(t["qid"], t["model"]): t for t in truths}
    wl = D.reference_whitelist()

    per_code: dict[str, Counter] = defaultdict(Counter)
    clean_confirmed: list[Finding] = []
    clean_flags: Counter = Counter()
    n_clean = 0
    escalation_violations: list[str] = []
    all_findings: list[Finding] = []
    all_metrics: list[dict] = []
    evidence_level: Counter = Counter()

    for r in recs:
        q = qs.get(r["qid"])
        if not q:
            continue
        metrics, findings = analyze(r["text"], q, wl)
        for f in findings:
            f["model"] = r["model"]
            f["is_mock"] = True
            all_findings.append(f)
            if f["verdict"] == CONFIRMED and CODE_MAX_VERDICT.get(f["code"]) != CONFIRMED:
                escalation_violations.append(f"{f['qid']}/{f['code']}")
        all_metrics.append({"qid": r["qid"], "model": r["model"], "source": "mock"} | metrics)

        t = tmap.get((r["qid"], r["model"]))
        if r["model"] == "mock_clean":
            n_clean += 1
            for f in findings:
                if f["verdict"] == CONFIRMED:
                    clean_confirmed.append(f)
                else:
                    clean_flags[f["code"]] += 1
            continue
        if not t:
            continue
        detected = {f["code"] for f in findings}
        detected_conf = {f["code"] for f in findings if f["verdict"] == CONFIRMED}
        for inj in t["injected"]:
            code = inj["code"]
            if inj.get("achieved", True) is False:
                # 注入没成功就不进 recall 分母。把"没注入成功"算成"没检出"，
                # 会逼着人去放松检测阈值——那是最坏的应对。
                per_code[code]["injection_failed"] += 1
                continue
            per_code[code]["injected"] += 1
            if code in detected:
                per_code[code]["detected"] += 1
            if code in detected or (EQUIVALENT_CODES.get(code, {code}) & detected):
                per_code[code]["detected_equiv"] += 1
            if code in detected_conf:
                per_code[code]["detected_confirmed"] += 1
            # 证据级：注入的具体文本是否落在某条 finding 的 char_span 附近。
            # 漏报型错误码不适用（证据文本已被删除），不计入分母。
            if code in OMISSION_CODES:
                continue
            sub = (inj.get("evidence_substring") or "")[:12]
            if sub and any(sub in r["text"][max(0, f["char_span"][0] - 80):f["char_span"][1] + 80]
                           for f in findings if f["code"] == code):
                evidence_level[code] += 1

    # ---- 输出
    MOCK_SCORES_DIR.mkdir(parents=True, exist_ok=True)
    (MOCK_SCORES_DIR / "rule_findings.jsonl").write_text(
        "\n".join(json.dumps(f, ensure_ascii=False) for f in all_findings) + "\n", encoding="utf-8")
    (MOCK_SCORES_DIR / "rule_metrics.jsonl").write_text(
        "\n".join(json.dumps(m, ensure_ascii=False) for m in all_metrics) + "\n", encoding="utf-8")

    print("=== 阳性对照：注入缺陷的检出率 ===")
    print(f"{'代码':<6}{'注入':>6}{'注入失败':>9}{'严格':>6}{'归码等价':>10}{'confirmed':>11}"
          f"{'严格recall':>11}{'等价recall':>11}{'证据级':>8}  文档允许")
    ok = True
    for code in sorted(per_code):
        c = per_code[code]
        inj = c["injected"] or 1
        strict = c["detected"] / inj
        equiv = c["detected_equiv"] / inj
        ev_s = "漏报型" if code in OMISSION_CODES else f"{evidence_level[code] / inj:.0%}"
        allowed = CODE_MAX_VERDICT.get(code, "?")
        note = ""
        if c["injected"] == 0:
            note = f"  <-- {c['injection_failed']} 次注入全部失败，无法评估"
            ok = False
        elif c["detected_equiv"] == 0:
            note = "  <-- recall = 0，检测线索写错了"
            ok = False
        elif c["detected"] == 0:
            note = (f"  <-- 严格 recall=0，全靠归码等价 "
                    f"{sorted(EQUIVALENT_CODES.get(code, set()) - {code})} 命中")
        print(f"{code:<6}{c['injected']:>6}{c['injection_failed']:>9}{c['detected']:>6}"
              f"{c['detected_equiv']:>10}{c['detected_confirmed']:>11}"
              f"{strict:>11.0%}{equiv:>11.0%}{ev_s:>8}  {allowed}{note}")

    no_control = sorted(set(CODE_MAX_VERDICT) - set(per_code) - {"E2c", "E4a"})
    print(f"\n{console.WARN} 无阳性对照的错误码：{no_control}")
    print("     这些码只能测「零缺陷对照上的假阳性率」，recall 无法测。")
    print("     要补，就在 mock_models.py 里加对应注入器——不能靠声称。")

    print(f"\n=== 阴性对照：{n_clean} 份零缺陷回答 ===")
    print(f"confirmed 数：{len(clean_confirmed)}（验收要求 = 0）")
    if clean_confirmed:
        ok = False
        for f in clean_confirmed[:20]:
            print(f"  {console.FAIL} {f['qid']} {f['code']} [{f['rule']}] {f['evidence'][:110]}")
    else:
        print(f"  {console.OK} 无任何假阳性 confirmed")
    print(f"flag 数：{sum(clean_flags.values())}　分布：{dict(clean_flags.most_common())}")
    if clean_flags:
        print("  flag 不是假阳性判定，但数量过大说明守卫不够——需人工抽查后收紧词典")

    if escalation_violations:
        ok = False
        print(f"\n{console.FAIL} 规则层越权升级（文档规定只能 flag 却输出了 confirmed）："
              f"{sorted(set(escalation_violations))}")
    else:
        print(f"\n{console.OK} 无越权升级：E1a/E1c/E3/E4b/E6/E10 全部停留在 flag_for_review")

    # 硬性验收
    print("\n=== 验收 ===")
    crit_zero = len(clean_confirmed) == 0
    recall_ok = all(c["detected_equiv"] > 0 for c in per_code.values())
    print(f"  [{'PASS' if recall_ok else 'FAIL'}] 每类注入缺陷 recall > 0（按归码等价计）")
    print(f"  [{'PASS' if crit_zero else 'FAIL'}] mock_clean 的 confirmed 数 = 0")
    print(f"  [{'PASS' if not escalation_violations else 'FAIL'}] 无错误码被越权升级为 confirmed")
    print(f"\n产物 -> {MOCK_SCORES_DIR.relative_to(ROOT)}/rule_findings.jsonl（{len(all_findings)} 条）"
          f"、rule_metrics.jsonl（{len(all_metrics)} 行）")
    if not ok:
        print(f"\n{console.FAIL} 规则层未通过自检。修 dictionaries.py 的检测线索，不要改验收标准。")
        return 1
    print(f"\n{console.OK} 规则层自检通过。")
    return 0


# =====================================================================
# 入口
# =====================================================================
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="规则层检测器（error_taxonomy.md §1 三层分工的第一层）")
    ap.add_argument("--source", default="auto", choices=["auto", "real", "demo", "mock"],
                    help="auto=优先真实回答，缺失时回退 demo/mock 并显著警告")
    ap.add_argument("--self-test", action="store_true", help="跑阳性+阴性对照并给出 precision/recall")
    ap.add_argument("--only", default="", help="逗号分隔的 qid 子集")
    ap.add_argument("--show", default="", help="打印指定 qid 的全部 finding")
    ap.add_argument("--with-key", action="store_true",
                    help="输出里填入匿名 response_id（需已揭盲；盲测期间勿用）")
    ap.add_argument("--out-dir", default=str(RULE_DIR))
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    only = {x.strip() for x in args.only.split(",") if x.strip()} or None

    if args.self_test:
        return self_test(only, args.verbose)

    source = "auto" if args.source == "auto" else args.source
    if source == "auto":
        source = "real" if RESPONSES.exists() else ("demo" if DEMO_RESPONSES.exists() else "mock")
    if args.show:
        show_qids = {x.strip() for x in args.show.split(",") if x.strip()}
        qs = load_questions()
        recs, note = load_responses(source, show_qids)
        wl = D.reference_whitelist()
        print(f"[show] 数据源：{note}")
        for r in recs:
            q = qs.get(r["qid"])
            if not q:
                continue
            metrics, findings = analyze(r["text"], q, wl)
            print(f"\n--- {r['qid']} / {r['model']} ---")
            print(f"  confirmed {metrics['n_confirmed']} {metrics['codes_confirmed']}　"
                  f"flag {metrics['n_flag']} {metrics['codes_flag']}")
            for f in findings:
                print(f"  [{f['verdict'][:4]}] {f['code']} {f['rule']}: {f['evidence'][:150]}")
        return 0

    metrics, findings = run(source, only, Path(args.out_dir), args.with_key)
    if not metrics:
        print("[ERROR] 没有可分析的回答")
        return 1
    recs, note = load_responses(source, only)
    print(f"数据源：{note}")
    print(f"分析 {len(metrics)} 份回答，产出 {len(findings)} 条错误实例")
    conf = [f for f in findings if f["verdict"] == CONFIRMED]
    flag = [f for f in findings if f["verdict"] == FLAG]
    print(f"  confirmed {len(conf)} 条　flag_for_review {len(flag)} 条")
    print(f"  confirmed 分布：{dict(Counter(f['code'] for f in conf).most_common())}")
    print(f"  flag 分布：{dict(Counter(f['code'] for f in flag).most_common())}")
    print(f"\n{console.WARN} 「确认幻觉数」与「无法裁定数」必须分开报（error_taxonomy.md §2.7）：")
    print(f"     confirmed {len(conf)} 条可直接采信；flag {len(flag)} 条须人工裁定后才能计入。")
    by_model: dict[str, Counter] = defaultdict(Counter)
    for f in conf:
        by_model[f["model"]][f["code"]] += 1
    for m in sorted(by_model):
        print(f"     {m:<16} {dict(by_model[m].most_common())}")
    out = Path(args.out_dir)
    print(f"\n产物 -> {out.relative_to(ROOT)}/rule_findings.jsonl、rule_metrics.jsonl")
    print(f"{console.WARN} 这两个文件含**明文模型名**。盲测期间评分者不得打开，"
          f"否则匿名化失效（rubric §1 呈现顺序纪律）。")
    if source != "real":
        print(f"{console.FAIL} 当前结果基于{note}，不得用于任何结论。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
