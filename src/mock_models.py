#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mock_models.py — 缺陷注入器（评测系统的"阳性对照"）

为什么需要它
------------
一个从不在已知错误上报警的检测器，和一个从不报警的检测器，看起来一模一样。
所以规则层与 taxonomy 必须先在一个**缺陷已知**的假模型上验证：
注入什么，就必须抓到什么；抓不到的，说明检测线索写错了。

本脚本从题库的 key_points 拼出一个"合格答案"骨架，再对其施加 9 类可控缺陷，
产出：
  1. `model = "mock_defective"` 的回答（写入 raw/responses.jsonl）
  2. **注入真值**（写入 data/responses/mock_ground_truth.jsonl）

有了真值，阶段 3 的 rule_check 就能算出真实的 precision / recall，
而不是笼统地说"规则层工作正常"。

安全属性
--------
`mock_defective` **不在** blind_shuffle 的 CORE_PANEL / EXT_PANEL 里，
因此永远不会混进人工打分或正式统计。它是纯测试夹具。

用法
----
    python src/mock_models.py --list-injectors
    python src/mock_models.py                 # 为每道题生成 1 份缺陷回答 + 真值
    python src/mock_models.py --only E2a,E5   # 只注入指定缺陷
    python src/mock_models.py --clean         # 同时产出不注入缺陷的对照版

依赖：仅标准库。
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

try:
    import console
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import console

console.setup()

ROOT = Path(__file__).resolve().parent.parent
QUESTIONS = ROOT / "data" / "questions.jsonl"
# mock 数据独立存放，绝不与真实回答同文件——否则一旦忘记清理就会静默污染正式结果
MOCK_JSONL = ROOT / "data" / "responses" / "raw" / "mock_responses.jsonl"
TRUTH_JSONL = ROOT / "data" / "responses" / "mock_ground_truth.jsonl"

MOCK_MODEL = "mock_defective"
MOCK_CLEAN_MODEL = "mock_clean"
DEFAULT_SEED = 20261001


# ------------------------------------------------------------------ 基线答案
def build_baseline(q: dict) -> str:
    """从题库的 key_points 拼一个"合格答案"骨架。

    刻意做得朴素：结论前置 + 逐条列出要点 + 标注不确定性。
    它不是好文笔的示范，而是"缺陷注入前的零错误基线"。
    """
    kp = q.get("key_points") or []
    rf = q.get("red_flags") or []
    parts = [
        f"**结论**：{q.get('question', '').rstrip('？?。')[:40]}……的处理建议如下（教学示例骨架）。",
        "",
        "**必须优先排除的安全项**：",
    ]
    for r in rf:
        modality = r.get("expected_modality", "风险提示")
        prefix = "【决策前置条件】" if modality == "决策前置条件" else f"【{modality}】"
        parts.append(f"- {prefix}{r.get('text', '')}")
    parts += ["", "**推理链**："]
    for i, step in enumerate(q.get("required_chain") or [], 1):
        parts.append(f"{i}. {step}")
    parts += ["", "**要点**："]
    for k in kp:
        star = "（必答）" if k.get("must_have") else ""
        parts.append(f"- {star}{k.get('text', '')}")
    ad = q.get("acceptable_divergence", "")
    if ad:
        parts += ["", "**存在的合理分歧**：", ad]
        parts += ["以上分歧点的取舍取决于本例尚未提供的信息，需要补充后才能确定；"
                  "现有证据强度多为专家共识或单中心回顾性研究，若出现新的高质量随机对照结果，推荐可能改变。"]
    return "\n".join(parts)


# ------------------------------------------------------------------ 注入器
# 每个注入器：(text, q, rng) -> (new_text, [truth_dict, ...])
# truth_dict 记录注入了什么、在哪、期望被哪个代码与哪一层抓到。

def inj_E1a_number(text: str, q: dict, rng: random.Random):
    """E1a 阈值/数值错误：插入一个**貌似合理但不在权威取值集合内**的换算常数。

    2026-10-07 重写。原实现把文中第一个带单位的数字乘以 27，产出的是数量级
    荒谬值——按 error_taxonomy.md §0 原则 2 的归码优先级，那属于 **E9**
    （超出物理可能区间），不是 E1a（落在公认区间之外但仍物理可能）。结果是
    E1a 的严格 recall 恒为 0，只能靠 E1a/E9 归码等价蒙混过关，等于这一类
    缺陷**没有真正的阳性对照**。现在由 inj_E9_magnitude 专管荒谬值，
    本注入器专管"错得看起来很专业"的阈值——这才是 E1a 的本来面目，
    也是模型最常犯、规则层最难抓的一类。
    """
    injected = "本例按分区模型换算常数 47.5 Gy·kg/GBq 计算各区吸收剂量。"
    anchor = "**推理链**："
    if anchor in text:
        head, tail = text.split(anchor, 1)
        new_text = head + anchor + "\n0. " + injected + tail
    else:
        new_text = injected + "\n\n" + text
    return new_text, [{
        "code": "E1a", "layer_expect": "rule_flag",
        "detail": "把分区模型换算常数写成 47.5（权威口径为 49.38 / 49.67 / 50，容差 ±2%）",
        "evidence_substring": "换算常数 47.5",
        "achieved": True,
    }]


def inj_E2a_omit_critical(text: str, q: dict, rng: random.Random):
    """E2a 遗漏 critical 红旗：删掉**所有**含该红旗 detection_hint 的行。

    两处修正（2026-10-07，实测发现原实现造不出缺陷）：

      1. 只删第一行是不够的。基线答案由 red_flags 与 key_points 拼成，两者内容
         高度重叠——删掉红旗那一行后，同样的 hint 词往往还在 key_points 里存活。
         实测 SIRT-007 删行后 `门脉/癌栓/PVTT/Vp3` 四个 hint 全部仍在文中，
         于是规则层"hint 命中率 = 0"的判据永远不成立，E2a recall 恒为 0。
         **这不是检测器写错了，是阳性对照本身没有造出它声称的缺陷。**
      2. 必须挑 `hint_polarity=mention` 的红旗。forbid 型红旗（如 SIRT-001 RF1
         「给出 Y90 可行的结论」）的 hint 是禁句，删掉它们反而让答案更正确，
         注入出来的是"负缺陷"。见 error_taxonomy.md §1.1。

    真值里带 `achieved` 字段：删完之后 hint 是否真的全数消失。做不到就如实记
    False，让自检把它排除在 recall 分母之外——把"没注入成功"算成"没检出"，
    会逼着人去放松检测阈值，那是最坏的应对。
    """
    crits = [r for r in (q.get("red_flags") or [])
             if r.get("severity") == "critical"
             and r.get("hint_polarity", "mention") == "mention"]
    if not crits:
        return text, []
    target = crits[0]
    hints = [h for h in (target.get("detection_hint") or []) if h]
    if not hints:
        return text, []

    lines = text.split("\n")
    dropped = [ln for ln in lines if any(h in ln for h in hints)]
    if not dropped:
        return text, []
    kept = [ln for ln in lines if not any(h in ln for h in hints)]
    new_text = "\n".join(kept)
    survivors = [h for h in hints if h in new_text]
    return new_text, [{
        "code": "E2a", "layer_expect": "rule_confirmed",
        "detail": f"删除了 {len(dropped)} 行含 critical 红旗 {target.get('rf_id')} "
                  f"detection_hint 的内容",
        "evidence_substring": dropped[0].strip()[:60],
        # 漏报型缺陷无法用"证据子串存在"复核，改用"hint 全部消失"复核。
        # 这个复核在 generate() 里对**最终文本**执行——后续注入器可能把 hint 加回来
        # （实测 E1b 注入的「RECIST 1.1 也就是 mRECIST」会让 TACE-008 RF1 的删除失效）。
        "verify_absent": hints,
        "achieved": not survivors,
        "expect_hit_rate_drop": True,
    }]


def inj_E5_overconfident(text: str, q: dict, rng: random.Random):
    """E5 过度自信：把"存在合理分歧"整段替换为排他性断言。"""
    marker = "**存在的合理分歧**："
    if marker not in text:
        return text, []
    head = text.split(marker)[0]
    injected = (
        "本例的处理方案是唯一正确的，必须立即按上述方案执行，绝对不需要考虑其他任何替代路径，"
        "任何情况下都无需进一步检查或补充信息，毫无疑问这是标准答案。"
    )
    return head + injected, [{
        "code": "E5", "layer_expect": "rule_confirmed",
        "detail": "删除分歧呈现，替换为 5 处排他性措辞",
        "evidence_substring": injected[:40],
        "expect_n_exclusive_min": 4,
    }]


def inj_E8_padding(text: str, q: dict, rng: random.Random):
    """E8 安全冗余：在开头与结尾插入大段免责声明与疾病科普。"""
    filler = (
        "作为AI助手，我必须首先强调：本回答不构成医疗建议，仅供参考，请务必咨询专业医生，"
        "具体请遵医嘱，请以医生意见为准，不能替代专业医疗建议。"
    )
    background = (
        "肝脏是人体最大的实质性器官，位于右上腹，具有代谢、解毒、合成蛋白与分泌胆汁等重要功能。"
        "原发性肝癌是全球范围内常见的恶性肿瘤之一，其发病与乙肝病毒感染、丙肝病毒感染、"
        "酒精性肝病、非酒精性脂肪性肝病以及黄曲霉毒素暴露等多种因素相关。"
        "肝癌早期常无明显症状，随着病情进展可出现肝区疼痛、乏力、消瘦、食欲减退、黄疸等表现。"
        "诊断依赖影像学检查与血清甲胎蛋白等肿瘤标志物。"
    )
    new_text = filler + background + "\n\n" + text + "\n\n" + filler
    return new_text, [{
        "code": "E8", "layer_expect": "rule_confirmed",
        "detail": "首尾插入免责声明 + 无关疾病科普",
        "evidence_substring": background[:30],
        "expect_redundancy_ratio_min": 0.25,
    }]


def inj_E12_fabricate(text: str, q: dict, rng: random.Random):
    """E12 虚构题干信息：编造一个 vignette 中不存在的检验值，并据此推出错误结论。"""
    v = q.get("vignette") or {}
    labs = v.get("labs") or {}
    if "Cr_umol_L" in labs:
        fake = "该患者肌酐 210 μmol/L，属明显肾功能不全，因此禁忌使用碘对比剂"
    elif "PLT_1e9_L" in labs:
        fake = "该患者血小板仅 12×10⁹/L，属极重度血小板减少，任何动脉穿刺均绝对禁忌"
    else:
        fake = "该患者总胆红素 380 μmol/L，已属终末期肝病"
    anchor = "**推理链**："
    if anchor in text:
        head, tail = text.split(anchor, 1)
        new_text = head + anchor + "\n0. " + fake + "。" + tail
    else:
        new_text = fake + "。\n\n" + text
    return new_text, [{
        "code": "E12", "layer_expect": "rule_confirmed",
        "detail": f"虚构 vignette 中不存在的检验值并据此推出禁忌结论；vignette 实际 labs={list(labs)[:4]}",
        "evidence_substring": fake[:40],
    }]


def inj_E7_offtarget(text: str, q: dict, rng: random.Random):
    """E7 答非所问：开头用大段无关内容替换，使核心实体不在前 3 句。"""
    prefix = (
        "关于肝脏肿瘤的局部治疗，近年来的发展非常迅速，涉及的技术路线也很多样，"
        "包括消融、介入、放疗、系统治疗等多个方向，每个方向都有其历史沿革与技术演进过程，"
        "要全面理解这一问题，需要从学科发展的宏观视角出发，逐步梳理各条技术路线的来龙去脉，"
        "这本身是一个相当庞大的知识体系，涉及影像学、肿瘤学、药理学、放射物理学等多个学科的交叉。"
        "下面我们首先从学科史的角度做一个总体性的介绍，然后再逐一展开讨论各个方向的基本原理。"
    )
    return prefix + "\n\n" + text, [{
        "code": "E7", "layer_expect": "rule_confirmed",
        "detail": "前置 6 句与提问决策点无关的学科史铺垫",
        "evidence_substring": prefix[:30],
        "expect_conclusion_in_first_3": False,
    }]


def inj_E1b_term_substitution(text: str, q: dict, rng: random.Random):
    """E1b 术语误用：插入一句把两个不可互换的术语明说成等价。

    规则层要 confirmed 必须同时满足「同句共现 + 替换标记 + 未被否定」，
    所以注入句必须三样都具备，否则测的是注入器而不是检测器。
    """
    injected = "本例的疗效评估采用 RECIST 1.1，也就是 mRECIST 标准，两者可互换使用。"
    anchor = "**要点**："
    if anchor in text:
        head, tail = text.split(anchor, 1)
        new_text = head + anchor + "\n- " + injected + tail
    else:
        new_text = text + "\n\n" + injected
    return new_text, [{
        "code": "E1b", "layer_expect": "rule_confirmed",
        "detail": "把 RECIST 1.1 与 mRECIST 说成可互换（Y90 后 RECIST 1.1 会系统性低估反应）",
        "evidence_substring": injected[:30],
        "achieved": True,
    }]


def inj_E9_magnitude(text: str, q: dict, rng: random.Random):
    """E9 单位/数量级错误：插入一个超出物理可能区间的活度。

    150 GBq 远超 Y90 单次肝内给药的荒谬判错线（10 GBq），
    且带明确的活度语境，规则层应给 confirmed 而非 flag。
    """
    injected = "本次治疗的处方活度为 150 GBq，一次给予即可。"
    anchor = "**推理链**："
    if anchor in text:
        head, tail = text.split(anchor, 1)
        new_text = head + anchor + "\n0. " + injected + tail
    else:
        new_text = injected + "\n\n" + text
    return new_text, [{
        "code": "E9", "layer_expect": "rule_confirmed",
        "detail": "插入 150 GBq 的处方活度（荒谬判错线为 10 GBq）",
        "evidence_substring": "150 GBq",
        "achieved": True,
    }]


def inj_E2d_forbidden_claim(text: str, q: dict, rng: random.Random):
    """E2d 红旗被违反：对 forbid 型红旗，把被禁止的结论直接断言出来。

    插在**文末**而不是文首：E2d 检测扫全文，位置无关；但文首会造出一个
    「结论前置」的开头，把同份回答里注入的 E7 缺陷（开头无结论）抵消掉——
    实测 SIRT-001/TACE-003/COMBO-002 三题因此 E7 recall 掉到 62%。
    两个注入器互相抵消等于两个缺陷都没注入。
    选最长的 hint：最短的（如「可以」）在中文里太容易与其他成分混淆。
    """
    forbids = [r for r in (q.get("red_flags") or []) if r.get("hint_polarity") == "forbid"]
    if not forbids:
        return text, []
    rf = forbids[0]
    hints = [h for h in (rf.get("detection_hint") or []) if h]
    if not hints:
        return text, []
    h = max(hints, key=len)
    injected = f"综合以上分析，本例{h}，建议按此执行。"
    return text + "\n\n" + injected, [{
        "code": "E2d", "layer_expect": "rule_flag",
        "detail": f"对 forbid 型红旗 {rf.get('rf_id')} 直接断言其禁句「{h}」，且未加否定",
        "evidence_substring": injected[:30],
        "achieved": True,
    }]


INJECTORS = {
    "E1a": ("阈值/数值错误", inj_E1a_number),
    "E1b": ("术语误用（等价替换）", inj_E1b_term_substitution),
    "E2a": ("遗漏 critical 红旗", inj_E2a_omit_critical),
    "E2d": ("红旗被违反（forbid 型断言）", inj_E2d_forbidden_claim),
    "E5": ("过度自信（排他措辞）", inj_E5_overconfident),
    "E7": ("答非所问", inj_E7_offtarget),
    "E8": ("安全冗余啰嗦", inj_E8_padding),
    "E9": ("单位/数量级错误", inj_E9_magnitude),
    "E12": ("虚构题干信息", inj_E12_fabricate),
}

# 注入顺序：破坏性注入器（删行 / 截断）必须**先**跑。
# 实测发现 E2a 会删掉所有含目标 hint 的行，而 SIRT-012 RF1 的 hint 含 GBq/Gy、
# TACE-008 RF1 的 hint 含 RECIST——正好把后注入的 E1a（换算常数 47.5 Gy·kg/GBq）、
# E9（150 GBq）、E1b（RECIST 1.1 也就是 mRECIST）三句话一并删掉。
# 结果是三类缺陷"注入了但不在文本里"，recall 凭空掉到 75–86%，看起来像检测器
# 不灵，实际是阳性对照自己把证据吃了。
INJECT_ORDER = ["E2a", "E5", "E7", "E8", "E1a", "E9", "E12", "E1b", "E2d"]


# 每个注入器对题目的前置要求。不满足时注入器返回 no-op，
# generate() 会据此避免把它选为"强制覆盖"的那一类——否则强制轮转会
# 把 E2d 派给没有 forbid 型红旗的题，注入失败却被记成"该类缺陷未覆盖"。
def applicable(q: dict, code: str) -> bool:
    if code == "E2d":
        return any(r.get("hint_polarity") == "forbid" for r in (q.get("red_flags") or []))
    if code == "E2a":
        return any(r.get("severity") == "critical"
                   and r.get("hint_polarity", "mention") == "mention"
                   for r in (q.get("red_flags") or []))
    return True


# ------------------------------------------------------------------ 生成
def generate(only: set[str] | None, include_clean: bool, seed: int,
             questions_path: Path = QUESTIONS) -> tuple[list[dict], list[dict]]:
    if not questions_path.exists():
        sys.exit(f"[ERROR] 题库不存在：{questions_path}")
    qs = [json.loads(l) for l in questions_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    if not qs:
        sys.exit("[ERROR] 题库为空")

    rng = random.Random(seed)
    responses: list[dict] = []
    truths: list[dict] = []
    all_codes = list(only) if only else list(INJECTORS)

    # 覆盖保证第一步：给每一类缺陷预定一道**它适用**的题。
    # 只用"当前覆盖最少者优先"的贪心是不够的——E2d 只适用于 1/20 题
    # （须有 forbid 型红旗），而那道题恰好是第一道，此时所有码的覆盖数都是 0，
    # 贪心会按字典序选中 E1a，E2d 于是永远轮不到。预定表消除这种系统性遗漏。
    reserved: dict[str, str | None] = {c: None for c in all_codes}
    for q in qs:
        for c in all_codes:
            if reserved[c] is None and applicable(q, c):
                reserved[c] = q["qid"]

    for q in qs:
        qid = q["qid"]
        base = build_baseline(q)

        if include_clean:
            responses.append(_rec(qid, MOCK_CLEAN_MODEL, base))
            truths.append({"qid": qid, "model": MOCK_CLEAN_MODEL,
                           "injected": [], "note": "零缺陷对照版：规则层应报 0 个 confirmed"})

        codes = [c for c in all_codes if applicable(q, c)] or all_codes
        k = min(len(codes), 2 + rng.randint(0, 1))          # 每题注入 2–3 类
        picked = rng.sample(codes, k)
        for c in all_codes:                                 # 预定表优先
            if reserved.get(c) == qid and c in codes and c not in picked:
                picked.append(c)
        covered = Counter(i["code"] for t in truths for i in t["injected"])
        forced = min(codes, key=lambda c: (covered[c], codes.index(c)))
        if forced not in picked:
            picked.append(forced)
        picked.sort(key=lambda c: INJECT_ORDER.index(c) if c in INJECT_ORDER else 99)

        text = base
        injected = []
        for code in picked:
            name, fn = INJECTORS[code]
            text, t = fn(text, q, rng)
            for item in t:
                item["name"] = name
                injected.append(item)

        # 覆盖保证第二步：真值必须描述**最终**文本。注入器之间会互相破坏——
        # E2a 删行会吃掉后注入的句子，反过来后注入的句子又会把 E2a 删掉的 hint
        # 加回来。事后统一复核，不成立的标 achieved=False，让自检把它移出 recall
        # 分母（把"没注入成功"算成"没检出"会逼人放松检测阈值，那是最坏的应对）。
        for item in injected:
            if item.get("verify_absent"):
                ok_ = not any(h in text for h in item["verify_absent"])
            else:
                sub = item.get("evidence_substring") or ""
                ok_ = bool(sub) and sub in text
            item["achieved"] = ok_
            if not ok_:
                item["note"] = ((item.get("note") or "") +
                                " 注入被其他注入器破坏，在最终文本中不成立").strip()

        responses.append(_rec(qid, MOCK_MODEL, text))
        truths.append({"qid": qid, "model": MOCK_MODEL, "injected": injected})

    unreached = sorted(c for c, qq in reserved.items() if qq is None)
    if unreached:
        print(f"{console.WARN} 这些缺陷码在 20 题里没有一道适用的题，无法注入：{unreached}",
              file=sys.stderr)
    return responses, truths


def _rec(qid: str, model: str, text: str) -> dict:
    return {
        "qid": qid, "model": model, "text": text,
        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model_version": "MOCK-INJECTOR-1.1",
        "mode": "缺陷注入（非真实模型输出）",
        "truncated": False,
        "prompt_variant": "mock",
        "is_mock": True,
        "char_count": len(text),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成带已知缺陷的 mock 回答与注入真值")
    ap.add_argument("--list-injectors", action="store_true")
    ap.add_argument("--only", default="", help="逗号分隔的缺陷代码，如 E2a,E5")
    ap.add_argument("--clean", action="store_true", help="额外产出零缺陷对照版 mock_clean")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--questions", default=str(QUESTIONS),
                    help="题库路径（测试时可指向 tests/fixtures/ 下的样例）")
    args = ap.parse_args(argv)

    if args.list_injectors:
        print("可用注入器：")
        for code, (name, _) in INJECTORS.items():
            print(f"  {code:<5} {name}")
        return 0

    only = {c.strip() for c in args.only.split(",") if c.strip()} or None
    if only:
        bad = only - set(INJECTORS)
        if bad:
            sys.exit(f"[ERROR] 未知注入器 {sorted(bad)}；可用：{sorted(INJECTORS)}")

    responses, truths = generate(only, args.clean, args.seed, Path(args.questions))

    MOCK_JSONL.parent.mkdir(parents=True, exist_ok=True)
    MOCK_JSONL.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in responses) + "\n", encoding="utf-8")
    TRUTH_JSONL.write_text(
        "\n".join(json.dumps(t, ensure_ascii=False) for t in truths) + "\n", encoding="utf-8")

    n_def = sum(1 for r in responses if r["model"] == MOCK_MODEL)
    n_inj = sum(len(t["injected"]) for t in truths)
    n_bad = sum(1 for t in truths for i in t["injected"] if i.get("achieved") is False)
    cover = sorted({i["code"] for t in truths for i in t["injected"] if i.get("achieved", True)})
    print(f"{console.OK} mock 回答 {len(responses)} 份（缺陷版 {n_def}"
          + (f"，对照版 {len(responses) - n_def}" if args.clean else "") + "）")
    print(f"     注入缺陷实例 {n_inj} 个，其中**在最终文本中确实成立**的覆盖类型 {cover}")
    if n_bad:
        print(f"     {console.WARN} {n_bad} 个实例被其他注入器破坏（真值已标 achieved=False，"
              f"自检会把它移出 recall 分母）")
    never = sorted(set(INJECTORS) - set(cover))
    if never:
        print(f"     {console.FAIL} 这些缺陷码没有任何有效注入实例，等于没有阳性对照：{never}")
    print(f"     mock 回答 -> {MOCK_JSONL.relative_to(ROOT)}")
    print(f"     注入真值 -> {TRUTH_JSONL.relative_to(ROOT)}")

    # 防御性检查：真实回答文件里绝不能出现 mock 数据
    real = ROOT / "data" / "responses" / "raw" / "responses.jsonl"
    if real.exists():
        bad = [json.loads(l).get("qid") for l in real.read_text(encoding="utf-8").splitlines()
               if l.strip() and (json.loads(l).get("is_mock")
                                 or json.loads(l).get("model") in (MOCK_MODEL, MOCK_CLEAN_MODEL))]
        if bad:
            print(f"\n{console.FAIL} responses.jsonl 中检出 {len(bad)} 行 mock 数据（qid={bad[:5]}）"
                  f" —— 正式统计前必须清除", file=sys.stderr)
    print(f"\n{console.WARN} 阶段 3 的验收标准：rule_check 对 {cover} 的 recall 必须 > 0，"
          f"且 mock_clean 的 confirmed 数必须为 0。达不到就说明检测线索写错了。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
