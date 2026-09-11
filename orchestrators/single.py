"""策略一：单 Agent 基线。

一个 agent，一套工具，一路跑到底。所有上下文堆在同一条 messages 里。

它是对照组。**另外两种策略的价值全靠跟它比才看得出来。**

## 它的失效模式（这是本项目要验证的核心假设）

每一轮的工具输出都永久留在 messages 里：

    grep "def send"      -> 30 行结果   ← 永远留着
    read_file sessions   -> 200 行      ← 永远留着
    grep "class Adapter" -> 25 行       ← 永远留着
    ...

任务越复杂，噪音堆得越厚。到后面模型要在几千行历史里找自己刚查到的东西 ——
它会被自己搜出来的中间结果带偏（第 6 步 interview 里那个「上下文污染」）。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runtime.loop import run_agent, SYSTEM_PROMPT, DEFAULT_MAX_TURNS  # noqa: E402
from runtime.tools import TOOLS, TOOL_SCHEMAS                          # noqa: E402

NAME = "single"


def run(task, task_id="", tracer=None, max_turns=DEFAULT_MAX_TURNS,
        timeout_s=300, model=None, **kw):
    return run_agent(
        task, task_id=task_id, strategy=NAME, tracer=tracer,
        max_turns=max_turns, timeout_s=timeout_s,
        system_prompt=SYSTEM_PROMPT,
        tools=TOOLS, tool_schemas=TOOL_SCHEMAS, model=model,
    )


if __name__ == "__main__":
    from runtime.tools import set_repo_root
    set_repo_root(os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "sandbox", "requests_src")))
    r = run("requests 里 Session 对象默认挂载了哪些 HTTP 适配器？分别在哪个文件？",
            task_id="DEMO")
    print(r)
    print(r.answer)
