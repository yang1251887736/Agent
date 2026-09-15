# agent-arena

> Agent 编排策略对比实验平台

**一句话**：自建轻量 Agent Runtime（无框架、约 1100 行），实现单 Agent / 主从
SubAgent / 并行 Teams 三种编排策略，在 60 条代码分析任务上用**断言式判定**
系统对比它们的完成率、错误调用率、token 消耗、耗时、上下文峰值，量化各自适用边界。

---

## 为什么做这个

业界对 Agent 编排存在**直接冲突**的观点：

| 来源 | 观点 |
|---|---|
| Anthropic《Building a multi-agent research system》 | 并行 multi-agent 让复杂查询耗时最多减少 90%；内部研究评估提升 90.2% |
| pi-mono 作者 Mario Zechner | 并行子代理是 anti-pattern —— "除非你不在乎你的代码库变成一堆垃圾" |

两边都是一线从业者，都拿得出数据。这个项目不站队，**用自己跑出来的数据回答**。

两种观点其实共享一个前提：**上下文隔离有价值**。
分歧在并行的可控性 —— 你能不能看见子 agent 在干嘛、两个 agent 改同一个文件怎么办。

---

## 快速开始

```bash
cd agent-arena
pip install -r requirements.txt

# 配置 key（DeepSeek / 任何 OpenAI 兼容服务）
cp .env.example .env      # 然后填 OPENAI_API_KEY

# 1. 自检评测集（每条断言的符号是否真实存在于仓库）—— 这个必须先过
python -m eval.build_tasks --check

# 2. 核心循环单条自测
python -m runtime.loop

# 3. 循环验收测试（假 LLM，不花钱，21 项）
python -m tests.test_loop

# 4. 小规模试跑（3 条 × 3 策略，约 1.5 分钟）
python -m eval.run --type A --limit 3 --tag pilot

# 5. 全量（3 策略 × 60 任务，约 30-60 分钟）
python -m eval.run --tag full
```

Windows 下如果 `python` 指向的解释器没装依赖，用绝对路径：
```
C:\Users\LEGION\.workbuddy\binaries\python\envs\default\Scripts\python.exe -m eval.run
```

---

## 语料

`psf/requests`，固定 commit `dae7ef63b4df6eded86637f251fc4e3a06c3b479`。

19 个 Python 文件 / 6394 行 / 320 个符号。选它的理由：模块边界清楚，
`Session` / `Adapter` / `Models` 分层天然适合出多跳题；规模适中 ——
agent 能穷举，人能逐条核对答案。

实验用 `sandbox/requests_src/` 下的**副本**，不碰原仓库。

---

## 目录结构

```
agent-arena/
├── runtime/                  # 自建 runtime（约 1100 行，无框架）
│   ├── loop.py               # 【核心】Agent 循环 —— 整个项目唯一的核心文件
│   ├── tools.py              # 6 个工具 + 菜单（定义与实现同文件，避免签名漂移）
│   ├── validate.py           # 参数校验 —— 借鉴 pi-mono
│   ├── llm.py                # LLM 调用封装
│   └── tracer.py             # SQLite 执行追踪 —— 所有实验数字的来源
├── orchestrators/            # 三种编排（同一套基础工具，只有编排是变量）
│   ├── single.py             # 基线：一条 messages 堆到底
│   ├── subagent.py           # 主 agent 串行派生子 agent，只回收结论
│   └── teams.py              # 拆任务 → 并行 worker（独立副本）→ 汇总
├── eval/
│   ├── build_tasks.py        # 评测集定义 + 自检器
│   ├── tasks.jsonl           # 生成的 60 条任务（由 build_tasks 产出）
│   ├── judge.py              # 断言式判定（不用 LLM 打分）
│   ├── run.py                # 批量跑 + 出对比表
│   └── rejudge.py            # 离线重判定（答案改了判定逻辑可随时重跑）
├── tests/
│   └── test_loop.py          # 核心循环验收（假 LLM，21 项）
├── sandbox/requests_src/     # requests 的可丢弃副本（评测语料）
└── results/                  # runs.db（180 次实验数据）+ analysis.txt（对比表）
```

---

## 实验设计

### 控制变量 —— 公平性的前提

三种编排共用：**同一份任务集、同一个模型、同一个 temperature(0)、同一套基础工具、
同一个 max_turns、同一个仓库 commit**。唯一的变量是编排方式。

subagent 多一个 `spawn` 工具、teams 多一层任务规划 —— 这些「多出来的能力」
正是被测试的对象本身，不算额外变量。

以上保证了对比只比较「编排方式」这一个变量。

### 任务集：60 条，三类各 20 条

| 类 | 特征 | 例子 |
|---|---|---|
| **A** 精确查找 | 问精确符号在哪，答案唯一 | 「重定向时判断要不要去掉 Authorization 的逻辑在哪个函数？」 |
| **B** 语义理解 | 问流程/机制，题目里**不出现任何符号名** | 「timeout 参数是怎么一层层传到底层的？」 |
| **C** 多跳推理 | 答案分散在三处以上，或需要反向追踪 | 「headers 一共被修改过几次？分别在哪些函数里？」 |

B 类的设计要点：题目里一个符号名都不给。这测的是「从功能描述反推实现位置」的能力，
跟 A 类正好相反 —— 两类任务分别对应两种检索方式的主场。

### 判定：断言，不用 LLM 打分

```json
{"id":"A01","type":"A","question":"...",
 "checks":{"must_contain":["should_strip_auth","sessions.py"],
           "must_not_contain":["models.py","adapters.py"]},
 "reference":"requests/sessions.py:154 should_strip_auth()",
 "why":"精确符号定位。函数名是生造的组合词，语义检索很难命中。"}
```

**为什么不用 LLM 当裁判**：

|  | LLM 打分 | 断言 |
|---|---|---|
| 可复现 | ❌ 同一答案两次可能不同 | ✅ 永远一致 |
| 成本 | 每条还要再花一次调用 | ✅ 零 |
| 可审计 | ❌ "模型觉得它对" | ✅ 能指出缺了哪个符号 |

更根本的理由：**判定标准跑之前就定死，跑完之后改不了**。
用 LLM 打分的话，你可以反复调评分 prompt 直到结果好看 ——
那样实验就不叫实验了。

### 评测集自带自检

`eval/build_tasks.py` 在生成 `tasks.jsonl` 前会逐条校验：

- 断言里写的符号，在仓库里真的存在吗？
- 符号真的在断言指定的那个文件里吗？（防止张冠李戴）
- 正则能编译吗？id 重复吗？

校验不过就**拒绝生成**。这条很重要：标准答案写错一条，
就会有一个任务无论 agent 答得多好都判不及格 ——
而且完全看不出来，因为「agent 没答对」和「标准答案错了」
在结果上长得一模一样。

### 五个指标

| 指标 | 来源 | 说明 |
|---|---|---|
| 任务完成率 | 断言判定 | 基础 |
| **错误调用率** | bad_calls / tool_calls | 工具不存在、参数错、执行失败 |
| token 消耗 | API 的 `usage` | 成本维度 |
| wall-clock 耗时 | 实测 | 并行的收益体现在这里 |
| **峰值上下文** | messages 字符数 | 衡量「决策者的上下文有没有被撑爆」 |



`peak_context_chars` ：它只记主 / lead 的 —— 那个指标衡量的正是
隔离有没有起作用，把 worker 的算进来就测不出来了。

---

## 三种编排

### single —— 基线

一个 agent，一套工具，一路跑到底。所有上下文堆在同一条 messages 里。

**失效模式**：每一轮的工具输出都永久留在 messages 里。任务越复杂噪音堆得越厚，
模型要在几千行历史里找自己刚查到的东西，容易被自己的中间结果带偏。

### subagent —— 串行派生，只回收结论

主 agent 拿到 `spawn` 工具，可以把一段探索整体外包出去，只拿回结论：

```
主 agent:  spawn("追踪 timeout 参数的传递路径")
               ↓ 子 agent 内部：grep -> read -> grep -> read（8 轮，几千字符）
               ↓ 只返回 60 个字符的结论
主 agent:  上下文只多了 60 个字符
```

三个必须做对的地方：

1. **子 agent 的工具集要剔除 spawn** —— 否则会无限派生下去烧钱。
   限制 agent 的能力边界，往往比扩展它的能力更能提升系统可靠性。
2. **子 agent 的 prompt 必须写死「只返回结论」** ——
   不写它会把 8 轮过程原样吐回来，隔离白做。
3. **子 agent 的开销要计入总账** —— 否则 subagent 会显得「又便宜又好」，
   那是不可能三角，一定是账算错了。**这条代码只做到了一半**：
   Ledger 把子 agent 的用量累加回了内存，但没落库（见上面的成本口径）。

### teams —— 拆分后并行，独立副本隔离

```
① plan    lead 把任务拆成 N 个独立子任务（一次 LLM 调用，输出 JSON）
② work    N 个 worker 并行跑，各自独立的代码库副本
③ merge   lead 综合全部结论后 submit
```

**为什么 worker 要用独立副本**：两个 worker 同时改同一个文件，后写的覆盖先写的，
而且这种冲突随机出现，取决于线程调度，极难复现。

这不是过度设计。pi-mono 作者反对并行子代理，核心论据就是合并成本 ——
他用 git worktree 隔离（每个 agent 一个 worktree），这里用文件副本，原理一样。

**为什么 `REPO_ROOT` 是线程局部的**（`runtime/tools.py`）：
并行的 worker 各持有自己的副本，如果是普通全局变量，
A 线程刚设成自己的副本，B 线程一设，A 的后续操作就全跑到 B 的副本上去了。
这种 bug 取决于线程调度，随机复现。用 `threading.local` 后每个线程看到自己那份。

---

## 核心循环的设计

`runtime/loop.py` 是整个项目唯一的核心文件。几个关键决策：

**① 没有 tool_calls 时不算完成。**
模型「只是说话」不算完成 —— 它必须调 `submit`。否则会出现很隐蔽的假成功：
模型输出一段看起来像答案的文字，循环以为它说完了就退出，判成成功，
但它根本没定位到正确位置。所以这里会 nudge 它，连续 3 次不提交才判失败。

**② 所有异常都变成 tool 消息，绝不冒出循环。**
工具不存在、参数类型错、执行抛异常 —— 全部包装成一条 `role=tool` 消息喂回模型。
模型看到「不存在名为 X 的工具，可用的是：...」，下一轮多半就自己改对了。
抛异常的话整个 agent 直接死掉，前面十几轮的 token 全白烧。

**③ `submit` 用 terminate 机制**（借鉴 pi-mono）。
让工具自己决定「行了别再跑了」，而不是在循环外面加特判。
它也是唯一出口：不调 submit 就一直循环到超时判失败。

**④ `max_turns = 20` 不是拍脑袋定的。**
跑完试点看轮次分布，90% 在 8 轮内完成，最长 13 轮，20 轮留出约 55% 余量，
同时保证跑偏的任务不会无限空转。改这个值之前先看：
```sql
SELECT strategy, MAX(n_turns), AVG(n_turns) FROM runs GROUP BY strategy
```

**⑤ 没有对 `call_llm` 做 try/except，是刻意的。**
它内部已经退避重试 3 次了，还失败说明是网络/鉴权级硬故障。
与其吞掉异常让实验静默变脏，不如让它冒出来，你立刻能看见。

### 借鉴 pi-mono 的三处设计

参考 pi-mono 的 `agent-core`（1966 行，其中真正的循环只有 64 行）实现：

| # | 借鉴什么 | 为什么 |
|---|---|---|
| 1 | 参数校验 | 模型给的参数经常是瞎编的，不校验直接 TypeError 炸掉整个循环 |
| 2 | 错误转反馈 | 把模型的错误翻译成模型能读懂的反馈，不是终止进程 |
| 3 | terminate | 让工具决定何时结束 |

**没借鉴的**：双层循环 + steering/follow-up 插话队列。
这是批处理评测场景，没有「用户中途插话」这个需求 —— 做了就是没有需求的复杂度。

---

## 结果

**环境**：DeepSeek（temperature 0）× `psf/requests` @ `dae7ef63`，
共 **180 次运行**（3 策略 × 60 任务），**0 次崩溃**。
180 条全部经 `eval/rejudge.py` 统一重判（消除 run 崩溃时的漏判），最终通过 **176 / 180**。

原始数据：`results/runs.db` ｜ 逐条明细：`results/round_full2.csv` ｜ 分析表：`results/analysis.txt`

### 总览

| 策略 | 完成率 | 提交率 | 平均轮次 | 工具调用 | 错误调用率 | **token/任务** | 峰值上下文 | 耗时/任务 |
|---|---|---|---|---|---|---|---|---|
| single | 98.3% | 100% | 5.4 | 9.0 | 0.0% | **20,100** | 15,381 | **7.9s** |
| subagent | 98.3% | 100% | 3.5 | 5.0 | 0.7% | 34,354 | **10,286** | 13.7s |
| teams | 96.7% | 100% | 2.8 | 5.1 | 0.0% | **101,990** | 20,006 | 8.0s |

> **完成率一栏不要当真**——三种编排都在 95% 以上，差距落在判定噪音范围内（见局限 2）。
> **token 一列是「完整口径」**（含子 agent / worker），也是本节唯一能横向比的成本数字。


| 策略 | 主 agent（按轮去重） | **完整（含子 agent）** | vs single |
|---|---|---|---|
| single | 20,100 | **20,100** | 1.00× |
| subagent | 11,075 | **34,354** | **1.71×** |
| teams | 18,595 | **101,990** | **5.07×** |

两列的差就是子 agent 的开销，也是本项目的核心发现：
**subagent 让主机省了 45%，却让总账涨到 1.71 倍。**

**怎么验证这个拆分是对的**：拿 CSV 的 120 行逐行减去「主 agent（去重）」——
**没有一行是负数**，并且其中 **25 行严格等于 0**。这 25 行全部是没有触发 spawn 的
subagent 运行：既然一次子 agent 都没派，内存口径和主 agent 口径**必须相等**。
它们确实相等，说明两边都没算错。

`peak_context_chars` 不走这条路（`finish_run` 取的是 `MAX(context_chars)`，一轮一个值），
**不受这两个 bug 影响**，可以直接引用。

### 一句话结论

**在这批任务上，三种编排的正确率没有实质差异；真正的差异在成本与延迟，
而这两条上赢的都是 single——subagent 唯一买到的是更小的决策者上下文
（15,381 → 10,286，−33%），代价是 1.71 倍的 token 和 1.7 倍的延迟。**

### 边界条件：什么场景用哪个

| 任务类型 | 完整 token/任务<br>single / subagent / teams | 最优解 | 依据 |
|---|---|---|---|
| **A 精确查找** | 7,650 / 13,458 / 58,354 | **single** | single 100% / 4.4s / token 最低；subagent 要 1.76× token 且 6.8s |
| **B 语义理解** | 22,134 / 36,989 / 113,623 | **single**（正确率看 subagent）| single token 最低但掉 1 条（95%）；subagent 100% 却贵 67% |
| **C 多跳推理** | 30,517 / 52,616 / 133,993 | **single** | single 100% 且 token 最低；teams 贵 4.4× 也掉 1 条 |

**三类任务上 single 的完整 token 都是最低的**，倍率稳定在 subagent 1.67–1.76×、
teams 4.39–7.63×。**倍率不随任务难度变化**——说明这是结构性开销，
不是「某类任务不适合派子 agent」。

唯一值得为 subagent 付溢价的地方是**决策者的上下文不被污染**：
A/B/C 三类它的峰值上下文都更低（6,362 / 10,353 / 14,143，single 是 7,380 / 16,543 / 22,219）。

### 三条可迁移的结论

**① 子 agent 省的是「决策者的上下文」，不是「token」。**
subagent 的主 agent 用量确实从 20,100 掉到 11,075（**−45%**），峰值上下文也从
15,381 降到 10,286（**−33%**）——因为子 agent 那几轮探索没进主上下文，
主 agent 只看到一句结论。
但**工作没有消失，只是搬了家**：子 agent 得自己 grep、自己 read，
原始文件内容照样要进某个上下文，而且搬去的那个上下文**更挤**——
子 agent 每轮 5,543 token，比主 agent 的 3,179 贵 74%。一进一出，
总账 34,354，是 single 的 **1.71 倍**。代价还包括延迟：spawn 是同步阻塞的，
主 agent 必须等子 agent 跑完，13.7s vs 7.9s。

**② 「teams 一定更贵」不是错觉，它是这批数据里最贵的选择。**
teams 的完整 token 是 single 的 **5.07 倍**（101,990 vs 20,100），其中 **81.8%** 花在
worker 和规划上。A 类最刺眼：**7.63 倍**——只是定位一个符号在哪，
却要走「规划 → 3 个 worker → 汇总」四道 LLM 调用。
并行只抵消了墙钟时间（8.0s ≈ single 的 7.9s），**抵消不了 token**。
（`--workers` 默认 3，`eval/run.py:90`。）

**③ 简单任务上多 agent 是纯亏。**
A 类精确查找：single 4.4s / 7,650 token 就做完；subagent 贵 1.76 倍还慢 55%，
teams 贵 7.63 倍并且掉了一条。
**能一个 agent 干完的活，别拆。**


### 一个反直觉的发现：「自信地答错」

提交成功、却判定失败的比例——**teams 最高（3.3%，2/60）**，single/subagent 各 1.7%。

拆解 + 汇总会放大主 agent 的过度自信：worker 各报一段局部结论，lead 汇总时容易把「拼起来的片段」当成完整答案直接提交。
**并行度越高，越需要一道「结论是否覆盖了所有子问题」的自检。**

### 局限（诚实清单）

**1. 天花板效应。** 60 条任务三种编排都做到 95%+，正确率维度区分度不足——要拉开差距需要更难的任务集。上面的 96.7% vs 98.3% **不要当成真实差异**。

**2. 错误模式统计不完整。** subagent 的子 agent、teams 的 worker 不写 `turns` 表，所以「错误调用模式」一节只覆盖主 agent。

**3. 单模型、单语料、单次运行。** 仅 DeepSeek 一个模型、requests 一个仓库、未做重复实验（无方差估计）。外推到别的模型或领域需谨慎。

### 复现

```bash
python -m eval.build_tasks --check                    # 评测集自检（符号真实性）
python -m eval.run --tag full                          # 180 次运行
python -m eval.rejudge                                  # 统一重判（默认 results/runs.db）
python -m eval.analyze                                  # 出对比表（默认 results/runs.db）
python -m eval.analyze --csv results/round_full2.csv     # 【9】完整口径要读这份明细 CSV
```
