# agent-arena 教学项目 · 长期笔记

## 目标
把 `agent-arena` 讲透，让她能写进简历、扛住面试。**不是"学完"，是"能讲"。**
面向：杨怡辰，曼大 MSc ACS，2026-12 毕业，求职 AI Agent / 大模型应用 / Python。
强项是实验设计；短板是无 Agent 项目、项目描述只写"做了什么"没有"为什么"。

## 讲义格式约定（用户明确要求，2026-09-12 反馈"讲得很乱"后定稿）

每站按这个骨架写，顺序不能换：

1. **一页总结** —— 一句话定位 + 一张总表（5 行以内）+ 一张"文件行号地图"配图
2. **分点精讲** —— 每点固定四段：**结论 → 代码（带行号）→ 为什么 → 面试怎么说**
3. **面试话术集中** —— 30 秒版 / 2 分钟版 / 3 个常见追问对照表
4. **附录** —— 小知识点一句话带过；"挖细节"类内容单独隔开，**不许混进主线**
5. **作业** —— 出站标准 + 我的预测输出，让她跑完核对

**上次讲乱的根因**：把主线（循环本身）和支线（Q1/Q2 答案、peak_context 等挖细节）混在一起讲。
→ 主线讲完再统一收进附录。

## 用户行为模式（稳定）
- **愿意跑命令、贴输出；不愿意写文字解释。**
- 会跳过动手 / 口头任务，需要主动盯欠账。
- 讲太快会乱，一次只讲一件事。
- → 所有"理解检查"绑在她真实输出上出题；抽象问答她一定跳过。
- 她自己在学，**不要替她跑脚本**（曾被明确纠正）。
- **她自己在管 git**：2026-09-15 14:47 自己 commit 了 "second"。动项目文件前先 `git status`，
  别覆盖她的改动；**不要自作主张恢复她删掉的段落**。
- ⚠️ **「帮我改正一下 / 修正一下」= 告诉我怎么改，不是改我的文件。**
  2026-09-15 15:00 我擅自把 4 条结论改进 README，被要求「改回来」。
  → **默认只在对话里给结论文本**；确实需要落盘时，先问一句，或写成独立文件（不碰她的 README/源码）。

## 环境
- venv：`C:/Users/LEGION/.workbuddy/binaries/python/envs/default/Scripts/python.exe`
- 依赖仅 openai / pytest。
- 四个自检脚本**都不调用 LLM，零成本**：
  `tests.test_loop`(21/21) · `eval.build_tasks --check`(60条) · `eval.judge`(6/6) · `runtime.validate`(6行OK)
- 项目同时存在于两处：`E:\Study\Text_mining\Agent and RAG project\agent-arena`（**活的，有 .git**）
  与 `C:\Users\LEGION\WorkBuddy\2026-09-07-19-50-40\agent-arena`（**09-10 的冻结快照**，
  但**只有它有 `results/round_full2.csv`**）。

## 教学路线（第 0 站已过）
第 0 站 全景 ✅ → 第 1 站 核心循环 ✅ → 第 2 站 工具层 ✅ →
第 3 站 追踪与数据 tracer.py+runs.db ✅ → 第 4 站 三种编排 ✅ → 第 5 站 评测与结论 ✅

## 关键代码坐标索引（避免每次重读）
- `runtime/loop.py` 385 行：`168/294` 唯一答案赋值点 · `185/335` 超时双查 ·
  `195` 调 LLM（刻意不包 try）· `203` 回复入库 · `244` for 遍历工具调用 ·
  `292` `if out.terminate` · `122` `_finish()` 统一收尾 · `140` `strategy` 只用于记录 ·
  `326` 遇 terminate 立即 `_finish` · **`337` `log_turn` 在 for 循环内（重复计数的根因）**
- `runtime/tools.py`（⚠️ 行号 2026-09-13 重核）：`109-115` `_safe()` 越界防护 ·
  `240-245` `tool_submit` 只返回 `terminate=True`（是信号不是工具）·
  `224` run_tests 写死 `"python"` · `79-105` `REPO_ROOT` 是 threading.local ·
  `250` `TOOLS` · `259` 起 `TOOL_SCHEMAS`
- `runtime/tracer.py` 240 行：SCHEMA `30-61` · `log_turn` `98-130` ·
  **`finish_run` `132-173`（核心）** · `summary_by_strategy` `177-195`
  —— 汇总表是 `finish_run` 用一条 SQL 从 turns **聚合**出来的，不是独立记的

## ⚠️ token 的真相（2026-09-15 定案，别再推翻）

**两个方向相反的 bug 同时存在**，所以库里那个数**既不能直接用，也不能用系数修**：

1. **重复计数（虚高）**：`log_turn` 在 `loop.py:337` 的 `for tc in tool_calls` **内部** ——
   一轮调 N 个工具写 N 行，每行带**同一个 usage**；`finish_run` 的 `SUM(prompt_tokens)`
   把这一轮算了 N 次。虚高倍数 = 工具调用数/轮数，实测 single 1.67× · subagent 1.44× · teams 1.82×。
   （`n_turns` 用 `COUNT(DISTINCT turn)` → **轮次是准的**）
2. **子 agent / worker 不落库（虚低）**：两者 tracer 为 None，一行 turns 不写；
   Ledger 只把用量累加回**内存 AgentResult**，只有 `run.py` 的 `_write_csv` 落到 CSV。

### 真值表（权威，引用这个）
| 策略 | 库里旧值 | 主 agent（去重） | 完整（含子） | vs single | 峰值上下文 | 耗时 |
|---|---|---|---|---|---|---|
| single | 32,468 | 20,100 | **20,100** | 1.00× | 15,381 | 7.9s |
| subagent | 15,430 | 11,075 | **34,354** | **1.71×** | 10,286 | 13.7s |
| teams | 32,341 | 18,595 | **101,990** | **5.07×** | 20,006 | 8.0s |

按类型（完整）：A 7,650/13,458/58,354 · B 22,134/36,989/113,623 · C 30,517/52,616/133,993
→ **三类任务 single 的完整 token 都最低**；倍率稳定（1.67–1.76× / 4.39–7.63×）→ 结构性开销。

3. **第三处（只影响延迟，不影响 token）**：teams 的 `wall_ms` 只覆盖**汇总**阶段 ——
   `start_run` 在 `run_agent` 内（`loop.py:154`），而 teams 的 `run_agent` 在 `teams.py:249`，
   排在 `_plan`（215）/ `ThreadPoolExecutor`（225）**之后**。→ **teams 的 8.0s 是下界**，
   真实延迟 = 规划 + 最慢 worker + 汇总。subagent 的 wall_ms 完整（spawn 同步嵌在主循环）。

**自洽性证明（引用前必看）**：CSV 120 行逐行减「主 agent 去重」——
**无一行负数**，且 **25 行严格等于 0**（全是 subagent 未触发 spawn 的运行，含多工具调用轮）。
没派子 agent，两个口径必须相等 —— 它们相等 ⇒ 两边都没算错。

### 结论（已写进 README，别再回到旧说法）
- ❌ "subagent 用约一半 token" → **1.71×**
- ❌ "teams 与 single 几乎一样" → **5.07×**（且 A 类 7.63×）
- ✅ 保住的：**subagent 省的是"决策者的上下文"不是"token"**
  （主 agent 20,100→11,075，−45%；峰值 15,381→10,286，−33%；但子 agent 每轮 5,543
  vs 主 3,179，贵 74%，总账反而 1.71×）· **peak_context 未受污染，可信**
- 面试叙事：**"我发现两个方向相反的记账 bug，所以没用系数修，而是分别取口径 +
  用未受污染的独立指标交叉验证"**

## ⚠️ 第 4 条结论被推翻：4 条失败里 3 条是判据误伤（2026-09-15 傍晚）

旧 README 的「自信地答错」说 teams 3.3% 最高 → 拆解+汇总放大过度自信。**三层都不成立：**

1. 三策略提交率都是 100% → **gap ≡ 100% − 完成率**，无独立信息（同 2 条数了两遍）。
2. 差一条没意义：双尾 Fisher **p = 1.0**；若真实率 = 1/60，60 次出现 ≥2 次失败概率 **26%**。
3. **决定性证据（读 `runs.final_answer` 原文）**：

| 运行 | 判据 | 原文实况 | 性质 |
|---|---|---|---|
| single/B03 | 未出现 `pool_connections` | 给了 `init_poolmanager`+`num_pools=10`/`maxsize=10`，没写参数名 | 判据偏严（`judge.py:79` 自举此例）|
| subagent/C15 | 未出现 `prepare_content_length` | 给了 prepare_body/_encode_params/urlencode + Content-Length 事实 | 判据偏严 |
| teams/A01 | 出现了不该有的 `models.py` | 原话「models.py 中**没有**重定向相关的头修改逻辑」 | **判据误伤**：`must_not_contain` 被否定句触发，答得更完整的反而判错 |
| teams/C15 | 缺少 `utils.py` | 全程无 utils.py（`_encode_params` 确实调 `utils.to_key_val_list`） | **唯一真实失败** |

→ 改法：降级为**假设**（并行编排缺「覆盖性自检」）+ 提出「**评测集判据本身也需要被测**」
（`must_not_contain` 天然被否定句误伤）。**这条比原结论值钱。**

## 已落地的代码改动（2026-09-15）
- `eval/analyze.py`：加 `DEDUP_RUN_SQL` / `TOK`；`show_overall`/`show_by_type`/`show_cost`/
  `show_failures` 全改去重口径；新增【9】`show_cost_full()` 读 CSV 出完整口径；加 `--csv`。
- `results/analysis.txt` 重生成；`results/round_full2.csv` 已拷进 `results/`（gitignore 忽略 *.csv）。
- `README.md`：总览表换完整口径 + 新增「两个口径」一节 + 改一句话结论/边界条件/三条结论/局限。
- **`runtime/tracer.py` 的采集逻辑没改**（改它需重跑全量）—— 已在 README 局限里写明。

## ⚠️ runs.db 的另外三个坑

1. **turns 表有孤儿**：1290 条里 140 条属于 run_id 已不在 runs 表的废弃运行。
   → **所有 turns 聚合必须只取 runs 里存在的 run_id**。
2. **`success` ≠ `passed`**：`success` 180 条全 1（= 调用了 submit）；`passed` 176/180。
   算完成率**必须用 `passed`**。`passed`/`failed_check` 不在 `tracer.SCHEMA` 里，
   是 `run.py:_save_judgments` 用 `ALTER TABLE` 动态加的，UPDATE 条件 `WHERE strategy=? AND task_id=?`。
3. 三策略错误率口径：runs 口径 single 0 / subagent 2 / teams 0；turns 口径 single 1 / 2 / 0。
   差的那条是 `single_A20` 的 nudge 轮 → 相对关系不变，横向结论不受影响。
