# Planner → 并行 Workers → Lead” 的多 Agent 编排器
"""策略三：Agent Teams —— 一次性拆分，并行推进，最后汇总。

## 和 subagent 的区别（这两个容易被混为一谈）

|  | subagent | teams |
|---|---|---|
| 调度 | **串行**：派一个，等结论，再决定下一个 | **并行**：一次拆 N 个，同时跑 |
| 依赖 | 后一个子任务可以依赖前一个的结果 | 子任务必须**相互独立** |
| 成本 | 较低（按需派生） | 较高（拆了就得全跑） |
| 耗时 | 累加 | 取决于最慢那个 |

Anthropic 那篇 blog 的数据：复杂查询耗时最多减少 **90%**，
但 token 消耗是单 agent 的 **15 倍**。
**用钱换时间** —— 本项目就是要拿数据验证这笔交易划不划算。

## 流程

    ① plan    lead 把任务拆成 N 个独立子任务（一次 LLM 调用，输出 JSON）
    ② work    N 个 worker 并行跑，各自**独立的代码库副本**
    ③ merge   lead 拿到全部结论，综合后 submit

## 为什么 worker 要用独立副本

两个 worker 同时调 edit_file 改同一个文件，后写的覆盖先写的 ——
而且这种冲突是随机出现的，取决于线程调度，极难复现和排查。

这不是过度设计：pi-mono 作者反对并行子代理，
核心论据就是合并成本。他用的是 git worktree 做隔离（每个 agent 一个 worktree），
这里用文件副本，原理一样 —— 先隔离，再合并。

## 成本口径

和 subagent 一样：worker 的开销全部计入总账，但 peak_context_chars 只记 lead 的。
理由：peak_context 衡量的是「决策者的上下文有没有被撑爆」，
worker 各自撑爆自己的不影响 lead 的判断。
"""

import json
import os
import shutil
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runtime.llm import call_llm                                   # noqa: E402
from runtime.loop import run_agent, AgentResult, DEFAULT_MAX_TURNS  # noqa: E402
from runtime.tools import TOOLS, TOOL_SCHEMAS, set_repo_root        # noqa: E402
from runtime.tools import current_repo_root                         # noqa: E402

NAME = "teams"

# ---------------------------------------------------------------- prompt

PLAN_PROMPT = """你是一个任务规划器。把一个代码库分析任务拆成若干个**相互独立**的子任务。

要求：
1. 拆成 2-4 个子任务。每个子任务必须能独立完成，不依赖其他子任务的结果。
2. 每个子任务的描述要自包含 —— worker 看不到原始任务，只能看到你写的这句描述。
   所以要把「查什么」「在哪查」「到什么程度算够」都写清楚。
3. 只输出 JSON，不要任何解释文字、不要 markdown 代码块标记。

输出格式：
{"subtasks": ["子任务1描述", "子任务2描述", "子任务3描述"]}"""

WORKER_PROMPT = """你是一个代码库分析员，工作目录是一个 Python 代码仓库。

你会收到一个明确的子任务。你的结论会被交给一个汇总者，
它手里还有其他人关于**同一大问题的不同侧面**的结论。

严格要求：
1. 只返回你这一路查到的结论。不要描述搜索过程。
2. 要具体：文件路径、函数名、行号、关键代码片段（只贴必要的几行）。
3. 信息够了立刻 submit，不要为了"更完整"扩大搜索范围。
4. 查不到就如实说"未找到"，不要编造。"""

MERGE_PROMPT = """你是汇总者。若干分析员已经并行完成了同一任务的不同侧面，
他们的结论在下面。

你的工作：
1. 综合这些结论，形成一份**完整、具体**的最终答案。
2. 如果某个侧面的结论缺失或矛盾，你可以自己查一下（find_files / grep / read_file）。
3. 不要重复罗列各分析员的结论原文 —— 要融合成一份连贯的答案。
4. 答案要给出文件路径、函数名、行号。
5. 完成后调用 submit —— 这是结束任务的唯一方式。"""

MERGE_TEMPLATE = """原始任务：
{task}

以下是各分析员的结论：

{findings}

请综合以上内容，给出最终答案，并调用 submit 提交。"""


# ---------------------------------------------------------------- 规划


def _plan(task, model=None, max_subtasks=3):
    """把任务拆成若干独立子任务。

    拆不出来（模型没吐合法 JSON）就降级成单任务 ——
    这时候 teams 退化成「一个 worker + 汇总」，仍然能跑完，不会崩。

    返回 (子任务列表, 原始回复, usage)。
    ⚠️ usage 必须带出来：规划本身是一次真实的 LLM 调用，花的钱要进总账。
    漏掉它会让 teams 显得比实际便宜（虽然只有一次调用的量级）。
    """
    reply = call_llm(
        messages=[
            {"role": "system", "content": PLAN_PROMPT},
            {"role": "user", "content": task},
        ],
        tools=None,
        model=model,
    )
    usage = reply.get("usage") or {}
    raw = (reply["content"] or "").strip()
    # 模型有时会自作主张包一层 ```json
    if raw.startswith("```"):
        raw = raw.strip("`").strip()
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
    try:
        data = json.loads(raw)
        subs = data.get("subtasks") or []
        subs = [s.strip() for s in subs if isinstance(s, str) and s.strip()]
    except (json.JSONDecodeError, AttributeError):
        subs = []

    if not subs:
        return [task], raw, usage
    return subs[:max_subtasks], raw, usage


# ---------------------------------------------------------------- worker


class Ledger:
    def __init__(self):
        self.n_turns = 0
        self.n_tool_calls = 0
        self.n_bad_calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.n_workers = 0
        self.plan_prompt_tokens = 0
        self.plan_completion_tokens = 0

    def absorb(self, r):
        self.n_turns += r.n_turns
        self.n_tool_calls += r.n_tool_calls
        self.n_bad_calls += r.n_bad_calls
        self.prompt_tokens += r.prompt_tokens
        self.completion_tokens += r.completion_tokens


def _make_worker_copy(src_root, workdir):
    """复制一份独立的代码库副本给 worker。"""
    dst = os.path.join(workdir, "worker_" + str(threading.get_ident()))
    shutil.copytree(src_root, dst)
    return dst


def _run_worker(idx, subtask, src_root, isolate, model, max_turns, timeout_s):
    """跑一个 worker。在自己的线程里，所以 set_repo_root 只影响自己。

    ⚠️ 不隔离时也必须显式 set_repo_root(src_root)：
    threading.local 不会继承主线程的值，新线程读到的是 DEFAULT_REPO_ROOT。
    两者恰好相等时看不出问题，一旦用 --repo 指向别处，
    worker 就会静默地查一个空目录 —— 表现为「teams 的 worker 全是废话」，
    而 lead 会自己重新查一遍，你只会看到 teams 成本奇高，
    完全想不到根因在这里。
    """
    tmpdir = None
    if isolate:
        tmpdir = tempfile.mkdtemp(prefix=f"arena_w{idx}_")
        set_repo_root(_make_worker_copy(src_root, tmpdir))
    else:
        set_repo_root(src_root)
    try:
        return run_agent(
            subtask, task_id=f"w{idx}", strategy=NAME + "_worker",
            tracer=None, max_turns=max_turns, timeout_s=timeout_s,
            system_prompt=WORKER_PROMPT,
            tools=TOOLS, tool_schemas=TOOL_SCHEMAS, model=model,
        )
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------- 入口


def run(task, task_id="", tracer=None, max_turns=DEFAULT_MAX_TURNS,
        timeout_s=600, model=None, n_workers=3, isolate=True,
        worker_max_turns=12, **kw):
    """teams 策略入口。

    参数
        n_workers: 拆成几个子任务（上限，实际由 planner 决定）
        isolate:   worker 是否用独立代码库副本。
                   只读任务可以关掉以省时间；涉及改代码的任务必须开。
    """
    ledger = Ledger()
    src_root = current_repo_root()
    workdir = None

    # ---- ① 规划
    subtasks, plan_raw, plan_usage = _plan(task, model=model,
                                           max_subtasks=n_workers)
    ledger.plan_raw = plan_raw
    ledger.n_workers = len(subtasks)  # 分成多少个worker并行
    ledger.plan_prompt_tokens = plan_usage.get("prompt_tokens", 0)
    ledger.plan_completion_tokens = plan_usage.get("completion_tokens", 0)

    # ---- ② 并行执行
    results = [None] * len(subtasks)
    worker_timeout = int(timeout_s * 0.5)
    with ThreadPoolExecutor(max_workers=len(subtasks)) as pool:
        futs = {
            pool.submit(_run_worker, i + 1, sub, src_root, isolate, model,
                        worker_max_turns, worker_timeout): i # 把这个任务扔进线程池，让它去执行
            for i, sub in enumerate(subtasks)
        }
        for fut in futs:
            i = futs[fut] 
            try:
                r = fut.result() # 得到每个worker的运行结果
            except Exception as e:          # 单个 worker 炸了不能拖垮整队
                r = AgentResult(success=False, # 单 Worker 故障隔离
                                error=f"worker 异常：{type(e).__name__}: {e}")
            results[i] = r
            ledger.absorb(r)

    # ---- ③ 汇总
    findings = []  # Worker 提供证据，Lead 负责最终判断
    for i, (sub, r) in enumerate(zip(subtasks, results), 1):
        status = "成功" if r.success else f"未完成（{r.error}）"
        findings.append(f"--- 分析员 #{i} [{status}]\n任务：{sub}\n结论：{r.answer or '（无）'}")

    merge_task = MERGE_TEMPLATE.format(task=task, findings="\n\n".join(findings))

    r = run_agent(
        merge_task, task_id=task_id, strategy=NAME, tracer=tracer,
        max_turns=max_turns, timeout_s=int(timeout_s * 0.4),
        system_prompt=MERGE_PROMPT,
        tools=TOOLS, tool_schemas=TOOL_SCHEMAS, model=model,
    )

    # 可以得到：这个 Teams 任务到底拆成了几个 Worker、拆成了什么。
    # 总账：规划 + 所有 worker + 汇总
    r.n_turns += ledger.n_turns
    r.n_tool_calls += ledger.n_tool_calls
    r.n_bad_calls += ledger.n_bad_calls
    r.prompt_tokens += ledger.prompt_tokens + ledger.plan_prompt_tokens
    r.completion_tokens += ledger.completion_tokens + ledger.plan_completion_tokens
    r.n_workers = ledger.n_workers
    r.subtasks = subtasks
    return r


if __name__ == "__main__":
    set_repo_root(os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "sandbox", "requests_src")))
    r = run("requests 里 Session 对象默认挂载了哪些 HTTP 适配器？分别在哪个文件？",
            task_id="DEMO", n_workers=3, isolate=False)
    print(r)
    print("worker 数:", getattr(r, "n_workers", 0))
    print("拆出的子任务:")
    for i, s in enumerate(getattr(r, "subtasks", []), 1):
        print(f"  {i}. {s}")
    print()
    print(r.answer)
