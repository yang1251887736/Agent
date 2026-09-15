"""策略二：Subagent —— 主 agent 派子 agent 干活，只回收结论。

## 它在解决什么问题

single 的毛病是「噪音只进不出」：每一次 grep 的 30 行结果、每一次 read_file 的
200 行，都永久留在主上下文里。子任务越多，主上下文越脏。

subagent 的做法：**把一段探索过程整体外包出去，只把结论拿回来。**

    主 agent:  spawn("找出 requests 里所有重定向相关的函数")
                   ↓ 子 agent 内部：grep -> read -> grep -> read（8 轮，几千字符）
                   ↓
               返回："should_strip_auth (sessions.py:154)、
                     resolve_redirects (sessions.py:307)"
    主 agent:  只多了 60 个字符

**子 agent 那 8 轮的中间过程，一个字都不进主上下文。** 这就是「上下文隔离」。

## 三个必须做对的地方

① **子 agent 的工具集要剔除 spawn。**
   否则子 agent 再派孙 agent，孙再派曾孙……你会在一觉醒来发现烧了一晚上。
   限制 agent 的能力边界，往往比扩展它的能力更能提升系统可靠性。

② **子 agent 的 prompt 必须写死「只返回结论」。**
   不写的话它会把 8 轮中间步骤原样吐回来，隔离白做。这是这个策略最容易翻车的地方。

③ **成本口径要统一。**
   子 agent 烧的 token 必须算进总账，否则 subagent 会显得「又便宜又好」——
   那是假象。所以这里用 ledger 把子 agent 的开销累加回主 result，
   但 **peak_context_chars 不累加** —— 那个指标衡量的正是
   「主上下文有没有被撑爆」，是本实验的核心观测量。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runtime.loop import run_agent, AgentResult, DEFAULT_MAX_TURNS  # noqa: E402
from runtime.tools import (TOOLS, TOOL_SCHEMAS, ToolOutput,          # noqa: E402
                           ToolError)

NAME = "subagent"

# ---------------------------------------------------------------- prompt

MAIN_PROMPT = """你是一个代码库分析助手，工作目录是一个 Python 代码仓库。

你有一个 spawn 工具，可以派一个子 agent 去独立完成一段探索，它只会把**结论**返回给你。

## 判断标准（按工具调用次数，这是硬性标准）

**预计需要 3 次以上工具调用才能回答 → 一律 spawn。**
**只需要 1-2 次工具调用 → 自己做。**

具体来说：

spawn 的情况（默认选择）：
- 要追踪一条调用链 / 参数传递路径（必然要 grep 好几次 + read 好几次）
- 要在多个文件里找东西、或者不确定答案在哪
- 要"列出所有……""有哪些地方……"这类穷举型问题
- 要理解一个流程 / 机制（得读完几个函数才能串起来）

自己做的情况（只有这两种）：
- 明确知道查一个符号在哪，一次 grep 就能定位
- 已经拿到子 agent 的结论，只是在此基础上做最后整合

## 例子

问「超时参数怎么一层层传下去的」→
  spawn("追踪 requests 中 timeout 参数从 api.request 到 HTTPAdapter.send 的传递路径")

问「重定向时剥 Authorization 头的函数叫什么」→
  自己 grep 一次就行，不用 spawn

## 其他

子 agent 任务描述要写清「要什么」和「到什么程度算够」。
可以派多个（并行无意义，串行即可），也可以拿到结论后再派一个更精确的。
信息够了就调用 submit 提交最终答案 —— 这是结束任务的唯一方式。
最终答案要具体：给出文件路径、函数名、行号。"""

SUB_PROMPT = """你是一个代码库分析子 agent，工作目录是一个 Python 代码仓库。

你会收到一个**明确的子任务**。你的输出会被交给主 agent 作为它的判断依据。

严格要求：
1. 只返回结论。不要描述你搜了什么、试了什么、排除了什么 —— 那些过程主 agent 不需要看。
2. 结论要具体：文件路径、函数名、行号、关键代码片段（只贴必要的几行）。
3. 信息够了就立刻调用 submit。不要为了"更完整"继续扩大搜索范围。
4. 如果子任务本身无法完成（比如要找的东西不存在），
   如实说明"未找到"，不要编造。

反例（不要这样）：
  "我先用 grep 搜索了 timeout，找到了 12 处，然后我读了 sessions.py 的
   request 方法，发现它调用了 self.send，接着我又去看 adapters.py……"

正例（要这样）：
  "timeout 传递路径：api.request(timeout) -> Session.request(self, timeout)
   -> Session.send(..., timeout=timeout) -> HTTPAdapter.send(timeout=timeout)。
   关键位置：api.py:60, sessions.py:590, sessions.py:700, adapters.py:558" """


# ---------------------------------------------------------------- 账本


class Ledger:
    """累计所有子 agent 的开销。

    为什么要单独记：子 agent 的 tracer 是 None（不进 runs 表，避免每任务多条记录
    把汇总表搞乱），但它的 token 和轮次必须算进总账，
    否则 subagent 会显得「又便宜又好」——那是不可能三角，一定是账算错了。
    """

    def __init__(self):
        self.n_spawns = 0
        self.n_turns = 0
        self.n_tool_calls = 0
        self.n_bad_calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.wall_ms = 0

    def absorb(self, r):
        self.n_turns += r.n_turns
        self.n_tool_calls += r.n_tool_calls
        self.n_bad_calls += r.n_bad_calls
        self.prompt_tokens += r.prompt_tokens
        self.completion_tokens += r.completion_tokens
        self.wall_ms += r.wall_ms


# ---------------------------------------------------------------- spawn 工具


def _sub_tools_and_schemas():
    """子 agent 能看到/能用的工具 —— 剔除 spawn，防止无限派生。

    注意 submit 保留：子 agent 要用它提交结论。
    这是子 agent 自己的 submit，跟主 agent 的那个是两回事 ——
    run_agent 遇到 terminate 就返回结果，不会一路 terminate 到主循环。
    """
    sub_tools = {k: v for k, v in TOOLS.items() if k != "spawn"}
    sub_schemas = [s for s in TOOL_SCHEMAS
                   if s["function"]["name"] != "spawn"]
    return sub_tools, sub_schemas


SPAWN_SCHEMA = {
    "type": "function",
    "function": {
        "name": "spawn",
        "description": (
            "派一个子 agent 独立完成一个子任务，它只把结论返回给你，"
            "搜索过程不会留在你的上下文里。"
            "适合需要多次探索才能收敛的问题；一两次调用就能查清的简单问题请自己做。"
            "子任务描述要写清楚「要什么」和「到什么程度算够」。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "交给子 agent 的明确任务描述",
                }
            },
            "required": ["task"],
        },
    },
}


def make_spawn(ledger, model=None, max_sub_turns=12, timeout_s=180):
    """构造 spawn 工具函数。

    用闭包而不是全局函数，是因为 spawn 要拿到 ledger 和 model 配置。
    """
    sub_tools, sub_schemas = _sub_tools_and_schemas()

    def spawn(task):
        ledger.n_spawns += 1
        idx = ledger.n_spawns
        r = run_agent(
            task,
            task_id=f"sub{idx}",
            strategy=NAME + "_sub",
            tracer=None,               # 不进 runs 表，开销走 ledger
            max_turns=max_sub_turns,
            timeout_s=timeout_s,
            system_prompt=SUB_PROMPT,
            tools=sub_tools,
            tool_schemas=sub_schemas,
            model=model,
        )
        ledger.absorb(r)
        if not r.success:
            return ToolOutput(
                f"子 agent #{idx} 未完成任务（原因：{r.error}）。"
                f"它目前得到的部分结论：{r.answer or '（无）'}\n"
                f"你可以换一种问法重新 spawn，或者自己查。",
                is_error=True,
            )
        return ToolOutput(f"[子 agent #{idx} 的结论]\n{r.answer}")

    return spawn


# ---------------------------------------------------------------- 入口


def run(task, task_id="", tracer=None, max_turns=DEFAULT_MAX_TURNS,
        timeout_s=420, model=None, max_sub_turns=12, **kw):
    ledger = Ledger()

    tools = dict(TOOLS)
    tools["spawn"] = make_spawn(ledger, model=model,
                                max_sub_turns=max_sub_turns,
                                timeout_s=int(timeout_s * 0.6)) # 创造spawn工具
    schemas = list(TOOL_SCHEMAS) + [SPAWN_SCHEMA]

    r = run_agent(
        task, task_id=task_id, strategy=NAME, tracer=tracer,
        max_turns=max_turns, timeout_s=timeout_s,
        system_prompt=MAIN_PROMPT,
        tools=tools, tool_schemas=schemas, model=model,
    )

    # 把子 agent 的开销并进总账（peak_context 除外 —— 那是主上下文的观测量）
    r.n_turns += ledger.n_turns
    r.n_tool_calls += ledger.n_tool_calls
    r.n_bad_calls += ledger.n_bad_calls
    r.prompt_tokens += ledger.prompt_tokens
    r.completion_tokens += ledger.completion_tokens
    r.n_spawns = ledger.n_spawns          # AgentResult 没有这个字段，动态挂上
    return r


if __name__ == "__main__":
    from runtime.tools import set_repo_root
    set_repo_root(os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "sandbox", "requests_src")))
    r = run("requests 里 Session 对象默认挂载了哪些 HTTP 适配器？分别在哪个文件？",
            task_id="DEMO")
    print(r)
    print("spawn 次数:", getattr(r, "n_spawns", 0))
    print(r.answer)
'''
subagent.py
    ↓
主 Agent 调 run_agent()
    ↓
主 Agent 调 spawn()
    ↓
spawn 又调用 run_agent()
    ↓
子 Agent 调 llm.py / validate.py / tools.py
    ↓
子 Agent submit
    ↓
返回 AgentResult
    ↓
Ledger 记成本
    ↓
只把 r.answer 作为 spawn 的 ToolOutput
    ↓
主 Agent 继续
'''