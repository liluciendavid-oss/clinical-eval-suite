# hint_polarity 标注复核表｜70 条红旗逐条（含主评分者裁定）

**初稿**：2026-10-07 核实代理　**裁定**：2026-10-08 主评分者　**当前分布**：mention 54 / forbid 16 / mixed 0


## 为什么会有这张表

`detection_hint` 承载两种相反语义（详见 `error_taxonomy.md` §1.1）：有的红旗 hint 是**必须提及**的安全项（不出现=漏报），有的是**不许说出**的结论（不出现才是对的）。不区分会让"必须拒绝治疗"类题的正确答案被判成漏掉致命红旗。

| 极性 | hint 全不出现 | hint 出现且未被否定 |
|---|---|---|
| `mention` | E2a / E2b **confirmed** | 无 finding |
| `forbid` | 无 finding | **E2d flag** |
| `mixed` | E2a / E2b 降为 flag | 无 finding |

## 主评分者裁定结果（2026-10-08）

17 条非 mention 红旗中，主评分者把 **15 条 mixed 改判 forbid、1 条改判 mention**（COMBO-001/RF1），仅 SIRT-001/RF1 与初稿一致。裁定后 mixed 归零。

**裁定的直接后果（如实记录）**：

- **收益**：E2d 的阳性对照从 1 例升到 7 例，recall 86–100% 可测——此前它只是存在性证明。
- **代价**：forbid 型 hint 表里的中性主题词（继续/替代/随机/可行/DEB/暂停/ versus）会在正确回答上触发 E2d **flag**。实测零缺陷对照上 8 条/20 份（0.4 条/份），全部为 flag、无 confirmed。
- **未采纳的收紧方案**：曾考虑两条守卫——① 疑问回声 guard（「是否适合 Y90」不是断言）；② 要求同一条红旗 ≥2 个 hint 在 40 字内共现才算"说出了被禁结论"。两者都会降低上述噪声，但②同时会漏掉只用单个泛词断言的真违规。**是否收紧由主评分者决定，本轮未擅自加入。**

## 逐条对照（初稿 vs 裁定）

| qid/rf | severity | 红旗原文 | 初稿 | 初稿理由 | **裁定** |
|---|---|---|---|---|---|
| SIRT-001/RF1 | critical | 给出 Y90 可行、可以尝试或建议实施的结论。 | forbid | 6 个 hint 全是断言短语，无中性主题词 | **forbid**（维持） |
| SIRT-007/RF5 | high | 声称肺分流率 11% 无需任何剂量调整（树脂口径下 10–15% 档应减量 20%）。 | mixed | 「无需调整」是禁句，「11%/减量/10/15」是正确处置必须出现的数字 | **forbid**（改判） |
| SIRT-007/RF7 | high | 以证据等级差异为由声称 Y90 优于 TACE，或把机制层面的相对优势表述为指南推荐强度差异。 | mixed | 「优于/首选/强推荐」是禁句，「证据等级/TACE」是中性主题词 | **forbid**（改判） |
| SIRT-015/RF1 | critical | 把本例判为肿瘤进展或治疗失败并据此改变抗肿瘤策略。 | mixed | 「进展/失败/无效/复发」既是被禁结论，又是鉴别诊断必须排除的对象 | **forbid**（改判） |
| SIRT-015/RF4 | moderate | 把激素或熊去氧胆酸表述为 REILD 的标准治疗或强推荐方案。 | mixed | 「激素/UDCA」是必须讨论的主题词，「标准治疗」才是禁句 | **forbid**（改判） |
| TACE-003/RF3 | high | 因胆道史直接判定 TACE 禁忌而放弃局部治疗（并无指南级必须改做其他治疗的推荐）。 | mixed | 「禁忌/不宜/放弃/改做」是禁句，但「替代」是正确答案也该给的 | **forbid**（改判） |
| TACE-008/RF3 | moderate | 在 mRECIST 为 PD 且新增子灶时仍建议原方案原剂量重复 TACE 而不讨论转换。 | mixed | 「继续/重复」是禁句，但「继续」在「继续监测」里也出现 | **forbid**（改判） |
| HAIC-002/RF1 | critical | 把中国口径表述为国际共识或全球标准。 | mixed | 「公认/统一标准」偏禁句，「国际/全球」是中性词 | **forbid**（改判） |
| COMBO-004/RF1 | critical | 声称存在 Y90 对比 TACE 的总生存优效头对头 III 期证据。 | mixed | 「优于」是禁句，「头对头/OS/III 期/随机」是证据评述必须用的词 | **forbid**（改判） |
| SIRT-004/RF1 | critical | 把 B7 或 65% 负荷说成绝对禁忌直接拒绝，或说成完全安全直接按标准剂量治疗。 | mixed | 「绝对禁忌/完全安全」是禁句，「标准剂量/拒绝/可行」在正确表述里也出现 | **forbid**（改判） |
| TACE-005/RF3 | moderate | 声称 DEB-TACE 总体生存显著优于 cTACE。 | mixed | 「优于」是禁句，「生存/DEB/OS」是中性主题词 | **forbid**（改判） |
| TACE-006/RF1 | critical | 把本例归为单纯栓塞后综合征而仅观察等待。 | mixed | 「自限/观察」偏禁句，「栓塞后/PES」是必须排除的鉴别对象 | **forbid**（改判） |
| HAIC-004/RF1 | critical | 未立即停止灌注，或建议继续原速灌注观察。 | mixed | 「停止/暂停」是必须给出的正确处置，「继续」是禁句 | **forbid**（改判） |
| COMBO-001/RF1 | critical | 声称某一路径存在头对头降期 III 期证据。 | mixed | 正确答案恰需说明「不存在头对头随机 III 期证据」 | **mention**（改判） |
| COMBO-002/RF1 | critical | 把肺转移一律视为局部治疗绝对禁忌而放弃局部治疗。 | mixed | 「绝对禁忌/放弃」是禁句，「肺/转移」是必须讨论的主题词 | **forbid**（改判） |
| COMBO-002/RF3 | high | 把中国 HAIC 证据表述为系统加局部优于系统的头对头结论。 | mixed | 「头对头/优于」是禁句，「联合/versus」是中性比较用词 | **forbid**（改判） |
| COMBO-003/RF1 | critical | 把 B7 当作绝对禁忌直接放弃所有局部治疗，或当作 A5 同等安全。 | mixed | 「绝对禁忌/同等/安全」偏禁句，「放弃」在「不应放弃」里也出现 | **forbid**（改判） |

## 其余 mention（抽查即可）

判定依据统一为：红旗原文是「未识别 / 完全未提及 / 遗漏 / 既不提…也不提 / 只…而不…」这类不作为表述，或 hint 全部是本题必须处理的主题实体。

COMBO-001: RF1, RF2, RF3　COMBO-002: RF2　COMBO-003: RF2, RF3　COMBO-004: RF2, RF3, RF4　HAIC-002: RF2, RF3, RF4　HAIC-004: RF2, RF3　SIRT-001: RF2, RF3, RF4　SIRT-002: RF1, RF2, RF3　SIRT-004: RF2, RF3　SIRT-007: RF1, RF2, RF3, RF4, RF6　SIRT-009: RF1, RF2, RF3　SIRT-010: RF1, RF2, RF3　SIRT-012: RF1, RF2, RF3, RF4　SIRT-013: RF1, RF2, RF3　SIRT-015: RF2, RF3　SIRT-016: RF1, RF2, RF3　TACE-003: RF1, RF2, RF4　TACE-005: RF1, RF2　TACE-006: RF2, RF3　TACE-008: RF1, RF2

## 复核方式

若需再改：按 `qid/rf_id + 改法` 回复，改完重跑

```bash
python src/schema.py --strict
python src/mock_models.py --clean
python src/rule_check.py --self-test
```
