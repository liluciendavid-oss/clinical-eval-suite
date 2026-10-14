#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
llm_judge.py — LLM-as-judge 的取回流水线（生成待粘贴输入 + 解析回填 + 证据校验）

它解决三个具体问题
------------------
1. **judge 必须来自被评面板之外**。同源 judge 会系统性偏爱与自己风格相似的输出
   （self-preference bias）。`--parse --judge-model X` 对 X ∈ 被评面板直接报错退出。
2. **无原文证据的判定一律丢弃**。每条负面判定必须带从回答里逐字复制的 `quote`，
   且该 quote 必须真的能在回答里做子串匹配（去空白后）。对不上的整条丢弃，
   并统计丢弃率——**丢弃率本身就是 judge 可靠性的一个指标**。
3. **成对比较的位置偏置**。每对生成 A/B 与 B/A 两份；只有两次同向才计入胜率，
   反向记 tie 并单独报告"位置敏感对数"。

匿名化
------
inbox 文件名**不含模型名**，只含 qid 与一个 6 位哈希槽号。模型名在
`data/judge/judge_key.jsonl` 里。理由：跑 judge 的人和人工打分的人往往是同一个，
inbox 里出现明文模型名，等于在盲测期间给评分者递了一张对照表。

用法
----
    python src/llm_judge.py --emit pointwise            # 生成 100 份待粘贴输入
    python src/llm_judge.py --emit pairwise             # 生成 120 份（含 A/B 互换）
    python src/llm_judge.py --status                    # 看取回进度
    python src/llm_judge.py --parse --judge-model gemini-2.5-pro

依赖：仅标准库。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import console  # noqa: E402
import rule_check as RC  # noqa: E402
from blind_shuffle import CORE_PANEL, EXT_PANEL, load_score_panel  # noqa: E402

console.setup()

ROOT = Path(__file__).resolve().parent.parent
PROMPTS = ROOT / "prompts"
JUDGE_ROOT = ROOT / "data" / "judge"
INBOX = JUDGE_ROOT / "inbox"
OUTBOX = JUDGE_ROOT / "outbox"
JUDGE_KEY = JUDGE_ROOT / "judge_key.jsonl"
JUDGE_SCORES = ROOT / "data" / "scores" / "judge"
QUESTIONS = ROOT / "data" / "questions.jsonl"

PANEL = CORE_PANEL + EXT_PANEL          # 被评面板；judge 不得来自其中
# pairwise 在人工打分面板内做全对组合；面板来自 score_panel.json（--score-models 可覆盖）
PAIR_PANEL = sorted(load_score_panel())
_sp = PAIR_PANEL
PAIRS = [(_sp[i], _sp[j]) for i in range(len(_sp)) for j in range(i + 1, len(_sp))]

# 面板外校验用：厂商级 token。
# 只做模型名精确匹配是不够的——实测 `--judge-model DeepSeek-V3.2` 能绕过
# `== "deepseek"` 的检查。同源模型（哪怕不同版本）共享输出风格，
# self-preference bias 依然存在，因此按厂商一并拦掉。
VENDOR_TOKENS: dict[str, list[str]] = {
    "deepseek": ["deepseek", "deepseekv", "深度求索"],
    "gpt5": ["gpt", "openai", "chatgpt", "o1", "o3", "o4"],
    "claude": ["claude", "anthropic"],
    "qwen3": ["qwen", "tongyi", "通义", "阿里"],
    "kimi": ["kimi", "moonshot", "月之暗面"],
}

# 解析质量门槛。低于此值说明 judge 没在按要求输出 JSON，结果不可用。
PARSE_SUCCESS_FLOOR = 0.90


# =====================================================================
# 模板与填充
# =====================================================================
def load_template(kind: str) -> str:
    """从 prompts/judge_{kind}.md 里取出 ```text 围栏中的提示词正文。"""
    p = PROMPTS / f"judge_{kind}.md"
    if not p.exists():
        sys.exit(f"[ERROR] 找不到提示词模板：{p}")
    m = re.search(r"```text\n(.*?)\n```", p.read_text(encoding="utf-8"), re.S)
    if not m:
        sys.exit(f"[ERROR] {p.name} 里没有 ```text 围栏的提示词正文")
    return m.group(1)


def _fmt_chain(q: dict) -> str:
    return "\n".join(f"{i}. {s}" for i, s in enumerate(q.get("required_chain") or [], 1))


def _fmt_key_points(q: dict) -> str:
    out = []
    for k in q.get("key_points") or []:
        flags = f"weight={k.get('weight')}" + ("，must_have=true" if k.get("must_have") else "")
        out.append(f"- [{k.get('kp_id')}]（{flags}）{k.get('text')}")
    return "\n".join(out)


def _fmt_red_flags(q: dict) -> str:
    out = []
    for r in q.get("red_flags") or []:
        out.append(f"- [{r.get('rf_id')}]（severity={r.get('severity')}，"
                   f"expected_modality={r.get('expected_modality')}，"
                   f"hint_polarity={r.get('hint_polarity', 'mention')}）{r.get('text')}")
    return "\n".join(out)


def _fmt_list(xs: list) -> str:
    return "\n".join(f"- {x}" for x in xs)


def slot(model: str) -> str:
    """模型名 -> 6 位稳定哈希槽号。inbox 文件名只暴露这个。"""
    return hashlib.sha256(model.encode("utf-8")).hexdigest()[:6]


def fill(tpl: str, q: dict, **extra: str) -> str:
    mapping = {
        "QID": q["qid"],
        "STEM": q.get("stem", ""),
        "QUESTION": q.get("question", ""),
        "REQUIRED_CHAIN": _fmt_chain(q),
        "KEY_POINTS": _fmt_key_points(q),
        "RED_FLAGS": _fmt_red_flags(q),
        "ACCEPTABLE_DIVERGENCE": q.get("acceptable_divergence", ""),
        "UNACCEPTABLE": _fmt_list(q.get("unacceptable") or []),
    }
    mapping.update(extra)
    out = tpl
    for k, v in mapping.items():
        out = out.replace("{{" + k + "}}", str(v))
    left = sorted(set(re.findall(r"\{\{(\w+)\}\}", out)))
    if left:
        sys.exit(f"[ERROR] 模板占位符未填完：{left}")
    return out


def load_questions() -> dict[str, dict]:
    out = {}
    for line in QUESTIONS.read_text(encoding="utf-8").splitlines():
        if line.strip():
            q = json.loads(line)
            out[q["qid"]] = q
    return out


# =====================================================================
# 生成待粘贴输入
# =====================================================================
def emit(kind: str, source: str, only: set[str] | None) -> int:
    qs = load_questions()
    recs, note = RC.load_responses(source, only)
    tpl = load_template(kind)
    # 只清理同类型旧文件：pointwise 与 pairwise 的 inbox 必须能共存，
    # 否则先 emit pointwise 再 emit pairwise 会把前 100 份输入一起删掉。
    prefix = "PW_" if kind == "pointwise" else "PR_"
    INBOX.mkdir(parents=True, exist_ok=True)
    for old in INBOX.glob(f"{prefix}*"):
        old.unlink()
    OUTBOX.mkdir(parents=True, exist_ok=True)
    JUDGE_SCORES.mkdir(parents=True, exist_ok=True)

    keys: list[dict] = []
    n = 0
    if kind == "pointwise":
        for r in sorted(recs, key=lambda x: (x["qid"], x["model"])):
            q = qs.get(r["qid"])
            if not q:
                continue
            name = f"PW_{r['qid']}_{slot(r['model'])}"
            body = fill(tpl, q, RESPONSE=r["text"])
            (INBOX / f"{name}.md").write_text(body, encoding="utf-8")
            keys.append({"task_id": name, "kind": "pointwise", "qid": r["qid"],
                         "model": r["model"], "slot": slot(r["model"]),
                         "char_count": len(r["text"]), "is_mock": bool(r.get("is_mock"))})
            n += 1
    else:
        by_qm = {(r["qid"], r["model"]): r for r in recs}
        for qid in sorted({r["qid"] for r in recs}):
            q = qs.get(qid)
            if not q:
                continue
            for pi, (m1, m2) in enumerate(PAIRS, 1):
                a, b = by_qm.get((qid, m1)), by_qm.get((qid, m2))
                if not a or not b:
                    print(f"{console.WARN} {qid} 缺 {m1 if not a else m2} 的回答，跳过该对",
                          file=sys.stderr)
                    continue
                for order, (x, y) in enumerate([(a, b), (b, a)], 1):
                    name = f"PR_{qid}_p{pi}_ord{order}"
                    body = fill(tpl, q, RESPONSE_A=x["text"], RESPONSE_B=y["text"])
                    (INBOX / f"{name}.md").write_text(body, encoding="utf-8")
                    keys.append({"task_id": name, "kind": "pairwise", "qid": qid,
                                 "pair_id": f"p{pi}", "order": order,
                                 "model_a": x["model"], "model_b": y["model"],
                                 "is_mock": bool(x.get("is_mock") or y.get("is_mock"))})
                    n += 1

    # 槽号对照表按 kind 合并写入，避免后一次 emit 覆盖前一次的记录
    old_keys = []
    if JUDGE_KEY.exists():
        old_keys = [json.loads(l) for l in JUDGE_KEY.read_text(encoding="utf-8").splitlines()
                    if l.strip() and json.loads(l).get("kind") != kind]
    all_keys = old_keys + keys
    JUDGE_KEY.write_text(
        "\n".join(json.dumps(k, ensure_ascii=False) for k in all_keys) + "\n", encoding="utf-8")
    print(f"[emit:{kind}] 生成 {n} 份待粘贴输入 -> {INBOX.relative_to(ROOT)}/")
    print(f"            数据源：{note}")
    print(f"            槽号对照表 -> {JUDGE_KEY.relative_to(ROOT)}")
    print(f"            {console.WARN} inbox 文件名不含模型名；对照表在盲测期间不得查看。")
    print(f"\n下一步：逐份复制 inbox/*.md 全文 -> 粘贴给 judge 模型 -> "
          f"把回复原样存成 {OUTBOX.relative_to(ROOT)}/同名.json")
    if kind == "pointwise":
        print(f"      应有 {n} 份（5 模型 × {len({k['qid'] for k in keys})} 题）")
    else:
        print(f"      应有 {n} 份（{len(PAIRS)} 对 × {len({k['qid'] for k in keys})} 题 × 2 顺序）")
    return n


# =====================================================================
# JSON 解析（容错）
# =====================================================================
_FENCE = re.compile(r"```(?:json|JSON)?\s*\n?(.*?)```", re.S)


def _balanced(text: str, start: int) -> str | None:
    """从 start 处的 '{' 起做括号匹配扫描，返回完整候选串（尊重字符串与转义）。"""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def extract_json(text: str) -> tuple[dict | None, str]:
    """从 judge 的原始回复里取出 JSON 对象。

    容错三档，按顺序尝试：① 代码围栏内容 ② 首个平衡花括号块 ③ 去掉尾逗号后重试。
    返回 (对象或 None, 失败原因)。
    """
    t = text.strip()
    cands: list[str] = []
    for m in _FENCE.finditer(t):
        cands.append(m.group(1).strip())
    b = t.find("{")
    if b >= 0:
        bal = _balanced(t, b)
        if bal:
            cands.append(bal)
    cands.append(t)
    for c in cands:
        for attempt in (c, re.sub(r",(\s*[}\]])", r"\1", c)):
            try:
                obj = json.loads(attempt)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                return obj, ""
            return None, f"解析出的不是 JSON 对象而是 {type(obj).__name__}"
    return None, "未找到可解析的 JSON 对象"


# =====================================================================
# 证据校验
# =====================================================================
def _squash(s: str) -> str:
    return re.sub(r"\s+", "", str(s))


def check_quote(quote: str, *texts: str) -> bool:
    """quote 必须是某份回答的逐字子串（忽略空白差异）。"""
    if not isinstance(quote, str) or not quote.strip():
        return False
    if quote.strip().lower() in ("null", "none", "n/a", "无"):
        return False
    q = _squash(quote)
    return any(q in _squash(t) for t in texts if t)


def audit_pointwise(obj: dict, resp_text: str) -> dict:
    """丢弃无证据的判定，返回审计结果。

    处置原则：丢的是**判定条目**，不是分数。分数照收，但如果某个维度的负面判定
    全被丢掉、而分数又低于 3，就把该行标 `unsupported`——一个没有证据支撑的低分
    和没有低分一样不可用。
    """
    dropped = kept = 0
    unsupported: list[str] = []

    def scrub(items: list, texts: tuple[str, ...]) -> list:
        nonlocal dropped, kept
        out = []
        for it in items or []:
            if not isinstance(it, dict):
                dropped += 1
                continue
            if check_quote(it.get("quote"), *texts):
                kept += 1
                out.append(it)
            else:
                dropped += 1
        return out

    for dim, key in (("D1", "errors"), ("D2", "inversions"),
                     ("D4", "overhedges")):
        d = obj.get(dim)
        if isinstance(d, dict) and key in d:
            before = len(d.get(key) or [])
            d[key] = scrub(d.get(key), (resp_text,))
            if before and not d[key]:
                sc = d.get("score")
                if isinstance(sc, int) and sc < 3:
                    unsupported.append(dim)
    d3 = obj.get("D3")
    if isinstance(d3, dict):
        # miss 的 quote 允许为 null（回答里根本没有相关内容，无从引用）
        for it in d3.get("misses") or []:
            if isinstance(it, dict) and it.get("quote") in (None, "null", ""):
                kept += 1
            elif isinstance(it, dict) and check_quote(it.get("quote"), resp_text):
                kept += 1
            else:
                dropped += 1
        d3["downgrades"] = scrub(d3.get("downgrades"), (resp_text,))
        if d3.get("modality_downgraded") and not d3.get("downgrades"):
            unsupported.append("D3")
    d5 = obj.get("D5")
    if isinstance(d5, dict):
        rs = d5.get("redundant_segment")
        if rs not in (None, "", "null") and not check_quote(rs, resp_text):
            dropped += 1
            d5["redundant_segment"] = None
            d5["redundancy_ratio"] = None
        elif rs:
            kept += 1
    d4 = obj.get("D4")
    if isinstance(d4, dict):
        d4["exemptions"] = [e for e in (d4.get("exemptions") or [])
                            if check_quote(e, resp_text)]
    return {"quotes_kept": kept, "quotes_dropped": dropped, "unsupported_dims": unsupported}


def audit_pairwise(obj: dict, text_a: str, text_b: str) -> dict:
    """decisive_dimensions 里的每个维度，A 和 B 都必须各有一条有效 quote。"""
    dropped = kept = 0
    ev = [e for e in (obj.get("evidence") or []) if isinstance(e, dict)]
    good = []
    for e in ev:
        about = e.get("about")
        txt = text_a if about == "A" else text_b if about == "B" else None
        if txt is not None and check_quote(e.get("quote"), txt):
            kept += 1
            good.append(e)
        else:
            dropped += 1
    obj["evidence"] = good
    per_dim = {e["dimension"] for e in good}
    have_both = {d for d in per_dim
                 if any(e["dimension"] == d and e["about"] == "A" for e in good)
                 and any(e["dimension"] == d and e["about"] == "B" for e in good)}
    decisive = [d for d in (obj.get("decisive_dimensions") or []) if isinstance(d, str)]
    unsupported = [d for d in decisive if d not in have_both]
    return {"quotes_kept": kept, "quotes_dropped": dropped,
            "unsupported_dims": unsupported, "decisive_unsupported": unsupported}


# =====================================================================
# 解析回填
# =====================================================================
REQUIRED_PW = ["qid", "D1", "D2", "D3", "D4", "D5", "D6"]
DIMS = ["D1", "D2", "D3", "D4", "D5", "D6"]


def _as_int(x, lo=0, hi=4):
    try:
        v = int(x)
    except (TypeError, ValueError):
        return None
    return v if lo <= v <= hi else None


def check_judge_outside_panel(judge_model: str) -> None:
    """judge 必须来自被评面板之外，且不能与面板模型同厂商。

    同源 judge 会系统性偏爱与自己风格相似的输出（self-preference bias），
    这会让"judge 与人工一致"变成一个自我实现的结论。
    """
    name = judge_model.strip()
    if not name:
        sys.exit("[ERROR] --judge-model 不能为空：judge 型号必须写进结果，否则无法复现")
    flat = re.sub(r"[\s\-_.]+", "", name).lower()
    hits: list[str] = []
    for m in PANEL:
        if m.lower() in flat:
            hits.append(f"型号名含面板成员 `{m}`")
        for tok in VENDOR_TOKENS.get(m, []):
            if tok in flat or tok in name.lower():
                hits.append(f"厂商 token `{tok}` 对应面板成员 `{m}`")
    if hits:
        sys.exit(f"[ERROR] judge 模型 `{judge_model}` 与被评面板同源：{sorted(set(hits))}\n"
                 f"        被评面板 = {PANEL}\n"
                 f"        同源 judge 会系统性偏爱与自己风格相似的输出（self-preference bias），\n"
                 f"        使「judge 与人工一致」变成自我实现的结论。\n"
                 f"        请换面板外的第 6 个模型（Gemini / GLM / 豆包任一），\n"
                 f"        见 prompts/judge_pointwise.md 开头的说明。")


def _outbox_file(task_id: str) -> Path | None:
    for ext in (".json", ".txt", ".md"):
        p = OUTBOX / f"{task_id}{ext}"
        if p.exists():
            return p
    return None


def parse(judge_model: str, source: str) -> int:
    check_judge_outside_panel(judge_model)
    if not JUDGE_KEY.exists():
        sys.exit("[ERROR] 找不到 data/judge/judge_key.jsonl，请先运行 --emit")
    keys = [json.loads(l) for l in JUDGE_KEY.read_text(encoding="utf-8").splitlines() if l.strip()]
    qs = load_questions()
    recs, note = RC.load_responses(source, None)
    by_qm = {(r["qid"], r["model"]): r["text"] for r in recs}

    JUDGE_SCORES.mkdir(parents=True, exist_ok=True)
    pw_out: list[dict] = []
    pr_out: list[dict] = []
    stats = Counter()
    problems: list[str] = []
    dropped_total = Counter()
    unsupported_rows: list[str] = []

    for k in keys:
        tid = k["task_id"]
        f = _outbox_file(tid)
        if f is None:
            stats["missing"] += 1
            continue
        stats["present"] += 1
        raw = f.read_text(encoding="utf-8")
        obj, err = extract_json(raw)
        if obj is None:
            stats["parse_failed"] += 1
            problems.append(f"{tid}: JSON 解析失败（{err}）")
            continue
        stats["parsed"] += 1

        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if k["kind"] == "pointwise":
            missing = [c for c in REQUIRED_PW if c not in obj]
            if missing:
                stats["schema_failed"] += 1
                problems.append(f"{tid}: 缺字段 {missing}")
                continue
            resp = by_qm.get((k["qid"], k["model"]), "")
            if not resp:
                stats["no_response"] += 1
                problems.append(f"{tid}: 找不到对应回答（qid={k['qid']} model={k['model']}）")
                continue
            audit = audit_pointwise(obj, resp)
            dropped_total["pointwise"] += audit["quotes_dropped"]
            if audit["unsupported_dims"]:
                unsupported_rows.append(f"{tid}: 维度 {audit['unsupported_dims']} 的负面判定全部无有效证据")
            row = {
                "task_id": tid, "qid": k["qid"], "model": k["model"],
                "judge_model": judge_model, "kind": "pointwise",
                "rf_hits": (obj.get("D3") or {}).get("rf_hits"),
                "off_list_observation": obj.get("off_list_observation", ""),
                "judge_note": obj.get("judge_note", ""),
                "quotes_kept": audit["quotes_kept"], "quotes_dropped": audit["quotes_dropped"],
                "unsupported_dims": audit["unsupported_dims"],
                "raw": obj, "parsed_at": ts,
            }
            for d in DIMS:
                dd = obj.get(d) or {}
                row[f"{d}_score"] = _as_int(dd.get("score"))
                if row[f"{d}_score"] is None:
                    problems.append(f"{tid}: {d}.score 缺失或越界（{dd.get('score')!r}）")
            for f_ in ("n_fatal", "n_core", "n_minor", "n_vague"):
                row[f_] = (obj.get("D1") or {}).get(f_)
            row["n_gap"] = (obj.get("D2") or {}).get("n_gap")
            row["n_inversion"] = (obj.get("D2") or {}).get("n_inversion")
            row["conclusion_consistent"] = (obj.get("D2") or {}).get("conclusion_consistent")
            row["hit_rate"] = (obj.get("D3") or {}).get("hit_rate")
            row["critical_miss"] = (obj.get("D3") or {}).get("critical_miss")
            row["modality_downgraded"] = (obj.get("D3") or {}).get("modality_downgraded")
            for f_ in ("n_exclusive", "n_uncertain", "n_overhedge", "both_paths", "gap_declared"):
                row[f_] = (obj.get("D4") or {}).get(f_)
            row["D4_raw"] = _as_int((obj.get("D4") or {}).get("score_raw"))
            row["off_target"] = (obj.get("D5") or {}).get("off_target")
            row["conclusion_in_first_3"] = (obj.get("D5") or {}).get("conclusion_in_first_3")
            row["redundancy_ratio"] = (obj.get("D5") or {}).get("redundancy_ratio")
            row["D6_raw"] = _as_int((obj.get("D6") or {}).get("score_raw"))
            row["veto_applied"] = (obj.get("D6") or {}).get("veto_applied", "none")
            pw_out.append(row)
        else:
            if "overall" not in obj or "per_dimension" not in obj:
                stats["schema_failed"] += 1
                problems.append(f"{tid}: 缺 overall 或 per_dimension")
                continue
            ta = by_qm.get((k["qid"], k["model_a"]), "")
            tb = by_qm.get((k["qid"], k["model_b"]), "")
            audit = audit_pairwise(obj, ta, tb)
            dropped_total["pairwise"] += audit["quotes_dropped"]
            if audit["decisive_unsupported"]:
                unsupported_rows.append(
                    f"{tid}: 决定性维度 {audit['decisive_unsupported']} 缺 A/B 双侧证据")
            overall = str(obj.get("overall")).strip().upper()
            if overall not in ("A", "B", "TIE"):
                stats["schema_failed"] += 1
                problems.append(f"{tid}: overall={obj.get('overall')!r} 不是 A/B/tie")
                continue
            # 把 A/B 翻译成模型名，ord2 里 A/B 已互换
            win = {"A": k["model_a"], "B": k["model_b"], "TIE": "tie"}[overall]
            pr_out.append({
                "task_id": tid, "qid": k["qid"], "pair_id": k["pair_id"], "order": k["order"],
                "model_a": k["model_a"], "model_b": k["model_b"],
                "judge_model": judge_model, "kind": "pairwise",
                "overall_ab": overall, "winner_model": win,
                "per_dimension": obj.get("per_dimension"),
                "veto_triggered": obj.get("veto_triggered"),
                "decisive_dimensions": obj.get("decisive_dimensions"),
                "margin": obj.get("margin"),
                "quotes_kept": audit["quotes_kept"], "quotes_dropped": audit["quotes_dropped"],
                "unsupported_dims": audit["unsupported_dims"],
                "raw": obj, "parsed_at": ts,
            })

    # 只在确实回收到该类型结果时才覆盖对应文件，
    # 否则"先跑完 pointwise 就 --parse"会把已有的 judge_pairwise.jsonl 清空。
    saw = {k: sum(1 for kk in keys if kk["kind"] == k and _outbox_file(kk["task_id"]))
           for k in ("pointwise", "pairwise")}
    if saw["pointwise"]:
        (JUDGE_SCORES / "judge_pointwise.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in pw_out) + "\n", encoding="utf-8")
    if saw["pairwise"]:
        (JUDGE_SCORES / "judge_pairwise.jsonl").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in pr_out) + "\n", encoding="utf-8")

    # ---- 位置偏置
    pos = _position_bias(pr_out)

    # ---- 报告
    print(f"judge 模型：{judge_model}（面板外校验通过）")
    print(f"数据源：{note}")
    print(f"\n=== 取回与解析 ===")
    print(f"  应取回 {len(keys)} 份　已放入 outbox {stats['present']} 份　缺 {stats['missing']} 份")
    parsed = stats["parsed"]
    rate = parsed / stats["present"] if stats["present"] else 0.0
    print(f"  JSON 解析成功 {parsed} 份　失败 {stats['parse_failed']} 份　"
          f"结构不合规 {stats['schema_failed']} 份")
    print(f"  解析成功率 {rate:.1%}（验收门槛 {PARSE_SUCCESS_FLOOR:.0%}）"
          f"　{'PASS' if rate >= PARSE_SUCCESS_FLOOR and stats['present'] else 'FAIL'}")
    if stats["missing"]:
        print(f"  {console.WARN} 还缺 {stats['missing']} 份，跑 --status 看清单")
    print(f"\n=== 证据纪律 ===")
    print(f"  被丢弃的无证据判定：pointwise {dropped_total['pointwise']} 条、"
          f"pairwise {dropped_total['pairwise']} 条")
    print(f"  无证据支撑的维度/判定：{len(unsupported_rows)} 处")
    for u in unsupported_rows[:10]:
        print(f"    - {u}")
    if unsupported_rows:
        print(f"  {console.WARN} 这些行的分数照收，但已标 unsupported；"
              f"进统计前必须人工复核（见 stats.py 的 --drop-unsupported）")
    if pos:
        print(f"\n=== 位置偏置（pairwise）===")
        for line in pos:
            print(f"  {line}")
    if problems:
        print(f"\n=== 问题清单（前 20 条，共 {len(problems)}）===")
        for p in problems[:20]:
            print(f"  - {p}")
    print(f"\n产物 -> {JUDGE_SCORES.relative_to(ROOT)}/judge_pointwise.jsonl（{len(pw_out)} 行）、"
          f"judge_pairwise.jsonl（{len(pr_out)} 行）")
    print(f"{console.WARN} 这两个文件含明文模型名，盲测期间评分者不得打开。")
    ok = rate >= PARSE_SUCCESS_FLOOR and stats["present"] > 0
    if not ok:
        print(f"\n{console.FAIL} 解析成功率未达门槛。检查 judge 是否被要求「只输出 JSON、不要围栏」，"
              f"以及回复是否被界面截断。")
        return 1
    print(f"\n{console.OK} judge 回填完成。")
    return 0


def _position_bias(rows: list[dict]) -> list[str]:
    """ord1 与 ord2 是否同向。反向 => 记 tie，并计入位置敏感对数。"""
    by_pair: dict[tuple, dict[int, dict]] = defaultdict(dict)
    for r in rows:
        by_pair[(r["qid"], r["pair_id"])][r["order"]] = r
    same = rev = tie = incomplete = 0
    reversals: list[str] = []
    for (qid, pid), d in sorted(by_pair.items()):
        if 1 not in d or 2 not in d:
            incomplete += 1
            continue
        w1, w2 = d[1]["winner_model"], d[2]["winner_model"]
        if w1 == "tie" or w2 == "tie":
            tie += 1
        elif w1 == w2:
            same += 1
        else:
            rev += 1
            reversals.append(f"{qid}/{pid}: ord1={w1} ord2={w2}")
    total = same + rev + tie
    if not total:
        return ["（尚无完整的成对结果，无法评估位置偏置）"]
    out = [f"完整题对 {total} 组（另有 {incomplete} 组只跑了一个顺序）",
           f"同向 {same}　反向（位置敏感）{rev}　至少一次 tie {tie}",
           f"位置敏感率 {rev / total:.1%}"]
    if rev:
        out.append(f"反向题对：{reversals[:8]}{' …' if len(reversals) > 8 else ''}")
        out.append(f"{console.WARN} 反向题对一律记 tie 后才进胜率；"
                   f"位置敏感率高说明 judge 的 pairwise 结论不能当独立证据，README 须降级表述")
    return out


def status() -> int:
    if not JUDGE_KEY.exists():
        print("尚未生成 inbox。先运行：python src/llm_judge.py --emit pointwise")
        return 0
    keys = [json.loads(l) for l in JUDGE_KEY.read_text(encoding="utf-8").splitlines() if l.strip()]
    todo = [k["task_id"] for k in keys if _outbox_file(k["task_id"]) is None]
    print(f"inbox 共 {len(keys)} 份，已回收 {len(keys) - len(todo)} 份，待回收 {len(todo)} 份")
    by_kind = Counter(k["kind"] for k in keys)
    print(f"  按类型：{dict(by_kind)}")
    if todo:
        print(f"  待回收（前 15）：{todo[:15]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="LLM-as-judge 取回流水线")
    ap.add_argument("--emit", choices=["pointwise", "pairwise"], help="生成待粘贴输入")
    ap.add_argument("--parse", action="store_true", help="解析 outbox 并回填")
    ap.add_argument("--status", action="store_true", help="看取回进度")
    ap.add_argument("--judge-model", default="", help="judge 模型名（不得属于被评面板）")
    ap.add_argument("--source", default="auto", choices=["auto", "real", "demo", "mock"])
    ap.add_argument("--only", default="", help="逗号分隔的 qid 子集")
    args = ap.parse_args(argv)

    only = {x.strip() for x in args.only.split(",") if x.strip()} or None
    if args.emit:
        emit(args.emit, args.source, only)
        return 0
    if args.status:
        return status()
    if args.parse:
        if not args.judge_model:
            sys.exit("[ERROR] --parse 必须给 --judge-model（要写进结果并做面板外校验）")
        return parse(args.judge_model, args.source)
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
