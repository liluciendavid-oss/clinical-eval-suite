# Protocol v1.0｜盲测流程与偏差控制操作规程

**配套文件**：`docs/rubric.md`（怎么打分）、`prompts/answer_wrapper.md`（怎么提问）、`docs/_stage0_review.md` §3（11 项偏差的设计依据）
> **2026-10-14 发布说明**：下文保留原设计与历史排期，不代表所有步骤均已完成。实际样本、R2/judge状态、评分复核与本轮验证结果以 [release_status.md](release_status.md) 为准。未经确认不得将原计划写成已完成结果。

**本文件回答的问题**：从今天到出结论，每一步**具体怎么做、谁来做、做完怎么验证**。

---

## 0. 角色

| 角色 | 承担者 | 权限 |
|---|---|---|
| **主评分者 R1** | 你（介入与血管外科硕士，执医 + 规培双证） | 全量打分；题库作者 |
| **第二评分者 R2** | 1 位同学/同事 | 仅 5 题探索性子集；**不参与题库设计** |
| **judge 模型** | 面板外第 6 个模型（Gemini / GLM / 豆包任一） | 只做 pointwise 与 pairwise 打分 |
| **被评面板** | 核心：DeepSeek / GPT-5 / Claude；扩展：Qwen3 / Kimi | — |

> **一个必须承认的结构性问题**：R1 既是题库作者又是主评分者，存在"自己出题自己判"的循环。缓解手段只有两条，都要如实写进 README：① R2 的探索性 inter-rater 子集；② 规则层与 judge 的交叉验证。**它们能削弱这个循环，但消不掉它。**

---

## 1. 流程总览

```
[1] 题库定稿 ──► [2] schema 校验 ──► [3] 取回 130 份回答 ──► [4] 匿名化+分block
                                                                    │
                                                                    ▼
[8] 揭盲+统计+图表 ◄── [7] 重测(≥72h) ◄── [6] R2 探索性打分 ◄── [5] R1 首轮打分(360判定)
                                                                    │
                                                                    ▼
                                              [9] judge pointwise(100) + pairwise(120)
                                                                    │
                                                                    ▼
                                              [10] 一致性分析 + mock 阳性对照验收
```

**每一步的准出条件**写在下面各节。准出条件不满足就不进下一步——这是防止"边跑边改口径"的唯一办法，而边跑边改口径会让最终结论失去解释力。

---

## 2. 各步骤操作规程

### [1]–[2] 题库定稿与校验

```bash
python src/schema.py --stats           # 必须 ERROR 0
python src/schema.py --strict          # 交付前必须 WARN 也为 0
```

**准出**：
- `ERROR 0`；
- **`VERIFY` 标记为 0**（schema 会拦）。未核实的临床数值不允许进入标准答案——这是本项目的诚信底线，也是 `schema.py` 存在的核心理由；
- `contamination_risk=high` 的题 ≤ 5；
- 9 个能力标签全部被覆盖，5 个维度全部有 `judge_focus` 指向；
- **70 条红旗的 `hint_polarity` 全部显式标注**（schema 对未标注者报 WARN）。该字段决定 E2a/E2b/E2d 的判定方向，标错会把正确答案判成最严重的失效；逐条标注理由见 `docs/_hint_polarity_review.md`，**须由主评分者复核**。

### [3] 取回 130 份回答

按 `prompts/answer_wrapper.md` 执行。**关键纪律**：

| 纪律 | 违反后果 |
|---|---|
| 每题新开对话 | 跨题上下文泄漏，且各模型上下文长度不同 → 不可比 |
| 关闭联网搜索 | 变成在比"谁的搜索好" |
| 推理模式保持产品默认并记录 | 默认状态才是生态效度最高的口径 |
| 复制全文，**不删免责声明** | 免责声明是 E8 与 `redundancy_ratio` 的检测对象，删掉等于销毁证据 |
| 被截断就记 `truncated=true`，**不点继续生成** | 续写改变长度分布，长度偏置调整失效 |
| 全部在 48 小时内取完 | 版本漂移 |

**准出**：`responses.jsonl` + `responses_format_probe.jsonl` 每行 8 个元数据字段齐全。**原设计为 130 行（5 模型 × 26）；实际取回 104 行（4 模型 × 26）——kimi 未取回**，见 deviation_log 2026-10-09「面板」行。人工打分面板见 `data/responses/score_panel.json`（`--score-models` 可覆盖并落盘理由）。

#### [3.1] 录入页操作 SOP（`tools/paste_recorder.html`，逐项约 20–40 秒）

**一次性准备（每个模型开取前做一遍，约 2 分钟）**

1. 浏览器打开 `tools/paste_recorder.html`（双击即可，零依赖、纯本地、不联网）。
2. 点「载入题库」选 `data/questions.jsonl`。左侧队列应显示 **主集 0/100、格式探针 0/30**。
3. 在目标模型的网页端做四件事并**记下来**：新开对话；清空自定义指令/System prompt/记忆；**关闭联网搜索**；**推理模式保持原样、不要手动开关**（只动联网这一个开关）。把当时的实际状态写成 `mode` 字段，格式如 `深度思考=默认开(未改动) 联网=手动关`。开还是关必须是你看到的现状，不是示例值。
4. 抄下界面**真实显示**的版本号填进 recorder 的 `model_version`。**消费级产品常不显示版本号**（如 DeepSeek 网页版）——此时统一填 `未公开显示（以取回日期标识）`，**禁止把本文档或示例里出现过的型号名当作观测值填入**（`DeepSeek-V3.2` 在 §[10] 只是"必须被拒的 judge 型号"测试夹具，不是任何人的版本号）。版本号不可得时的真实版本控制是第 1 步的分批纪律：**同一模型的 26 项在一次连续作业内取完**，版本在构造上即为常量。

**逐项循环（130 次）**

1. 左侧队列点一项（默认勾选「只看未完成」；可按模型/类型筛选）。建议**按模型分批**：一次取完一个模型的 26 项（20 主集 + 6 探针），换模型前重做上面第 3、4 步。
2. 点「复制提示词」→ 粘进模型对话框 → 发送。
3. **等模型完全停止输出**再复制全文。一个字都不要删：开头的「作为 AI 助手…」、结尾的「请务必咨询专业医生」是 E8 与 `redundancy_ratio` 的检测对象；Markdown 符号（`# * |`）是 D5 结构判断的依据。
4. 粘回 recorder 的大文本框；填 `model_version` 与 `mode`；若被长度截断，勾「回答被长度限制截断」，**不要点「继续生成」**（续写会改变长度分布，使长度偏置调整失效）。
5. 点「保存并跳下一项」。已保存的项标绿。
6. 中途可以关页面：数据在浏览器 localStorage 里，下次打开仍在。**但不要用两台机器或两个浏览器并行取同一模型**——localStorage 不互通，会漏项。

**收尾**

1. 全部 130 项标绿后点「导出 jsonl」：浏览器下载 `responses.jsonl`（主集 100）与 `responses_format_probe.jsonl`（探针 30）两个文件；toast 报「仍缺 N 份」，**N 必须为 0**。
2. 两个文件放进 `data/responses/raw/`。
3. 跑 `python src/blind_shuffle.py` 生成盲测包（它会做缺漏检查并打印 block 规模，应 10±2）。**不要**跑 `--make-demo`。
4. **48 小时内取完全部 130 份**（版本漂移控制）。跨天取时 `retrieved_at` 由 recorder 自动逐条记录，无需手填。

#### [3.2] 取回之后的命令序列（按顺序，勿跳步）

```bash
python src/blind_shuffle.py                    # 匿名化 + 分 block + 打分表模板
python src/rule_check.py                       # 规则层（产物含模型名，打分期间别看）
# —— 人工打分：填 data/scores/human_pass1_template.csv，另存为 human_pass1.csv ——
python src/score_audit.py                      # 质控；mismatch 进队列，不自动改分
python src/blind_shuffle.py --unblind          # 6 个 block 全部打完后才揭盲
python src/llm_judge.py --emit pointwise       # judge 输入（文件名匿名）
python src/llm_judge.py --emit pairwise
# —— 粘贴回收 judge 回复到 data/judge/outbox/ 同名文件 ——
python src/llm_judge.py --parse --judge-model <面板外型号>
python src/stats.py --all
python src/report.py --full                    # results/SUMMARY.md + 5 图 + 原始表
```

> **打分期间的盲测纪律**：`data/scores/rule/`、`data/scores/judge/`、`data/stats/` 含明文模型名与对每份回答的预判，`human_pass1.csv` 完成前一律不要打开。

### [4] 匿名化与 block 分配

```bash
python src/blind_shuffle.py --seed 20261001 --n-blocks 6
```

产出：`data/responses/blinded/pass1/block_0..5/`（评分者可见）、`key.jsonl`（**评分者不可见**）、`manifest.json`、`data/scores/human_pass1_template.csv`。

**准出**：脚本自身会校验"同题不同 block"约束并打印 `[OK]`；block 规模应在 10±2。

> **6 个 block × 10 份 ≈ 每 block 60–70 分钟**。不要一天做完两个 block：疲劳会同时压低 D5 的严格度与 `off_list_observation` 的质量。

**本轮实际排期（2026-10-09 定，取回于当日 18:19 完成）**：block 0 于 **10-09 晚**开做，block 1–5 为 10-10→10-14（一天一个）；[7] 重测须距 [5] 末次打分 ≥72h，落 **10-17**；[9]/[10] 于 10-18；**10-19 交付，留 1 天缓冲**。该排期成立的前提是 block 0 不拖到 10-10——若拖，[5] 收在 10-15、重测落 10-18、报告与交付同日，缓冲归零，须在 deviation_log 登记并相应打折结论。

### [5] R1 首轮打分（360 判定）

按 `docs/rubric.md` §1 的 9 步流程，逐 block 进行。**首次开打前先读 `docs/scoring_worksheet.md`**（rubric ↔ `human_pass1.csv` 逐列对照、`key_points`/`red_flags` 的读取位置、forbid 型红旗的 hit/miss 语义）。

**盲测期的额外纪律**：规则层与 judge 层的产物（`data/scores/rule/`、`data/scores/judge/`、`data/stats/`）**含明文模型名，且含对每份回答的预判**。打分期间一律不得打开——看到「deepseek 在 SIRT-007 上漏了 RF2」再去打分，匿名化就白做了。这些目录在盲测期已 gitignore（见 `.gitignore` 的「盲测期间」小节）。

**准出**（`src/score_audit.py` 自动检查）：
- 无空的中间量列；
- `off_list_observation` 全部非空；
- 填的维度分与中间量查表结果一致；不一致的进 `mismatch` 队列——**优先怀疑 rubric 有歧义，改 rubric 而不是改分数**；
- `minutes_spent < 2` 的行复核。

### [6] R2 探索性打分（90 判定）

- 题量：5 题 × 3 模型 = 15 份回答；
- 培训：30 分钟（读 rubric §0–§1 → 做 1 道已标注校准题 → 讨论分歧）；
- R2 **不看** R1 的任何分数，也**不参与**题库设计；
- 单独存为 `data/scores/human_r2.csv`（`rater_id=R2`）。

**准出**：R2 完成 15 份，`score_audit` 通过。**结果只作 exploratory 报告，n=15，不做推断性结论。**

### [7] R1 重测（144 判定，距 [5] ≥72 小时）

```bash
python src/blind_shuffle.py --pass2 --retest-questions 8 --seed 20261001
```

- 用 **不同的随机流**（`seed + 777`）与**不同的编号前缀**（`P####`）重新匿名化；
- 呈现顺序与 pass1 不同；
- 打分时**不得接触 pass1 的任何分数**（模板里不含 pass1 数据，这是脚本保证的，不是靠自觉）。

**准出**：`human_pass2.csv` 完成 24 行 × 6 维。**实际间隔天数必须记录**，写进 README。

> 若排期被迫压缩到 <72h：重测一致性会被记忆效应系统性高估，结论必须相应打折，并在局限里写明实际间隔。不要假装 72h 达成了。

### [9] LLM-judge

```bash
python src/llm_judge.py --emit pointwise     # 生成 80 份待粘贴 judge 输入（4 模型 × 20 主集；kimi 未取回）
python src/llm_judge.py --emit pairwise      # 生成 120 对（score_panel 内 3 对 × 20 题 × 2 顺序）
# 手动粘贴 → 回收 → 
python src/llm_judge.py --parse              # 解析回填，丢弃无法解析或无原文证据的判定
```

**judge 纪律**：
- judge 必须是面板外模型，型号写入 `meta.json`；
- judge 输出必须包含**原文证据片段**，无证据的判定直接丢弃；
- pairwise 的 A/B 两次结果**同向才计胜率，反向记 tie**，并单独报告"位置敏感对数"。

**准出**：judge JSON 解析成功率 ≥ 90%（低于此说明 prompt 模板有问题，修模板而不是硬凑数据）。

### [10] 一致性分析与 mock 阳性对照

```bash
python src/stats.py --all      # 胜率+CI、α/κ、长度调整、热力图
python src/report.py --full    # 出表出图到 results/（SUMMARY.md + 5 图 + 原始表）
python src/verify_all.py       # 一键跑完下面所有门（CI 入口）
```

`verify_all.py` 内部依次执行：

```bash
python src/schema.py --strict                 # ERROR 0 且 WARN 0
python src/dictionaries.py --check-sync       # 词典与 error_taxonomy.md §2 零漂移
python src/mock_models.py --clean             # 阳性 + 阴性对照
python src/rule_check.py --self-test          # 三条验收门
python src/score_audit.py --self-test         # 审计器自身的 19 个阳性用例
python src/score_audit.py --rubric-probe      # rubric 查表无空洞、无重叠
python src/stats.py --self-test               # α/κ/bootstrap 的已知答案夹具
python src/blind_shuffle.py --make-demo && python src/blind_shuffle.py && \
    python src/blind_shuffle.py --pass2 --retest-questions 8
python src/llm_judge.py --emit pointwise --source demo
python src/llm_judge.py --parse --judge-model DeepSeek-V3.2   # 必须被拒（退出码 1）
python src/report.py --demo --full            # DEMO 报告渲染
```

**验收标准（阶段 3 的硬性准出）**：
- 规则层对 mock 注入缺陷的 **recall > 0**，且逐类型报告；**严格 recall 与归码等价 recall 分开报**（E1a/E9 的归码优先级见 error_taxonomy §0 原则 2）；
- `mock_clean`（零缺陷对照）的 `confirmed` 数 **必须为 0**——若不为 0，说明规则层在误报，检测线索要重写；
- **无越权升级**：文档规定只能 `flag_for_review` 的错误码（E1a/E1c/E3/E4b/E6/E10/E2d），代码不得输出 `confirmed`；
- **无阳性对照的错误码必须显式列出**（当前为 E10/E11/E1c/E2b/E3/E4b/E6；E2d 自 2026-10-08 改判后有 7 例对照）。把"没测"说成"通过"是最不允许的事；
- rubric 查表探针报告「无空洞、无重叠」。若出现新的空洞/重叠，**改 rubric 的锚点，不改分数**；
- 人工 vs judge 的 α / weighted κ 逐维度报告，**无论高低都如实写**。κ 低不是失败，是发现；judge 的系统偏差（judge − 人工）必须与 α 并列报告。

> **阳性对照必须同时有阴性对照。** 只有缺陷注入（阳性）没有零缺陷对照（阴性），规则层会朝着"什么都报"的方向退化，而这种退化在只看 recall 时完全隐形。本项目曾因此发现 3 类假阳性（E5 的「绝对禁忌」、E7 的免责声明、E9 的归因窗口），全部登记在 deviation_log。

---

## 3. 偏差控制对照表（谁在哪一步被处理）

| 偏差 | 处理步骤 | 验证方式 |
|---|---|---|
| 位置偏置 | [9] A/B 互换 | 位置敏感对数 |
| 长度偏置 | [3] 记录字数 → [10] 长度调整 | 调整前后胜率对照表 |
| 自我偏好 | [0] judge 选面板外模型 | judge 型号留痕；若被迫用面板内模型则作废其自评并报差值 |
| 数据污染 | [1] `contamination_risk` 标注 | 高/低污染分层报分 |
| 单评分者 | [6] R2 子集 + [7] 重测 | α / weighted κ |
| 相对比较退化 | [4] 同题分入不同 block | 脚本强制校验并打印 `[OK]` |
| 提示词不对称 | [3] 逐字相同模板 | `answer_wrapper.md` 固定文本 |
| 版本漂移 | [3] 元数据留痕 | `manifest.json` + `meta` 字段 |
| 格式依赖 | [3] 6 题格式探针 | 排序是否翻转 |
| 评分者锚定于清单 | [5] 强制清单外观察 | §4 回收流程 |
| 数值口径不统一 | [1] 题库固定口径 | rubric"任一公认口径均算对" |
| 红旗 hint 极性歧义（把正确答案判成漏报） | [1] `hint_polarity` 显式标注 + [10] 规则层按极性分流 | `rule_check --self-test` 阴性对照 confirmed=0；`docs/_hint_polarity_review.md` 人工复核 |
| 阳性对照自毁（注入器互相删除证据） | [10] `INJECT_ORDER` 破坏性注入先跑 + 真值对最终文本复核 | 自检单独报「注入失败数」，失败者移出 recall 分母 |
| rubric 锚点空洞/重叠（评分者被迫临场发明规则） | [5] `score_audit --rubric-probe` 穷举中间量组合 | 探针报告「无空洞、无重叠」；发现即改 rubric 并登记 |
| judge 同源自我偏好 | [9] 型号名 + 厂商 token 双重校验 | 5 个面板家族全部验证被拒（含带版本号写法） |
| 盲测期产物破盲 | [5] rule/judge 产物独立目录并 gitignore；judge inbox 文件名用哈希槽号 | 对 inbox 文件名 grep 面板模型名 = 0 命中 |
| 无证据判定混入结论 | [9] judge 每条判定须带逐字 quote，程序做子串校验 | 丢弃率单独报；无证据支撑的维度标 `unsupported` |

---

## 4. 偏离登记（deviation log）

**任何对本 protocol 的偏离都必须记录**，写在 `docs/deviation_log.md`：日期、偏离了哪一条、为什么、对结论的影响评估。

> 这一条不是官僚程序。评测项目最常见的失效方式不是造假，而是**中途悄悄改了口径却没记录**，最后得到的数字谁也不知道在测什么。有了偏离登记，读者可以自己判断哪些结论仍然可信。临床试验有 GCP，评测项目也该有它的简化版。

---

## 5. 数据卫生与安全边界（硬约束，不可协商）

1. **不使用任何真实患者数据**。全部题目为去标识化教学式或合成病例；数值组合刻意非典型。`schema.py` 会扫描 `vignette` 中的身份证号、病历号、精确出生日期模式并直接报错。
2. **原始临床影像数据目录只读**。本项目全部脚本与产出写在 `clinical-eval-suite/` 内，不读入、不复制、不上传任何外部临床数据。
3. **仓库与脚本内不含任何 API 密钥**。`.gitignore` 已排除 `.env` / `*.key` / `secrets.*`；所有模型交互通过手动粘贴完成，代码里没有任何网络请求。
4. **mock 数据物理隔离**：`mock_responses.jsonl` 与 `demo_responses.jsonl` 独立存放，`blind_shuffle.load_raw()` 若在 `responses.jsonl` 中检出 mock 行会**直接终止**。
5. **仓库定位声明**：README 首行写明"本项目为方法学作品集，不构成医疗建议，不用于任何临床决策"。

---

## 6. 时间线（起算 2026-10-01）

| 日 | 步骤 | 需要你做的事 | 耗时 |
|---|---|---|---|
| D1 10-01 | 阶段 0 评审 | 已完成 | — |
| D2 10-02 | [1] 8 道样题 + rubric 定稿 | 评审临床正确性 | 你约 40 分钟 |
| D3 10-03 | [1] 补齐 20 题 + 取回工具 | 再评审 12 题 | 你约 50 分钟 |
| D4–D5 10-04/05 | [3] 取回 130 份回答 | 手动粘贴 | 约 2.5 小时 |
| D5 10-05 | [4] 匿名化 | 跑一条命令 | 1 分钟 |
| D5–D7 10-05~07 | [5] R1 首轮打分 | 6 个 block | 约 3.5 小时 |
| D6 10-06 | [6] R2 打分 | 协调 1 位同学 | 他约 1 小时 |
| D7 10-07 | [9] judge pointwise | 手动粘贴 100 次 | 约 1 小时 |
| D8 10-08 | [7] 重测（距 [5] ≥72h） | 24 份 × 6 维 | 约 50 分钟 |
| D8 10-08 | [9] judge pairwise | 手动粘贴 120 次 | 约 1.2 小时 |
| D9 10-09 | [10] 统计与图 | 跑命令 + 审读 | 你约 30 分钟 |
| D10 10-10 | 阶段 4 README + 讲稿 | 终审 | 你约 30 分钟 |

**你的总投入约 10–11 小时**，其中约 5 小时是机械粘贴、约 5 小时是打分、约 1 小时是评审与终审。

**可以提前投递的时点**：D3 结束时（10-03）仓库已有 20 题 + 完整 rubric + protocol + 可跑脚本，**这一天起就能在网申里填 GitHub 链接**，赶得上数坤 10/19、中金 10/28、罗氏诊断 10/30。不必等统计做完。

