#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
blind_shuffle.py — 匿名化、随机化、block 分配、打分表模板生成

它解决三个具体的偏差问题（对应 docs/rubric.md §1 呈现顺序纪律、docs/_stage0_review.md §3 第 1/6 条）：

  1. **匿名化**：模型名 → R### 编号。评分者在看不到模型身份的情况下打分，
     否则"DeepSeek 应该更强"这类先验会直接污染 D1–D6。
  2. **绝对独立评分**：同一 qid 的 3 份回答被强制分到**不同 block**，
     评分者永远不会在同一时段看到同一道题的两个模型答案 → 无法退化成主观相对比较。
  3. **可复现的随机化**：固定 seed，manifest 记录全部参数。任何人重跑得到同一套编号与顺序。

pass2（重测）会用**不同的 seed 和不同的编号体系**重新匿名化，且不会把 pass1 的分数
呈现给评分者——否则重测一致性会被记忆效应系统性高估。

用法
----
    # 0) 还没有真实回答时，生成标注为 MOCK 的假数据把流程跑通
    python src/blind_shuffle.py --make-demo

    # 1) 匿名化 + 分 block（核心面板 3 模型进人工打分，扩展面板只进自动评测）
    python src/blind_shuffle.py

    # 2) 生成 8 题重测包（须在 pass1 完成 ≥72 小时后使用）
    python src/blind_shuffle.py --pass2 --retest-questions 8

    # 3) 打分结束后揭盲
    python src/blind_shuffle.py --unblind

依赖：仅标准库。
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import shutil
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

try:
    import console
except ImportError:  # 允许从仓库根目录以 -m 方式运行
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import console

console.setup()

ROOT = Path(__file__).resolve().parent.parent
RAW_JSONL = ROOT / "data" / "responses" / "raw" / "responses.jsonl"
DEMO_JSONL = ROOT / "data" / "responses" / "raw" / "demo_responses.jsonl"
RAW_DIR = ROOT / "data" / "responses" / "raw"
BLINDED_DIR = ROOT / "data" / "responses" / "blinded"
KEY_FILE = ROOT / "data" / "responses" / "key.jsonl"
MANIFEST = ROOT / "data" / "responses" / "manifest.json"
SCORES_DIR = ROOT / "data" / "scores"
QUESTIONS = ROOT / "data" / "questions.jsonl"

DEFAULT_SEED = 20261001

# 核心面板：人工 + 自动全量。扩展面板：仅规则层 + LLM-judge。
CORE_PANEL = ["deepseek", "gpt5", "claude"]
EXT_PANEL = ["qwen3", "kimi"]

# 人工打分面板（CORE 的子集/重排）。默认 = CORE_PANEL；可用 --score-models 覆盖。
# 选择落盘到 score_panel.json：blind/emit/pass2/llm_judge 读同一份，避免各处口径漂移。
SCORE_PANEL_FILE = ROOT / "data" / "responses" / "score_panel.json"


def load_score_panel() -> list[str]:
    if SCORE_PANEL_FILE.exists():
        d = json.loads(SCORE_PANEL_FILE.read_text(encoding="utf-8"))
        p = d.get("score_models")
        if p:
            return list(p)
    return list(CORE_PANEL)


def set_score_panel(models: list[str], reason: str = "") -> list[str]:
    pool = CORE_PANEL + EXT_PANEL
    bad = [m for m in models if m not in pool]
    if bad:
        sys.exit(f"[ERROR] --score-models 含未知模型 {bad}；可选 {pool}")
    if len(models) < 2:
        sys.exit("[ERROR] 人工打分面板至少 2 个模型，否则 pairwise 无意义")
    if len(set(models)) != len(models):
        sys.exit("[ERROR] --score-models 有重复模型")
    SCORE_PANEL_FILE.parent.mkdir(parents=True, exist_ok=True)
    SCORE_PANEL_FILE.write_text(
        json.dumps({"score_models": models, "reason": reason,
                    "set_at": datetime.now().astimezone().isoformat(timespec="minutes")},
                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return models

# 人工打分表字段（顺序即列顺序）——见 docs/rubric.md §3
ID_COLS = ["rater_id", "pass_id", "block_id", "position", "qid", "response_id", "ts"]
D1_COLS = ["n_fatal", "n_core", "n_minor", "n_vague", "D1"]
D2_COLS = ["n_gap", "n_inversion", "conclusion_consistent", "D2"]
D3_COLS = ["rf_hits", "hit_rate", "critical_miss", "modality_downgraded", "D3"]
D4_COLS = ["n_exclusive", "n_uncertain", "both_paths", "gap_declared",
           "n_overhedge", "exemption_note", "D4_raw", "D4"]
D5_COLS = ["off_target", "conclusion_in_first_3", "redundancy_ratio", "word_count", "D5"]
D6_COLS = ["D6_raw", "unacceptable_hit", "veto_applied", "D6"]
QC_COLS = ["off_list_observation", "minutes_spent", "rater_note"]
SCORE_COLS = ID_COLS + D1_COLS + D2_COLS + D3_COLS + D4_COLS + D5_COLS + D6_COLS + QC_COLS


# ------------------------------------------------------------------ 载入
def load_questions() -> dict[str, dict]:
    if not QUESTIONS.exists():
        return {}
    out = {}
    for line in QUESTIONS.read_text(encoding="utf-8").splitlines():
        if line.strip():
            q = json.loads(line)
            out[q["qid"]] = q
    return out


def load_raw() -> list[dict]:
    """优先读 responses.jsonl（paste_recorder.html 的导出格式）；
    不存在时回退到 demo_responses.jsonl；再回退到 raw/{model}--{qid}.md 散文件。

    硬性防线：responses.jsonl 里若出现 is_mock 或 mock_* 模型，直接终止。
    mock 数据混进正式盲测是最容易犯、也最难被发现的错误。
    """
    recs: list[dict] = []
    if RAW_JSONL.exists():
        for ln, line in enumerate(RAW_JSONL.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError as e:
                sys.exit(f"[ERROR] {RAW_JSONL.name} 第 {ln} 行 JSON 解析失败：{e}")
            for f in ("qid", "model", "text"):
                if not r.get(f):
                    sys.exit(f"[ERROR] {RAW_JSONL.name} 第 {ln} 行缺字段 `{f}`")
            if r.get("is_mock") or str(r.get("model", "")).startswith("mock"):
                sys.exit(f"[ERROR] {RAW_JSONL.name} 第 {ln} 行是 mock 数据（model={r.get('model')}）。"
                         f" mock 必须放在 mock_responses.jsonl，不得混入正式回答文件。")
            recs.append(r)
        return recs

    if DEMO_JSONL.exists():
        print(f"{console.WARN} 未找到正式回答，使用 DEMO 假数据：{DEMO_JSONL.name}"
              f"（任何基于它的结果都无意义，仅用于跑通流水线）", file=sys.stderr)
        for line in DEMO_JSONL.read_text(encoding="utf-8").splitlines():
            if line.strip():
                recs.append(json.loads(line))
        return recs

    if RAW_DIR.exists():
        for p in sorted(RAW_DIR.glob("*--*.md")):
            model, qid = p.stem.split("--", 1)
            recs.append({"qid": qid, "model": model, "text": p.read_text(encoding="utf-8")})
    return recs


def make_demo(n_questions: int = 20) -> None:
    """生成明确标注 MOCK 的假回答到 demo_responses.jsonl，用来在真实取回前跑通全流程。
    刻意与 responses.jsonl 分开存放，避免误当正式数据。"""
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    qs = load_questions()
    qids = sorted(qs)[:n_questions] or [f"SIRT-{i:03d}" for i in range(1, n_questions + 1)]
    rng = random.Random(DEFAULT_SEED)
    recs = []
    for qid in qids:
        for model in CORE_PANEL + EXT_PANEL:
            n = rng.randint(180, 520)
            body = (
                f"[MOCK RESPONSE — 非真实模型输出，仅用于跑通流水线，正式数据到位后必须删除]\n"
                f"model={model} qid={qid}\n\n"
                f"结论：本例的处理建议为示例占位文本。{'占位内容。' * (n // 8)}\n\n"
                f"理由：{'示例推理步骤占位。' * 6}\n"
            )
            recs.append({
                "qid": qid, "model": model, "text": body,
                "retrieved_at": "MOCK", "model_version": "MOCK", "mode": "MOCK",
                "is_mock": True,
            })
    DEMO_JSONL.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n", encoding="utf-8"
    )
    print(f"[demo] 写入 {len(recs)} 份 MOCK 回答 -> {DEMO_JSONL.relative_to(ROOT)}")
    print("[demo] 这些不是真实模型输出。load_raw 会在使用时打印显著警告。")
    if RAW_JSONL.exists():
        print(f"{console.WARN} 检测到 responses.jsonl 已存在——它会优先于 demo 数据被读取。")


# ------------------------------------------------------------------ block 分配
def assign_blocks(items: list[dict], n_blocks: int, seed: int) -> None:
    """约束：同一 qid 的回答不得进同一 block；各 block 规模尽量均衡。

    做法：固定 seed 打乱后贪心装入"当前人数最少且不含该 qid"的 block。
    确定性 → 任何人重跑得到同一套分配。
    """
    rng = random.Random(seed)
    order = list(range(len(items)))
    rng.shuffle(order)

    members: dict[int, list[int]] = defaultdict(list)
    qids_in_block: dict[int, set[str]] = defaultdict(set)

    for idx in order:
        qid = items[idx]["qid"]
        cands = [b for b in range(n_blocks) if qid not in qids_in_block[b]]
        if not cands:  # block 数 < 每 qid 回答数，理论上不该发生
            raise SystemExit(f"[ERROR] block 数 {n_blocks} 不足以隔离 qid={qid} 的多份回答")
        b = min(cands, key=lambda x: (len(members[x]), x))
        members[b].append(idx)
        qids_in_block[b].add(qid)
        items[idx]["block_id"] = b

    for b in range(n_blocks):
        rng.shuffle(members[b])
        for pos, idx in enumerate(members[b]):
            items[idx]["position"] = pos


# ------------------------------------------------------------------ 匿名化
def blind(pass_id: int, panel: list[str], seed: int, n_blocks: int,
          qids: list[str] | None = None, id_prefix: str = "R") -> dict:
    raw = load_raw()
    if not raw:
        sys.exit("[ERROR] data/responses/raw/ 为空。先手动取回回答，或用 --make-demo 跑通流程。")

    qs = load_questions()
    recs = [r for r in raw if r["model"] in panel and (qids is None or r["qid"] in qids)]
    if not recs:
        sys.exit(f"[ERROR] 面板 {panel} 在 raw 中没有匹配记录（qid 过滤={qids}）")

    # 完整性检查：每题每模型都该有一份
    have = Counter((r["qid"], r["model"]) for r in recs)
    expect_qids = sorted({r["qid"] for r in recs})
    missing = [(q, m) for q in expect_qids for m in panel if have[(q, m)] == 0]
    if missing:
        print(f"[WARN] 缺 {len(missing)} 份回答，例如 {missing[:5]}（流水线继续，但结果不完整）",
              file=sys.stderr)

    items = []
    for r in recs:
        items.append({
            "qid": r["qid"],
            "model": r["model"],
            "text": r["text"],
            "panel": "core" if r["model"] in load_score_panel() else "extension",
            "retrieved_at": r.get("retrieved_at", ""),
            "model_version": r.get("model_version", ""),
            "mode": r.get("mode", ""),
            "is_mock": bool(r.get("is_mock")),
            "char_count": len(r["text"]),
        })

    assign_blocks(items, n_blocks, seed)

    # 编号：按 (block, position) 排序后顺序分配，编号本身不泄露任何信息
    items.sort(key=lambda x: (x["block_id"], x["position"]))
    for i, it in enumerate(items, 1):
        it["response_id"] = f"{id_prefix}{i:04d}"

    # 落盘
    out_root = BLINDED_DIR / f"pass{pass_id}"
    if out_root.exists():
        shutil.rmtree(out_root)
    for it in items:
        d = out_root / f"block_{it['block_id']}"
        d.mkdir(parents=True, exist_ok=True)
        q = qs.get(it["qid"], {})
        header = (
            f"# {it['response_id']}　（block {it['block_id']} / 第 {it['position'] + 1} 份）\n\n"
            f"> 匿名回答。评分前不要查看 data/responses/key.jsonl。\n\n"
            f"---\n\n"
        )
        stem_block = ""
        if q:
            stem_block = (
                f"## 题干（qid={it['qid']}，仅供对照，不计分）\n\n"
                f"{q.get('stem', '')}\n\n"
                f"**提问**：{q.get('question', '')}\n\n"
                f"---\n\n"
            )
        (d / f"{it['position']:02d}_{it['response_id']}.md").write_text(
            header + stem_block + "## 待评回答\n\n" + it["text"] + "\n", encoding="utf-8"
        )

    key_path = KEY_FILE if pass_id == 1 else KEY_FILE.with_name(f"key_pass{pass_id}.jsonl")
    key_path.write_text(
        "\n".join(json.dumps({k: it[k] for k in
                              ("response_id", "qid", "model", "block_id", "position",
                               "panel", "retrieved_at", "model_version", "mode",
                               "is_mock", "char_count")}, ensure_ascii=False)
                  for it in items) + "\n",
        encoding="utf-8",
    )

    manifest = {
        "pass_id": pass_id,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seed": seed,
        "n_blocks": n_blocks,
        "id_prefix": id_prefix,
        "panel": panel,
        "restricted_qids": qids,
        "n_responses": len(items),
        "n_questions": len({it['qid'] for it in items}),
        "block_sizes": dict(Counter(it["block_id"] for it in items)),
        "model_counts": dict(Counter(it["model"] for it in items)),
        "any_mock": any(it["is_mock"] for it in items),
    }
    mpath = MANIFEST if pass_id == 1 else MANIFEST.with_name(f"manifest_pass{pass_id}.json")
    mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[pass{pass_id}] {len(items)} 份回答 → {out_root.relative_to(ROOT)}/")
    print(f"          block 规模：{manifest['block_sizes']}")
    print(f"          模型分布：{manifest['model_counts']}")
    print(f"          映射表：{key_path.relative_to(ROOT)}（打分完成前不得查看）")
    if manifest["any_mock"]:
        print(f"          {console.WARN} 含 MOCK 数据，不可用于正式结论")

    # 校验隔离约束
    viol = [b for b, s in _block_qid_conflicts(items).items() if s]
    if viol:
        sys.exit(f"[ERROR] block 隔离约束被违反：{viol}")
    print(f"          {console.OK} 同题不同 block 约束已校验通过")

    return manifest


def _block_qid_conflicts(items: list[dict]) -> dict[int, list[str]]:
    seen: dict[int, set] = defaultdict(set)
    bad: dict[int, list[str]] = defaultdict(list)
    for it in items:
        k = (it["block_id"], it["qid"])
        if k in seen[it["block_id"]]:
            bad[it["block_id"]].append(it["qid"])
        seen[it["block_id"]].add(k)
    return bad


# ------------------------------------------------------------------ 打分表模板
def emit_template(pass_id: int, panel: list[str], rater_id: str,
                  qids: list[str] | None = None, retest_hide_pass1: bool = False) -> Path:
    key_path = KEY_FILE if pass_id == 1 else KEY_FILE.with_name(f"key_pass{pass_id}.jsonl")
    if not key_path.exists():
        sys.exit(f"[ERROR] 找不到 {key_path.name}，请先运行匿名化")

    keys = [json.loads(l) for l in key_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    keys = [k for k in keys if k["model"] in panel and (qids is None or k["qid"] in qids)]
    keys.sort(key=lambda k: (k["block_id"], k["position"]))

    out = SCORES_DIR / f"human_pass{pass_id}_template.csv"
    SCORES_DIR.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8-sig", newline="") as f:  # BOM 便于 Excel 打开
        w = csv.DictWriter(f, fieldnames=SCORE_COLS)
        w.writeheader()
        for k in keys:
            row = {c: "" for c in SCORE_COLS}
            row.update({
                "rater_id": rater_id, "pass_id": pass_id,
                "block_id": k["block_id"], "position": k["position"],
                "qid": k["qid"], "response_id": k["response_id"],
                "ts": "", "conclusion_consistent": "", "both_paths": "",
                "gap_declared": "", "off_target": "", "conclusion_in_first_3": "",
                "critical_miss": "", "modality_downgraded": "",
            })
            w.writerow(row)
    print(f"[pass{pass_id}] 打分表模板 → {out.relative_to(ROOT)}（{len(keys)} 行）")
    if pass_id == 2 and retest_hide_pass1:
        print("          重测提示：请勿对照 pass1 的任何分数；本表编号体系与 pass1 不同。")
    return out


# ------------------------------------------------------------------ 揭盲
def unblind(pass_id: int) -> None:
    key_path = KEY_FILE if pass_id == 1 else KEY_FILE.with_name(f"key_pass{pass_id}.jsonl")
    src = SCORES_DIR / f"human_pass{pass_id}.csv"
    if not src.exists():
        src = SCORES_DIR / f"human_pass{pass_id}_template.csv"
    if not src.exists():
        sys.exit(f"[ERROR] 找不到打分表 human_pass{pass_id}.csv")

    keys = {json.loads(l)["response_id"]: json.loads(l)
            for l in key_path.read_text(encoding="utf-8").splitlines() if l.strip()}

    out = SCORES_DIR / f"human_pass{pass_id}_unblinded.csv"
    with src.open(encoding="utf-8-sig", newline="") as fi, \
         out.open("w", encoding="utf-8-sig", newline="") as fo:
        r = csv.DictReader(fi)
        cols = ["model", "panel", "char_count", "model_version", "is_mock"] + (r.fieldnames or [])
        w = csv.DictWriter(fo, fieldnames=cols)
        w.writeheader()
        n = 0
        for row in r:
            k = keys.get(row["response_id"])
            if not k:
                print(f"[WARN] {row['response_id']} 不在映射表，跳过", file=sys.stderr)
                continue
            w.writerow({c: k.get(c, "") for c in cols[:5]} | row)
            n += 1
    print(f"[pass{pass_id}] 揭盲 → {out.relative_to(ROOT)}（{n} 行）")


# ------------------------------------------------------------------ 入口
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="匿名化 / 随机化 / block 分配 / 打分表模板")
    ap.add_argument("--make-demo", action="store_true", help="生成 MOCK 假回答以跑通流程")
    ap.add_argument("--pass2", action="store_true", help="生成重测包（须在 pass1 后 ≥72h 使用）")
    ap.add_argument("--retest-questions", type=int, default=8)
    ap.add_argument("--unblind", action="store_true")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--n-blocks", type=int, default=6, help="人工打分的 block 数（每 block≈10 份，约 1 小时）")
    ap.add_argument("--rater", default="R1")
    ap.add_argument("--panel", default="core", choices=["core", "all"],
                    help="core=只匿名化人工打分面板（见 score_panel.json，默认 CORE）；all=含扩展面板")
    ap.add_argument("--score-models", default="",
                    help="覆盖人工打分面板，逗号分隔，如 deepseek,gpt5,qwen3；落盘 score_panel.json")
    ap.add_argument("--score-reason", default="", help="--score-models 的变更理由，写入 score_panel.json")
    args = ap.parse_args(argv)

    if args.make_demo:
        make_demo()
        return 0

    if args.score_models:
        sp = set_score_panel([m.strip() for m in args.score_models.split(",") if m.strip()],
                             args.score_reason)
        print(f"[panel] 人工打分面板已设为 {sp} -> {SCORE_PANEL_FILE.relative_to(ROOT)}")
    else:
        sp = load_score_panel()
        if sp != CORE_PANEL:
            print(f"[panel] 人工打分面板（来自 score_panel.json）：{sp}")

    panel = sp if args.panel == "core" else CORE_PANEL + EXT_PANEL

    if args.unblind:
        unblind(2 if args.pass2 else 1)
        return 0

    if args.pass2:
        qs = load_questions()
        pool = sorted(qs) if qs else sorted({r["qid"] for r in load_raw()})
        if not pool:
            sys.exit("[ERROR] 既无题库也无回答，无法选定重测题")
        rng = random.Random(args.seed + 777)  # 与 pass1 不同的随机流
        picked = sorted(rng.sample(pool, min(args.retest_questions, len(pool))))
        print(f"[pass2] 重测题（{len(picked)} 题）：{picked}")
        blind(pass_id=2, panel=sp, seed=args.seed + 777,
              n_blocks=max(3, len(picked) // 4 or 3), qids=picked, id_prefix="P")
        emit_template(2, sp, args.rater, qids=picked, retest_hide_pass1=True)
        return 0

    blind(pass_id=1, panel=panel, seed=args.seed, n_blocks=args.n_blocks, id_prefix="R")
    emit_template(1, sp, args.rater)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
