#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dictionaries.py — error_taxonomy.md §2 检测词典的机器可读版

为什么单独成文件
----------------
词典是规则层的全部智能所在，也是**最容易被悄悄改坏**的部分：改一个阈值就能让
某个模型的错误率凭空下降。所以它必须满足三个条件——可版本化、可 review、
可自动核对。本文件提供第三条：`--check-sync` 会把这里的常量与
`docs/error_taxonomy.md` §2.x 的表格逐项比对，任一处漂移即报错退出。

**改一处必须改另一处。** 这不是注释里的客套话，是 CI 会拦的硬约束。

一条贯穿全文件的设计纪律
------------------------
只有假阳性率在直觉上接近 0 的规则才允许输出 `confirmed`；其余一律
`flag_for_review`（见 error_taxonomy.md §0 原则 1）。因此下面几乎每个词典都
配套一个「豁免 / 守卫」结构——它们不是可选的润色，而是 `confirmed` 合法性的前提。

实测依据（2026-10-07，20 题零缺陷对照版 mock_clean）：
  * 「绝对」在干净回答里出现 15 次，**15 次全部是「绝对禁忌」这一正确术语**。
    没有 EXCLUSIVE_EXEMPTIONS，E5 会在 14/20 份无缺陷回答上误报 confirmed。
  * 干净回答里有 7/20 份的前 3 句不含任何术式名（多为剂量计算题，提问本身
    就不含术式名）。所以 E7 采用「核心实体缺失 **且** 结论标记缺失」双条件，
    单条件版本会把三分之一的正确答案判成答非所问。

用法
----
    python src/dictionaries.py --check-sync     # 与 error_taxonomy.md 核对（CI 用）
    python src/dictionaries.py --stats          # 打印各词典规模

零第三方依赖：只用标准库。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

try:
    import console
except ImportError:  # 允许从仓库根目录以 -m 方式运行
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import console

console.setup()

ROOT = Path(__file__).resolve().parent.parent
TAXONOMY_DOC = ROOT / "docs" / "error_taxonomy.md"
QUESTIONS = ROOT / "data" / "questions.jsonl"

DICT_VERSION = "1.0"
DOC_LAST_CHECKED = "2026-10-02"   # error_taxonomy.md 的最后更新日期，用于 E1c 时效提示

# =====================================================================
# §2.1 术语混淆词典（E1b）
# =====================================================================
# 文档原文的判定是「成对出现即判错」，但实测发现这会误伤正确回答：
# 一句「不要用 RECIST 1.1，应改用 mRECIST」同时含两个术语，且完全正确。
# 因此规则层把判定收紧为「成对**替换**」（文档 §1 表格里 E1b 的原始措辞），
# 即两个术语在同一句内出现、且带等价/互换标记词时才 confirmed；
# 仅同句共现而无替换标记 → flag_for_review。
TERM_CONFUSION: list[dict] = [
    {"correct": "mRECIST", "confused": "RECIST 1.1",
     "why": "Y90 后肿瘤坏死但体积不变，RECIST 1.1 只测径线会系统性低估反应"},
    {"correct": "LI-RADS Treatment Response (LR-TR)", "confused": "LI-RADS 诊断分类",
     "why": "前者评疗效，后者评诊断概率，两套类别不可互换"},
    {"correct": "cTACE（碘油+化疗药）", "confused": "DEB-TACE（载药微球）",
     "why": "栓塞机制、剂量学与并发症谱不同"},
    {"correct": "TAE（单纯栓塞）", "confused": "TACE",
     "why": "是否含化疗药物直接影响适应证与毒性"},
    {"correct": "Vp3（一级分支癌栓）", "confused": "Vp4（主干癌栓）",
     "why": "决定 TACE 是相对禁忌还是绝对禁忌"},
    {"correct": "REILD（放射性栓塞后肝病）", "confused": "普通肝衰竭 / 病毒性肝炎发作",
     "why": "诊断标准与时间窗特异（Y90 后数周至数月）"},
    {"correct": "放射性肺炎", "confused": "放射性肝炎 / REILD",
     "why": "不同靶器官、不同限值、不同预防手段"},
    {"correct": "Child-Pugh 分级", "confused": "ALBI 分级 / MELD",
     "why": "三套体系阈值与用途不同，不可互相替代"},
    {"correct": "BCLC 分期", "confused": "CNLC 分期",
     "why": "两套分期对同一患者的归类可能不同，混用会导致适应证判断错"},
    {"correct": "玻璃微球（TheraSphere）", "confused": "树脂微球（SIR-Spheres）",
     "why": "剂量学体系不通用：前者用 Gy 处方，后者用 BSA/经验法"},
    {"correct": "GBq", "confused": "mCi", "why": "1 GBq = 27 mCi，混用导致 27 倍剂量错误"},
    {"correct": "Gy", "confused": "cGy", "why": "100 倍差"},
    {"correct": "HAIC（肝动脉灌注化疗）", "confused": "全身静脉化疗",
     "why": "给药途径决定毒性谱与适应证"},
    {"correct": "门静脉癌栓（PVTT，肿瘤性）", "confused": "门静脉血栓（bland thrombus）",
     "why": "处理策略完全不同，影像鉴别要点不同"},
    {"correct": "肺分流率（LSF）", "confused": "肺灌注比例 / 分流分数（心内分流）",
     "why": "概念不同"},
    {"correct": "Vp2（二级分支/段支）", "confused": "Vp3",
     "why": "日本分型：段支 = Vp2，一级分支（门脉左/右支）= Vp3，主干 = Vp4。混用会导致禁忌判断错一级"},
    {"correct": "Vp3（一级分支/叶支）", "confused": "Vp4",
     "why": "同上；Vp4 的 Y90 禁忌是**条件性**的（需同时 MAA 显像癌栓无摄取）"},
    {"correct": "程氏 Ⅱ 型", "confused": "Vp3",
     "why": "中国分型对照：程 Ⅰ ≈ Vp1–2，程 Ⅱ ≈ Vp3，程 Ⅲ ≈ Vp4，程 Ⅳ 累及肠系膜上静脉"},
    {"correct": "REILD（放射性栓塞后肝病）", "confused": "外照射经典 RILD",
     "why": "REILD 为**有黄疸**型；经典外照射 RILD 为**无黄疸**型腹水，两者机制与时间窗不同"},
    {"correct": "mRECIST", "confused": "LI-RADS TRA v2024",
     "why": "v2024 起放射路径为独立算法，与 mRECIST **并行**而非替代；且 LR-TR v2024 不适用于系统/免疫治疗"},
    {"correct": "120 ± 20 Gy（玻璃标准处方）", "confused": "≥205 Gy（个体化靶剂量）",
     "why": "两个口径对应不同治疗意图（标准 vs 个体化/消融性），混用会导致剂量目标错档"},
]

# 只有出现这些等价/互换标记时，术语共现才升级为 confirmed E1b。
# 刻意不收单字「即」：它会命中「必须立即停用」「即使」这类与术语替换无关的
# 常见搭配，单字标记在中文里的假阳性率高到不可用。
SUBSTITUTION_MARKERS: list[str] = [
    "即是", "即为", "也就是", "等同于", "相当于", "等于", "又称", "也叫",
    "可互换", "同义", "视为", "算作", "换成", "替换为", "就是一样",
    "两者相同", "通用",
]

# =====================================================================
# §2.2 排他性措辞词典（E5）
# =====================================================================
EXCLUSIVE_WORDING: list[str] = [
    "必须", "唯一", "绝对", "一定", "只能", "毫无疑问", "无需考虑", "必然",
    "任何情况下", "标准答案就是", "肯定不能", "不允许", "完全排除",
]

# 豁免：这些搭配是**正确的临床术语**，不是过度自信。
# 实测 mock_clean 中「绝对」出现 15 次，全部落在「绝对禁忌」上。
# rubric D4 的豁免规则要求「默认不豁免 + 备注强制」，那是针对人工评分者的纪律；
# 规则层若不豁免术语，产出的将全是假阳性，反而淹没真信号。
# 人工评分时仍须按 rubric 逐条写豁免备注——两层规则刻意不同，理由记录在此。
EXCLUSIVE_EXEMPTIONS: list[str] = [
    "绝对禁忌", "绝对禁忌证", "绝对不应", "绝对不可", "绝对禁用",
    "必须排除", "必须停用", "必须停药", "必须纠正", "必须先", "必须立即停",
    "必然进展", "唯一可根治", "唯一治愈",
]

# E5 confirmed 的第二个必要条件：回答**没有**对争议点作双向呈现。
# 文档定义是「排他措辞 ≥2 且用于 acceptable_divergence 覆盖的争议点」，
# 后半句规则层无法直接判，用「双向呈现缺席」作为可观测代理，并在此写明。
DIVERGENCE_MARKERS: list[str] = [
    "存在争议", "存在分歧", "合理分歧", "两种路径", "两条路径", "另一种选择",
    "不同中心", "不同做法", "也可以考虑", "亦可", "取决于", "尚无定论",
    "争议点", "不同指南", "各自适用", "一方面", "另一方面",
]

# =====================================================================
# §2.3 模糊/对冲措辞词典（E6）
# =====================================================================
HEDGE_WORDING: list[str] = [
    "可能", "或许", "也许", "视情况", "存在争议", "尚无定论", "需个体化",
    "证据有限", "不一定", "有待研究", "尚在探索",
]

# =====================================================================
# §2.4 时间敏感断言词典（E1c）——一律 flag_for_review
# =====================================================================
# `assertion` 字段必须与文档表格第一列逐字一致（--check-sync 会核对）。
# `pattern` 是规则层实际用的正则；`negation_guard` 命中即跳过，
# 因为「Vp3 并非绝对禁忌」这类**正确**表述会被朴素正则误伤。
TIME_SENSITIVE: list[dict] = [
    {"assertion": "索拉非尼是晚期 HCC 唯一的一线选择",
     "pattern": r"索拉非尼[^。；\n]{0,25}唯一[^。；\n]{0,12}一线",
     "why": "已有多个一线方案", "basis": "—"},
    {"assertion": "中晚期肝癌尚无有效系统治疗",
     "pattern": r"中晚期[^。；\n]{0,15}(?:尚无|没有|缺乏)[^。；\n]{0,10}(?:有效)?系统治疗",
     "why": "明显过时", "basis": "—"},
    {"assertion": "TACE 是不可切除 HCC 的唯一治疗手段",
     "pattern": r"TACE[^。；\n]{0,25}唯一[^。；\n]{0,12}(?:治疗手段|选择|方法|方案)",
     "why": "忽略了 HAIC、Y90、系统治疗", "basis": "—"},
    {"assertion": "肝移植是唯一可能根治的手段",
     "pattern": r"肝移植[^。；\n]{0,12}唯一[^。；\n]{0,12}(?:根治|治愈)",
     "why": "忽略了消融与切除的根治性", "basis": "—"},
    {"assertion": "Y90 在中国尚未开展",
     "pattern": r"Y\s?-?90[^。；\n]{0,18}(?:尚未|还未|未)[^。；\n]{0,8}(?:开展|获批|上市|应用)",
     "why": "明显错误", "basis": "—"},
    {"assertion": "PD-1 单抗在 HCC 中无效",
     "pattern": r"PD-?1[^。；\n]{0,22}(?:无效|无疗效|没有疗效|不敏感)",
     "why": "明显过时", "basis": "—"},
    {"assertion": "SARAH 或 SIRveNIB 证明 Y90 优于索拉非尼",
     "pattern": r"(?:SARAH|SIRveNIB)[^。；\n]{0,45}(?:优于|证明[^。；\n]{0,12}优|胜过|好过)",
     "why": "两项 III 期均为**阴性**（SARAH HR 1.15 p=0.18；SIRveNIB HR 1.1 P=.36）",
     "basis": "Lancet Oncol 2017; J Clin Oncol 2018 摘要逐字"},
    {"assertion": "DOSISPHERE-01 是阴性试验",
     "pattern": r"DOSISPHERE[^。；\n]{0,35}(?:阴性|未达[^。；\n]{0,6}终点|失败|无差异)",
     "why": "**阳性**：个体化组 3 个月客观缓解 71% vs 标准组 36%（p=0074）；长期 OS 24.8 vs 10.7 个月（HR 0.51）",
     "basis": "Lancet Gastroenterol Hepatol 2021; J Nucl Med 2024"},
    {"assertion": "必须常规预防性栓塞胃十二指肠动脉",
     "pattern": r"(?:常规|必须|所有患者|一律)[^。；\n]{0,14}栓塞[^。；\n]{0,12}(?:胃十二指肠动脉|GDA)",
     "why": "已转为**选择性**：系统评价示栓塞组 55/456 vs 未栓塞组 34/781，胃肠道并发症无差异；CIRSE 调查 GDA 栓塞率 71%→34%",
     "basis": "CVIR 2016 系统评价; CVIR 2018; EANM 2022"},
    {"assertion": "Vp2/Vp3（段支/叶支）癌栓是 Y90 绝对禁忌",
     "pattern": r"Vp\s?[23][^。；\n]{0,28}绝对禁忌",
     "why": "非禁忌，反而是相对适应证；中国 2026 版 PVTT 指南对 Ⅰ/Ⅱ/Ⅲ 型 Child-Pugh A 给「可行 SIRT，B 级证据、强推荐」",
     "basis": "中国 2026 版指南推荐意见 8; EANM 2022; Salem 2008"},
    {"assertion": "RECIST 1.1 适用于 Y90 后疗效评估",
     "pattern": r"RECIST\s?1\.1[^。；\n]{0,28}(?:适用|可用于|用于|首选于)[^。；\n]{0,18}(?:Y\s?-?90|放射栓塞|微球|治疗后)",
     "why": "系统性低估：mRECIST 缓解率 40.8% vs RECIST 3.9%（p=0.025）",
     "basis": "Seyal 2015 Hepatology; EANM 2022"},
    {"assertion": "肺分流率 10–20% 一律减量 20%",
     "pattern": r"10\s?[–~\-—]\s?20\s?%[^。；\n]{0,28}(?:一律|均|统一|都)[^。；\n]{0,12}(?:减|降低|下调)\s?20",
     "why": "树脂为**三档**：10–15% 减 20%、15–20% 减 40%、>20% 不治疗；玻璃无百分比档",
     "basis": "Kallini 2016 / Garin 2016 / Cremonesi 2014 逐字复述说明书"},
    {"assertion": "换算常数为 49.7",
     "pattern": r"(?:(?:换算常数|k\s?值|分区模型系数|转换系数)[^。；\n]{0,12}49\.7)|(?:49\.7[^。；\n]{0,14}(?:Gy·kg|Gy\s?·\s?kg|换算|系数))",
     "why": "权威口径为 49.67（EANM/Levillain）、49.38（AAPM MPPG 14.a）或临床惯写 50；「49.7 出自 Ho 1996」无法核实",
     "basis": "AAPM 2024; EANM 2022; Levillain 2021"},
]

# 命中守卫：匹配片段内含这些词说明原文是在**否定**该错误断言，属正确表述。
# 前后各 20 字都要查——实测 SIRT-016 的零缺陷回答写「常规预防性栓塞 GDA
# **已非必需**」，纠正词落在匹配片段之后，只查片段内会把它判成过时断言。
NEGATION_GUARD: list[str] = [
    "并非", "并不是", "不是", "不属于", "不构成", "非绝对", "不适用", "不宜用于",
    "错误", "误区", "不应", "不可", "勿", "避免", "已被推翻", "不再", "相反",
    "常见误解", "需要注意的", "不要",
    "已非必需", "非必需", "无需", "已转", "已不", "选择性", "并非必需", "不主张",
]

# E2d 的近距否定前缀：在 forbid 型 hint 命中处**之前** 4 字内出现，即视为被否定。
# 实测「本例不适合 Y90 微球治疗」里的「适合 Y90」是正确答案，不是违规；
# 「目前缺乏头对头 III 期证据」里的「头对头」同理。
# 2026-10-08 用户把 15 条 mixed 改判 forbid 后新增四个排除类前缀：forbid 型 hint 表
# 里含主题词（进展/复发/国际/继续…），而正确答案常以「需排除肿瘤进展」「肝脓肿暂不支持」
# 的句式用到它们——不把"排除/不支持"当否定，E2d 会在正确回答上刷屏。
NEGATION_PREFIX: list[str] = [
    "不", "非", "未", "无", "别", "勿", "拒", "免", "缺乏", "缺少", "欠缺", "尚无",
    "排除", "除外", "不支持", "不考虑",
]

# E2d 的禁止性框架：命中处所在行、或其上方最近的小节标题/引导句里含这些词，
# 说明该短语是被**列出来否定**的（"必须优先排除的安全项：给出 Y90 可行的结论"），
# 而不是被断言的。没有这一层，任何"以下误区需避免"式的正确回答都会被误报。
PROHIBITION_FRAMING: list[str] = [
    "必须排除", "必须优先排除", "需要排除", "应排除", "要排除", "应予排除",
    "不得", "不应", "不可", "禁止", "严禁", "切勿", "避免", "误区", "陷阱",
    "不能", "不宜", "忌讳", "下列错误", "以下错误", "需警惕", "应警惕",
]

# =====================================================================
# §2.5 单位合法集与数量级白名单（E9）
# =====================================================================
LEGAL_UNITS: list[str] = [
    "GBq", "mCi", "Gy", "cGy", "mL", "L", "kg", "%", "cm", "mm",
    "μmol/L", "mg/dL", "g/L", "×10⁹/L", "mg/m²", "mg/kg", "ng/mL",
    "周", "月", "天",
]

# 每条白名单 = 「各口径并集的上界 + 一个明显荒谬的判错线」。
# 文档明确要求「不用单一数字判错」，因此：
#   * 超出 fail_above / 低于 fail_below → confirmed（荒谬值，无口径可解释）
#   * 落在「文献冲突区间」内 → flag_for_review（可能是引用了另一套合法口径）
# context_required / context_excluded 是归因守卫：同一个「300 Gy」在肿瘤靶剂量
# 语境下合法（段切 330–400 Gy），在非肿瘤肝语境下荒谬。不做归因就会误判。
MAGNITUDE_WHITELIST: list[dict] = [
    {"quantity": "分区模型换算常数 k",
     "acceptable": "{49.38, 49.67, 50} Gy·kg/GBq，容差 ±2%",
     "fail_rule": "k < 40 或 k > 60",
     "unit": "", "number_re": r"(?:换算常数|k\s?值|分区模型系数|转换系数)[^。；\n0-9]{0,12}(\d{1,3}(?:\.\d+)?)",
     "fail_above": 60.0, "fail_below": 40.0,
     "allowed_set": [49.38, 49.67, 50.0], "allowed_tol_pct": 2.0,
     "context_required": [], "context_excluded": [],
     "verdict_confirmed": "E9", "verdict_flag": "E1a",
     "basis": "AAPM MPPG 14.a 2024（49.38）; EANM 2022 / Levillain 2021（49.67）; ABS 2022（约 50）"},
    {"quantity": "Y90 单次总活度",
     "acceptable": "0.05–10 GBq", "fail_rule": "> 10 GBq",
     "unit": "GBq", "number_re": r"(\d+(?:\.\d+)?)\s*(GBq|mCi)",
     "fail_above": {"GBq": 10.0, "mCi": 270.0}, "fail_below": None,
     "context_required": [], "context_excluded": ["肺"],
     "verdict_confirmed": "E9", "verdict_flag": None,
     "basis": "临床常规区间；>10 GBq 单次肝内给药无物理/临床依据"},
    {"quantity": "肺吸收剂量限值",
     "acceptable": "单次 ≤30 Gy、累积 ≤50 Gy", "fail_rule": "声称单次安全上限 >30 Gy",
     "unit": "Gy", "number_re": r"(\d+(?:\.\d+)?)\s*(Gy|cGy)",
     "fail_above": {"Gy": 50.0, "cGy": 5000.0}, "fail_below": None,
     "context_required": ["肺"], "context_excluded": [],
     "single_dose_line": {"Gy": 30.0, "cGy": 3000.0},
     "single_markers": ["单次", "单回", "一次", "本次", "single"],
     "verdict_confirmed": "E9", "verdict_flag": "E9",
     "basis": "TheraSphere 标签; Ho 1997; Sangro 2017; AAPM 2024; ABS 2022（两型通用）"},
    {"quantity": "玻璃微球肺活度上限",
     "acceptable": "≤0.61 GBq（16.5 mCi）", "fail_rule": ">0.61 GBq 仍称可行",
     "unit": "GBq", "number_re": r"(\d+(?:\.\d+)?)\s*(GBq|mCi)",
     "fail_above": {"GBq": 0.61, "mCi": 16.5}, "fail_below": None,
     "context_required": ["肺"], "context_excluded": [],
     "verdict_confirmed": None, "verdict_flag": "E9",
     "basis": "TheraSphere 美国标签禁忌证逐字"},
    {"quantity": "非肿瘤肝平均受量（树脂）",
     "acceptable": "≤40 Gy（肝功能受损 ≤30 Gy）", "fail_rule": ">70 Gy",
     "unit": "Gy", "number_re": r"(\d+(?:\.\d+)?)\s*(Gy|cGy)",
     "fail_above": {"Gy": 150.0, "cGy": 15000.0}, "fail_below": None,
     "flag_above": {"Gy": 70.0, "cGy": 7000.0},
     "context_required": ["非肿瘤肝", "正常肝", "非靶肝", "NTL", "余肝"],
     "context_excluded": ["肿瘤靶", "靶剂量", "段切", "消融性"],
     "verdict_confirmed": "E9", "verdict_flag": "E1a",
     "basis": "Levillain 2021（强一致）; Cremonesi 2014; EANM 2022"},
    {"quantity": "非肿瘤肝平均受量（玻璃）",
     "acceptable": "Child-Pugh A5 约 ≤70 Gy；全正常肝 <90 Gy（胆红素 <1.1 mg/dL）或 ≤50 Gy（>1.1）",
     "fail_rule": ">150 Gy",
     "unit": "Gy", "number_re": r"(\d+(?:\.\d+)?)\s*(Gy|cGy)",
     "fail_above": {"Gy": 150.0, "cGy": 15000.0}, "fail_below": None,
     "context_required": ["非肿瘤肝", "正常肝", "非靶肝", "NTL", "余肝"],
     "context_excluded": ["肿瘤靶", "靶剂量", "段切", "消融性"],
     "verdict_confirmed": "E9", "verdict_flag": None,
     "basis": "EANM 2022; Cremonesi 2014; KJR 2025"},
    {"quantity": "玻璃微球肿瘤靶剂量",
     "acceptable": "标准 120±20 Gy；个体化 ≥205 Gy；段/叶切 330–400 Gy", "fail_rule": ">500 Gy",
     "unit": "Gy", "number_re": r"(\d+(?:\.\d+)?)\s*(Gy|cGy)",
     "fail_above": {"Gy": 500.0, "cGy": 50000.0}, "fail_below": None,
     "context_required": ["靶剂量", "处方剂量", "肿瘤剂量", "肿瘤区", "tumor dose"],
     "context_excluded": ["肺", "非肿瘤肝", "正常肝"],
     "verdict_confirmed": "E9", "verdict_flag": None,
     "basis": "AAPM 2024; DOSISPHERE-01; ABS 2022"},
    {"quantity": "肺分流率",
     "acceptable": "0–1（或 0–100%）", "fail_rule": "超出定义域",
     "unit": "%", "number_re": r"(\d+(?:\.\d+)?)\s*(%)",
     "fail_above": {"%": 100.0}, "fail_below": None,
     "context_required": ["分流"], "context_excluded": [],
     "verdict_confirmed": "E9", "verdict_flag": None,
     "basis": "定义域，无需外部依据"},
    {"quantity": "Child-Pugh 评分",
     "acceptable": "5–15", "fail_rule": "超出定义域",
     "unit": "", "number_re": r"Child-?Pugh\s*[ABCabc]?\s*(\d{1,2})\s*分",
     "fail_above": 15.0, "fail_below": 5.0,
     "context_required": [], "context_excluded": [],
     "verdict_confirmed": "E9", "verdict_flag": None,
     "basis": "定义域"},
    {"quantity": "总胆红素（μmol/L）",
     "acceptable": "0–600", "fail_rule": ">600",
     "unit": "μmol/L", "number_re": r"(?:胆红素|TBil|TBIL)[^。；\n0-9]{0,10}(\d+(?:\.\d+)?)\s*(?:μmol/L|umol/L)",
     "fail_above": 600.0, "fail_below": None,
     "context_required": [], "context_excluded": [],
     "verdict_confirmed": "E9", "verdict_flag": None,
     "basis": "超出生理可能"},
]

# 文档 §2.5 明确「仍未核实、因此仍未启用 E9」的量：只 flag，永不 confirmed
UNVERIFIED_QUANTITIES: list[dict] = [
    {"quantity": "REILD 的 TBil/ALP 量化倍数阈值",
     "reason": "一手英文指南只给定性描述，倍数阈值未核实",
     "pattern": r"REILD[^。；\n]{0,40}(\d+(?:\.\d+)?)\s*倍",
     "verdict_flag": "E1a"},
]

# =====================================================================
# §2.6 免责声明模板句库（E8）
# =====================================================================
DISCLAIMER_TEMPLATES: list[str] = [
    "作为AI助手", "作为人工智能", "本回答不构成医疗建议", "不构成医疗建议",
    "请务必咨询专业医生", "建议及时就医", "具体请遵医嘱", "仅供参考",
    "请以医生意见为准", "不能替代专业医疗建议", "如有疑问请咨询",
]

# E8 的判定是「免责声明 **+ 疾病科普段落**」占全文比 ≥ 0.25（文档 §1 E8 行、
# §2.6 末段）。文档只给了免责声明句库，科普段落的识别线索此前无处存放，
# 规则层却必须有——否则 E8 只能抓到一半。线索在此定义，并已同步写入
# error_taxonomy.md §2.6（--check-sync 会核对）。
BACKGROUND_EDU_CUES: list[str] = [
    "是人体", "是最大的", "是常见的恶性肿瘤", "发病率", "流行病学",
    "其发病与", "早期常无明显症状", "早期常无症状", "解剖上", "组织学上",
    "随着病情进展可出现", "诊断依赖影像学", "是一种常见的", "在全球范围内",
]

# =====================================================================
# §2.7 引用白名单（E3）——永远只能是 flag_for_review
# =====================================================================
CITATION_PATTERNS: dict[str, str] = {
    "cn_guideline": r"《[^》]{2,60}》",
    "nct": r"\bNCT\d{8}\b",
    "pmid": r"\bPMID[:\s]?\d{6,9}\b",
    "year_claim": r"\b\d{4}\s*年?\s*(?:发表|报道|研究)",
    "et_al": r"[A-Z][a-z]+\s+et\s+al\.?",
}

# 核心文献清单。来源：题库各题 reference_basis 的并集（运行时自动并入）
# + 下面这份静态清单，其条目全部取自本仓库已核实的文档
# （error_taxonomy.md §2.4 核实依据列、00_交接说明.md 纠错表、deviation_log.md），
# 未新增任何仓库外的文献名——凭空编一个「白名单条目」与凭空编一个引文同样是幻觉。
# 它只用于**降低 E3 的误报**；白名单不可能穷尽，所以 E3 永远只是 flag。
CORE_REFERENCES: list[str] = [
    "EANM 2022", "AAPM MPPG 14.a", "AAPM 2024", "Levillain 2021", "ABS 2022",
    "TheraSphere", "SIR-Spheres", "DOSISPHERE-01", "SARAH", "SIRveNIB",
    "Lancet Oncol 2017", "J Clin Oncol 2018", "Lancet Gastroenterol Hepatol 2021",
    "J Nucl Med 2024", "CVIR 2016", "CVIR 2018", "Salem 2008", "Seyal 2015",
    "Kallini 2016", "Garin 2016", "Cremonesi 2014", "Ho 1997", "Sangro 2017",
    "KJR 2025", "FOHAIC-1", "He 2019", "JAMA Oncol", "Yuan 2022", "JSH",
    "CIRSE", "Hepatology", "mRECIST", "LI-RADS", "BCLC", "CNLC", "Child-Pugh",
    "ALBI", "MELD", "RECIST 1.1", "MAA", "REILD",
]

# =====================================================================
# 规则层其他词典（rubric D2/D5/D6 与 E4b/E7/E10/E11/E12 所需）
# =====================================================================

# E7 双条件之一：结论标记。与 DIVERGENCE_MARKERS 一样，是「可观测代理」，
# 在文档 error_taxonomy.md §1 的 E7 行有对应说明。
CONCLUSION_MARKERS: list[str] = [
    "结论", "综上", "建议", "首选", "不宜", "禁忌", "应予", "应行", "不推荐",
    "判定", "判断为", "本例", "该患者", "我的处理", "处理顺序", "答案是",
    "不适合", "适合", "可以治疗", "不应", "可选择", "方案为",
]

# E7 双条件之二：题干核心实体（术式名 / 决策对象）
PROCEDURE_ENTITIES: list[str] = [
    "Y90", "Y-90", "钇90", "钇-90", "SIRT", "TACE", "DEB-TACE", "cTACE",
    "HAIC", "微球", "TheraSphere", "SIR-Spheres", "放射栓塞", "肝动脉",
    "肝动脉化疗栓塞", "栓塞", "灌注", "消融", "肝移植", "系统治疗", "靶向",
    "免疫", "分流", "分区模型", "BSA", "剂量", "活度", "MAA", "PVTT", "癌栓",
]

# 其中的**特异**子集：只有这些能用于 E7 的 confirmed 判定。
# 通用词（消融 / 系统治疗 / 栓塞 / 灌注 / 剂量 / 分流 / 靶向 / 免疫）会出现在
# 任何一段泛泛而谈的铺垫里——实测缺陷注入器的学科史填充段就含「消融、介入、
# 放疗、系统治疗」，用通用词做核心实体判定会让 E7 在 2 道题上漏检。
PROCEDURE_ENTITIES_SPECIFIC: list[str] = [
    "Y90", "Y-90", "钇90", "钇-90", "SIRT", "TACE", "DEB-TACE", "cTACE",
    "HAIC", "微球", "TheraSphere", "SIR-Spheres", "放射栓塞", "分区模型",
    "BSA", "MAA", "肝动脉化疗栓塞", "PVTT",
]

# E12 虚构题干信息：只针对**患者个体检验值**，不针对指南阈值。
# 三者缺一不可，否则「胆红素 <2 mg/dL 时禁忌」这类指南语句会被判成虚构：
#   1) 实体别名命中；2) 单位与该实体的规范单位一致（或可换算）；
#   3) 数值前带患者指代标记、且不带阈值比较符。
LAB_ENTITIES: dict[str, dict] = {
    "TBil": {"aliases": ["总胆红素", "胆红素", "TBIL", "TBil", "total bilirubin"],
             "unit": "μmol/L", "conv": {"mg/dL": 17.1, "μmol/L": 1.0},
             "vignette_keys": ["TBil_umol_L", "TBil_baseline_umol_L"]},
    "ALB": {"aliases": ["血清白蛋白", "白蛋白", "ALB", "albumin"],
            "unit": "g/L", "conv": {"g/dL": 10.0, "g/L": 1.0},
            "vignette_keys": ["ALB_g_L", "ALB_baseline_g_L"]},
    "PLT": {"aliases": ["血小板", "PLT", "platelet"],
            "unit": "×10⁹/L", "conv": {"×10⁹/L": 1.0, "10⁹/L": 1.0, "G/L": 1.0},
            "vignette_keys": ["PLT_1e9_L"]},
    "Cr": {"aliases": ["血肌酐", "肌酐", "Cr", "creatinine"],
           "unit": "μmol/L", "conv": {"mg/dL": 88.4, "μmol/L": 1.0},
           "vignette_keys": ["Cr_umol_L"]},
    "INR": {"aliases": ["INR", "国际标准化比值", "国际标准化比率"],
            "unit": "", "conv": {"": 1.0}, "vignette_keys": ["INR"]},
    "AFP": {"aliases": ["甲胎蛋白", "AFP"], "unit": "ng/mL",
            "conv": {"ng/mL": 1.0, "μg/L": 1.0},
            "vignette_keys": ["AFP_ng_mL", "AFP_baseline_ng_mL"]},
    "Hb": {"aliases": ["血红蛋白", "Hb", "HGB"], "unit": "g/L",
           "conv": {"g/L": 1.0, "g/dL": 10.0}, "vignette_keys": ["Hb_g_L", "Hb_baseline_g_L"]},
    "WBC": {"aliases": ["白细胞", "WBC"], "unit": "×10⁹/L",
            "conv": {"×10⁹/L": 1.0, "10⁹/L": 1.0}, "vignette_keys": ["WBC_1e9_L"]},
    "ALT": {"aliases": ["谷丙转氨酶", "丙氨酸氨基转移酶", "ALT"], "unit": "U/L",
            "conv": {"U/L": 1.0}, "vignette_keys": ["ALT_U_L"]},
    "ALP": {"aliases": ["碱性磷酸酶", "ALP"], "unit": "U/L",
            "conv": {"U/L": 1.0}, "vignette_keys": ["ALP_U_L", "ALP_baseline_U_L"]},
    "CRP": {"aliases": ["C反应蛋白", "C 反应蛋白", "CRP"], "unit": "mg/L",
            "conv": {"mg/L": 1.0}, "vignette_keys": ["CRP_mg_L"]},
    # PT 单独处理：vignette 记的是「延长秒数」，回答里常写「绝对秒数」，
    # 两者不可直接比。因此 PT 只 flag，永不 confirmed。
    "PT": {"aliases": ["凝血酶原时间", "PT"], "unit": "s", "conv": {"s": 1.0},
           "vignette_keys": ["PT_prolong_s"], "ambiguous": True},
}

# E12 的患者指代标记与阈值比较符
PATIENT_MARKERS: list[str] = ["该患者", "本例", "患者", "其", "该", "此", "本患者"]
THRESHOLD_COMPARATORS: list[str] = ["<", ">", "≤", "≥", "小于", "大于", "低于", "高于",
                                    "约", "超过", "不足", "~", "±", "以下", "以上",
                                    "之内", "以内"]

# 差值标记。E12 **不覆盖差值断言**，这是明确的能力边界而非疏漏：
# 实测 TACE-006 的零缺陷回答写「本例胆红素 +20 μmol/L、PT +4 s 已达标」，
# 那是 44−24 的正确差值，被当成绝对值就会误判成虚构。
# 要覆盖差值就得同时校验基线配对与减法本身，规则层做不到可靠，故一律跳过。
DELTA_MARKERS: list[str] = [
    "+", "＋", "升高", "上升", "增加", "增高", "较前", "较基线", "Δ", "延长",
    "下降", "降低", "减少", "翻倍", "倍",
]

# E9 的语境归因窗口（字符数，只看数字**之前**）。
# 中文里量的名称在数值之前（「非肿瘤肝平均受量 ≤40 Gy」）。实测 SIRT-013 的
# 零缺陷回答写「肿瘤受量不低于 205 Gy…，非肿瘤肝超限…」，用 ±40 字的对称窗口
# 会把后面的「非肿瘤肝」当成 205 Gy 的归因语境，凭空造出一个 confirmed 假阳性。
CONTEXT_ATTRIB_CHARS: int = 18

# E10 自相矛盾：显式否定词冲突（语义级交给 LLM-judge）
RECOMMEND_MARKERS: list[str] = ["首选", "推荐", "应行", "建议行", "宜行", "可以考虑",
                                "可选择", "应予", "优先选择"]
PROHIBIT_MARKERS: list[str] = ["不建议", "不推荐", "禁忌", "不宜", "不应", "避免",
                               "不予", "不适合", "禁用"]

# E11 拒答/反问替代
REFUSAL_MARKERS: list[str] = [
    "我无法", "我不能提供", "无法回答", "作为AI", "作为人工智能", "我不能给出",
    "请提供更多信息", "需要更多信息才能", "无法给出具体", "抱歉，我不能",
]

# E4b 推理链缺失：required_chain 元素的字符二元组重叠阈值（低于即视为缺失）
# 规则层只做「辅助」，产出 flag_for_review，最终由 LLM-judge / 人工裁定。
CHAIN_OVERLAP_THRESHOLD: float = 0.34

# E7 的开头窗口（字符数）。rubric D5 的原话是「结论出现在前 3 句或首个小节
# 标题内」，但按句号切句在中文长铺垫下不可靠：实测缺陷注入器写的 200 字学科史
# 只含 2 个句号，「前 3 句」会把正确答案的首句也算进去，导致 E7 漏检。
# 改用固定字符窗口，150 字约等于中文回答的开头 3–5 行。
LEAD_WINDOW_CHARS: int = 150

# =====================================================================
# 载入题库派生的白名单
# =====================================================================
def reference_whitelist(questions_path: Path = QUESTIONS) -> set[str]:
    """题库 reference_basis 并集 + CORE_REFERENCES，归一化为小写去空格 token 集。"""
    wl: set[str] = {norm(r) for r in CORE_REFERENCES}
    if questions_path.exists():
        for line in questions_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            q = json.loads(line)
            for rb in q.get("reference_basis") or []:
                wl.add(norm(rb))
                # 长串里再切出可识别的来源名，避免整句比对导致的漏匹配
                for m in re.finditer(r"[A-Z][A-Za-z\-]+(?:\s?\d{4})?", rb):
                    if len(m.group(0)) >= 4:
                        wl.add(norm(m.group(0)))
                for m in re.finditer(r"《[^》]{2,60}》", rb):
                    wl.add(norm(m.group(0)))
    return wl


def norm(s: str) -> str:
    """归一化：去空白、转小写、统一破折号。白名单比对全靠它。"""
    return re.sub(r"\s+", "", str(s)).replace("－", "-").replace("—", "-").lower()


# =====================================================================
# 与 error_taxonomy.md 的同步核对
# =====================================================================
def _doc_tables(doc: str) -> dict[str, list[list[str]]]:
    """按 §2.x / §2.x.y 小节切分，抽出每节的 markdown 表格行。"""
    out: dict[str, list[list[str]]] = {}
    cur = None
    for line in doc.splitlines():
        m = re.match(r"^#{3,4}\s+(2\.\d+(?:\.\d+)?)\s", line)
        if m:
            cur = m.group(1)
            out.setdefault(cur, [])
            continue
        if line.startswith("## ") and not line.startswith("### "):
            cur = None
            continue
        if cur and line.strip().startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if cells and not re.match(r"^:?-{2,}", cells[0]):
                out[cur].append(cells)
    return out


def _doc_section(doc: str, section: str) -> str:
    m = re.search(rf"^###\s+{re.escape(section)}\s.*?(?=^###\s|\Z)", doc, re.M | re.S)
    return m.group(0) if m else ""


def _doc_backtick_list(doc: str, section: str) -> list[str]:
    """抽出 §2.x 小节里第一个用 `、` 连接的行内代码列表（跳过表格行）。"""
    body = _doc_section(doc, section)
    items: list[str] = []
    for line in body.splitlines():
        if line.strip().startswith(("|", ">")):
            continue
        found = re.findall(r"`([^`]+)`", line)
        if found and "、" in line:
            items += found
            break
    return items


def _doc_label_list(doc: str, label: str) -> list[str]:
    """抽出形如 `**标签**（说明）：` 那一行（或紧随其后一行）的行内代码项。

    从 m.start() 而非 m.end() 开始扫，因为文档里标签与列表常在同一行。
    """
    m = re.search(rf"^\*\*{re.escape(label)}\*\*", doc, re.M)
    if not m:
        return []
    for line in doc[m.start():].splitlines():
        if not line.strip():
            continue
        if line.strip().startswith(("|", ">", "#")):
            break
        found = re.findall(r"`([^`]+)`", line)
        if found:
            return found
        if not re.match(rf"^\*\*{re.escape(label)}\*\*", line.strip()):
            break
    return []


# §2.8.2 守卫词典：文档标签 -> 本文件常量名
GUARD_DICTS: dict[str, str] = {
    "术语替换标记": "SUBSTITUTION_MARKERS",
    "排他措辞豁免": "EXCLUSIVE_EXEMPTIONS",
    "争议双向呈现标记": "DIVERGENCE_MARKERS",
    "否定守卫": "NEGATION_GUARD",
    "近距否定前缀": "NEGATION_PREFIX",
    "禁止性框架": "PROHIBITION_FRAMING",
    "结论标记": "CONCLUSION_MARKERS",
    "题干核心实体": "PROCEDURE_ENTITIES",
    "特异术式实体": "PROCEDURE_ENTITIES_SPECIFIC",
    "患者指代标记": "PATIENT_MARKERS",
    "阈值比较符": "THRESHOLD_COMPARATORS",
    "差值标记": "DELTA_MARKERS",
    "推荐标记": "RECOMMEND_MARKERS",
    "禁止标记": "PROHIBIT_MARKERS",
    "拒答标记": "REFUSAL_MARKERS",
    "疾病科普段落线索句": "BACKGROUND_EDU_CUES",
}


def check_sync(doc_path: Path = TAXONOMY_DOC) -> list[str]:
    """逐项比对本文件常量与 error_taxonomy.md §2。返回差异列表（空 = 同步）。"""
    if not doc_path.exists():
        return [f"找不到文档：{doc_path}"]
    doc = doc_path.read_text(encoding="utf-8")
    tables = _doc_tables(doc)
    code = globals()
    diffs: list[str] = []

    def cmp_sets(label: str, doc_items: set[str], code_items: set[str]) -> None:
        for x in sorted(doc_items - code_items):
            diffs.append(f"{label} 文档有、代码无：{x}")
        for x in sorted(code_items - doc_items):
            diffs.append(f"{label} 代码有、文档无：{x}")

    # §2.1 术语混淆（跳过表头行）
    rows = [r for r in tables.get("2.1", []) if r and r[0] != "正确术语"]
    cmp_sets("§2.1 术语混淆", {(r[0], r[1]) for r in rows if len(r) >= 2},
             {(d["correct"], d["confused"]) for d in TERM_CONFUSION})

    # §2.2 / §2.3 / §2.6 行内代码列表
    cmp_sets("§2.2 排他措辞", set(_doc_backtick_list(doc, "2.2")), set(EXCLUSIVE_WORDING))
    cmp_sets("§2.3 模糊措辞", set(_doc_backtick_list(doc, "2.3")), set(HEDGE_WORDING))
    cmp_sets("§2.6 免责声明模板", set(_doc_label_list(doc, "免责声明模板句")),
             set(DISCLAIMER_TEMPLATES))

    # §2.4 时间敏感断言：断言文本必须逐字一致
    rows = [r for r in tables.get("2.4", []) if r and r[0] != "断言模式"]
    cmp_sets("§2.4 时间敏感断言", {r[0].strip('"“”') for r in rows},
             {d["assertion"] for d in TIME_SENSITIVE})
    for d in TIME_SENSITIVE:
        try:
            re.compile(d["pattern"])
        except re.error as e:
            diffs.append(f"§2.4 `{d['assertion']}` 的 pattern 无法编译：{e}")

    # §2.5 合法单位集 + 数量级白名单的「量」名
    cmp_sets("§2.5 合法单位", set(_doc_backtick_list(doc, "2.5")), set(LEGAL_UNITS))
    rows = [r for r in tables.get("2.5", []) if r and r[0] != "量"]
    # 未核实的量在文档里是 §2.5 末尾的散文说明，不是表格行
    sec25 = _doc_section(doc, "2.5")
    unverified_in_doc = {d["quantity"] for d in UNVERIFIED_QUANTITIES
                         if d["quantity"] in sec25}
    cmp_sets("§2.5 数量级白名单",
             {r[0] for r in rows} | unverified_in_doc,
             {d["quantity"] for d in MAGNITUDE_WHITELIST} |
             {d["quantity"] for d in UNVERIFIED_QUANTITIES})
    for d in MAGNITUDE_WHITELIST + UNVERIFIED_QUANTITIES:
        for key in ("number_re", "pattern"):
            if d.get(key):
                try:
                    re.compile(d[key])
                except re.error as e:
                    diffs.append(f"§2.5 `{d['quantity']}` 的 {key} 无法编译：{e}")

    # §2.7 抽取正则
    m = re.search(r"^###\s+2\.7\s.*?(?=^###\s|\Z)", doc, re.M | re.S)
    doc_res = set(re.findall(r"^-\s*`(.+?)`", m.group(0), re.M)) if m else set()
    cmp_sets("§2.7 抽取正则", doc_res, set(CITATION_PATTERNS.values()))
    for name, pat in CITATION_PATTERNS.items():
        try:
            re.compile(pat)
        except re.error as e:
            diffs.append(f"§2.7 `{name}` 正则无法编译：{e}")

    # §2.8.2 守卫词典
    for label, const_name in GUARD_DICTS.items():
        cmp_sets(f"§2.8.2 {label}", set(_doc_label_list(doc, label)),
                 set(code[const_name]))
    m = re.search(r"\*\*字符二元组重叠阈值\*\*[^`]*`([\d.]+)`", doc)
    if not m:
        diffs.append("§2.8.2 字符二元组重叠阈值 文档缺失")
    elif abs(float(m.group(1)) - CHAIN_OVERLAP_THRESHOLD) > 1e-9:
        diffs.append(f"§2.8.2 字符二元组重叠阈值 文档={m.group(1)} 代码={CHAIN_OVERLAP_THRESHOLD}")
    m = re.search(r"\*\*E7 开头窗口\*\*[^`]*`(\d+)`", doc)
    if not m:
        diffs.append("§2.8.2 E7 开头窗口 文档缺失")
    elif int(m.group(1)) != LEAD_WINDOW_CHARS:
        diffs.append(f"§2.8.2 E7 开头窗口 文档={m.group(1)} 代码={LEAD_WINDOW_CHARS}")
    m = re.search(r"\*\*E9 语境归因窗口\*\*[^`]*`(\d+)`", doc)
    if not m:
        diffs.append("§2.8.2 E9 语境归因窗口 文档缺失")
    elif int(m.group(1)) != CONTEXT_ATTRIB_CHARS:
        diffs.append(f"§2.8.2 E9 语境归因窗口 文档={m.group(1)} 代码={CONTEXT_ATTRIB_CHARS}")
    # 特异子集必须真的是子集，否则 E7 的 confirmed 会用到通用词
    if not set(PROCEDURE_ENTITIES_SPECIFIC) <= set(PROCEDURE_ENTITIES):
        diffs.append("§2.8.2 特异术式实体不是题干核心实体的子集："
                     f"{sorted(set(PROCEDURE_ENTITIES_SPECIFIC) - set(PROCEDURE_ENTITIES))}")

    # §2.8.3 检验实体表：实体名 / 别名 / 规范单位 / vignette 字段
    rows = [r for r in tables.get("2.8.3", []) if r and r[0] != "实体"]
    doc_labs = {r[0]: r for r in rows if len(r) >= 5}
    cmp_sets("§2.8.3 检验实体", set(doc_labs), set(LAB_ENTITIES))
    for name, spec in LAB_ENTITIES.items():
        row = doc_labs.get(name)
        if not row:
            continue
        # 大小写不敏感比对：文档写 TBIL、代码里同时留 TBil，属同一别名
        doc_alias = {a.strip().lower() for a in row[1].split("/")
                     if a.strip() and a.strip() != "—"}
        code_alias = {a.lower() for a in spec["aliases"]}
        if doc_alias != code_alias:
            diffs.append(f"§2.8.3 {name} 别名不一致：文档={sorted(doc_alias)} "
                         f"代码={sorted(code_alias)}")
        if row[2].strip() != (spec["unit"] or "—"):
            diffs.append(f"§2.8.3 {name} 规范单位不一致：文档={row[2]} 代码={spec['unit']}")
        doc_keys = {k.strip().strip("`") for k in row[4].split(",") if k.strip()}
        if doc_keys != set(spec["vignette_keys"]):
            diffs.append(f"§2.8.3 {name} vignette 字段不一致：文档={sorted(doc_keys)} "
                         f"代码={sorted(spec['vignette_keys'])}")

    return diffs


# =====================================================================
# 入口
# =====================================================================
def stats() -> dict[str, int]:
    base = {
        "TERM_CONFUSION": len(TERM_CONFUSION),
        "SUBSTITUTION_MARKERS": len(SUBSTITUTION_MARKERS),
        "EXCLUSIVE_WORDING": len(EXCLUSIVE_WORDING),
        "EXCLUSIVE_EXEMPTIONS": len(EXCLUSIVE_EXEMPTIONS),
        "DIVERGENCE_MARKERS": len(DIVERGENCE_MARKERS),
        "HEDGE_WORDING": len(HEDGE_WORDING),
        "TIME_SENSITIVE": len(TIME_SENSITIVE),
        "NEGATION_GUARD": len(NEGATION_GUARD),
        "NEGATION_PREFIX": len(NEGATION_PREFIX),
        "PROHIBITION_FRAMING": len(PROHIBITION_FRAMING),
        "LEGAL_UNITS": len(LEGAL_UNITS),
        "MAGNITUDE_WHITELIST": len(MAGNITUDE_WHITELIST),
        "UNVERIFIED_QUANTITIES": len(UNVERIFIED_QUANTITIES),
        "DISCLAIMER_TEMPLATES": len(DISCLAIMER_TEMPLATES),
        "BACKGROUND_EDU_CUES": len(BACKGROUND_EDU_CUES),
        "CITATION_PATTERNS": len(CITATION_PATTERNS),
        "CORE_REFERENCES": len(CORE_REFERENCES),
        "CONCLUSION_MARKERS": len(CONCLUSION_MARKERS),
        "PROCEDURE_ENTITIES": len(PROCEDURE_ENTITIES),
        "PROCEDURE_ENTITIES_SPECIFIC": len(PROCEDURE_ENTITIES_SPECIFIC),
        "LAB_ENTITIES": len(LAB_ENTITIES),
        "PATIENT_MARKERS": len(PATIENT_MARKERS),
        "THRESHOLD_COMPARATORS": len(THRESHOLD_COMPARATORS),
        "DELTA_MARKERS": len(DELTA_MARKERS),
        "RECOMMEND_MARKERS": len(RECOMMEND_MARKERS),
        "PROHIBIT_MARKERS": len(PROHIBIT_MARKERS),
        "REFUSAL_MARKERS": len(REFUSAL_MARKERS),
        "CHAIN_OVERLAP_THRESHOLD": CHAIN_OVERLAP_THRESHOLD,
        "LEAD_WINDOW_CHARS": LEAD_WINDOW_CHARS,
        "CONTEXT_ATTRIB_CHARS": CONTEXT_ATTRIB_CHARS,
    }
    return dict(sorted(base.items(), key=lambda kv: str(kv[0])))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="检测词典（error_taxonomy.md §2 的机器可读版）")
    ap.add_argument("--check-sync", action="store_true",
                    help="与 docs/error_taxonomy.md 逐项核对，漂移即返回 1")
    ap.add_argument("--stats", action="store_true", help="打印各词典规模")
    ap.add_argument("--doc", default=str(TAXONOMY_DOC))
    args = ap.parse_args(argv)

    if args.stats or not args.check_sync:
        print(f"=== dictionaries v{DICT_VERSION}（文档核对日期 {DOC_LAST_CHECKED}）===")
        for k, v in stats().items():
            print(f"  {k:<26} {v}")
        print(f"  {'reference_whitelist':<26} {len(reference_whitelist())}（含题库 reference_basis 并集）")

    if args.check_sync:
        diffs = check_sync(Path(args.doc))
        if diffs:
            print(f"\n{console.FAIL} 词典与 error_taxonomy.md 不同步，共 {len(diffs)} 处：")
            for d in diffs:
                print(f"  - {d}")
            print("\n改一处必须改另一处（见本文件顶部说明）。")
            return 1
        print(f"\n{console.OK} dictionaries.py 与 error_taxonomy.md §2 完全同步。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
