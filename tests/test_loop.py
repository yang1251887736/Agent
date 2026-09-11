"""核心循环的验收测试 —— 覆盖 6 条验收标准。

为什么用假 LLM 而不是真模型：
  ① 这些路径真模型正常跑不会触发（幻觉工具名、参数类型错），
     靠真模型碰运气去撞，既不稳定又烧钱
  ② 可复现 —— 同样的输入永远同样的输出，能进回归测试
  ③ 不花一分钱

用法： python -m tests.test_loop
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import runtime.loop as loop                       # noqa: E402
from runtime.tools import set_repo_root, TOOLS, TOOL_SCHEMAS  # noqa: E402
from runtime.tracer import Tracer                 # noqa: E402

set_repo_root(os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "sandbox", "requests_src")))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))


def fake_llm(script):
    """按脚本逐轮返回固定回复。script 是 list[dict]，用完后重复最后一条。

    每条形如：
        {"content": str, "tool_calls": [ {"id","name","args"} ], "usage": {...}}
    """
    state = {"i": 0}

    def _call(messages, tools=None, model=None, **kw):
        item = script[state["i"]] if state["i"] < len(script) else script[-1]
        state["i"] += 1
        return {
            "content": item.get("content", ""),
            "tool_calls": item.get("tool_calls", []),
            "usage": item.get("usage", {"prompt_tokens": 10, "completion_tokens": 5}),
            "stop_reason": "tool_calls" if item.get("tool_calls") else "stop",
        }
    return _call


def install(script):
    loop.call_llm = fake_llm(script)


# ============================================================ 1. 空跑不算成功
print("\n【1】只问好、不调 submit —— 不能被判成功")
install([
    {"content": "你好！我是助手，有什么可以帮你的吗？"},
    {"content": "我还是不调工具。"},
    {"content": "真的不调。"},
    {"content": "好吧我调了：", "tool_calls": [
        {"id": "c1", "name": "submit", "args": {"answer": "最终答案"}}]},
])
r = loop.run_agent("你好", task_id="T1", strategy="single", max_turns=20)
check("前几轮空话后仍要求 submit，最终成功", r.success is True and r.answer == "最终答案",
      f"turns={r.n_turns}")

print("\n【1b】一直不调 submit —— 应判失败而不是无限循环")
install([{"content": "我就是不调工具。"}])
r = loop.run_agent("你好", task_id="T1b", strategy="single", max_turns=20)
check("连续空话判失败", r.success is False and "submit" in r.error, f"error={r.error!r}")
check("不会跑满 max_turns（nudge 及时止损）", r.n_turns <= loop.MAX_NUDGES + 1,
      f"n_turns={r.n_turns}")

# ============================================================ 2. 幻觉工具名
print("\n【2】模型调用不存在的工具 —— 程序不能崩，且要告诉模型可用工具")
install([
    {"tool_calls": [{"id": "c1", "name": "read_the_whole_repo", "args": {}}]},
    {"tool_calls": [{"id": "c2", "name": "submit", "args": {"answer": "改好了"}}]},
])
r = loop.run_agent("读代码", task_id="T2", strategy="single", max_turns=20)
check("幻觉工具不导致崩溃", r.success is True, repr(r))
check("计一次错误调用", r.n_bad_calls == 1, f"n_bad_calls={r.n_bad_calls}")

# ============================================================ 3. 参数类型错
print("\n【3】参数类型错误（max_results='abc'）—— 不崩，返回具体错误")
install([
    {"tool_calls": [{"id": "c1", "name": "grep",
                     "args": {"pattern": "def send", "max_results": "abc"}}]},
    {"tool_calls": [{"id": "c2", "name": "submit", "args": {"answer": "ok"}}]},
])
r = loop.run_agent("找 send", task_id="T3", strategy="single", max_turns=20)
check("类型错误不崩溃", r.success is True, repr(r))
check("计一次错误调用", r.n_bad_calls == 1, f"n_bad_calls={r.n_bad_calls}")

print("\n【3b】缺必填参数")
install([
    {"tool_calls": [{"id": "c1", "name": "grep", "args": {}}]},
    {"tool_calls": [{"id": "c2", "name": "submit", "args": {"answer": "ok"}}]},
])
r = loop.run_agent("找 send", task_id="T3b", strategy="single", max_turns=20)
check("缺参数不崩溃", r.success is True and r.n_bad_calls == 1, repr(r))

# ============================================================ 4. submit 立即停
print("\n【4】submit 之后立刻停止，不再调 LLM")
install([
    {"tool_calls": [{"id": "c1", "name": "submit", "args": {"answer": "done"}}]},
    {"tool_calls": [{"id": "c2", "name": "grep", "args": {"pattern": "不该执行"}}]},
])
r = loop.run_agent("任务", task_id="T4", strategy="single", max_turns=20)
check("submit 后停止", r.success is True and r.n_turns == 1, f"n_turns={r.n_turns}")
check("submit 后的工具没有被执行", r.n_tool_calls == 1, f"n_tool_calls={r.n_tool_calls}")

# ============================================================ 5. 超限
print("\n【5】跑满 max_turns —— 干净退出，success=False")
install([{"tool_calls": [{"id": "c1", "name": "grep", "args": {"pattern": "def "}}]}])
r = loop.run_agent("任务", task_id="T5", strategy="single", max_turns=3)
check("超限判失败", r.success is False, repr(r))
check("轮数等于上限", r.n_turns == 3, f"n_turns={r.n_turns}")
check("error 有说明", "最大轮次" in r.error, f"error={r.error!r}")

# ============================================================ 6. tracer 完整
print("\n【6】tracer 里有完整的 turns 记录")
db = os.path.join(tempfile.gettempdir(), "arena_test_loop.db")
if os.path.exists(db):
    os.remove(db)
tracer = Tracer(db)
install([
    {"tool_calls": [{"id": "c1", "name": "grep", "args": {"pattern": "def send"}}]},
    {"tool_calls": [{"id": "c2", "name": "submit", "args": {"answer": "sessions.py:752"}}]},
])
r = loop.run_agent("找 send", task_id="T6", strategy="single", tracer=tracer)
import sqlite3  # noqa: E402
conn = sqlite3.connect(db)
turns = conn.execute(
    "SELECT turn, tool_name, tool_args, is_error, prompt_tokens, context_chars "
    "FROM turns WHERE run_id LIKE '%T6%' ORDER BY id"
).fetchall()
runs = conn.execute(
    "SELECT strategy, task_id, success, n_tool_calls, n_turns, final_answer "
    "FROM runs WHERE task_id='T6'"
).fetchall()
conn.close()
check("turns 表有 2 条记录", len(turns) == 2, f"实际 {len(turns)} 条")
check("每条都带 tool_name", all(t[1] for t in turns), str([t[1] for t in turns]))
check("每条都带 usage(prompt_tokens)", all(t[4] for t in turns), str([t[4] for t in turns]))
check("每条都带 context_chars", all(t[3] is not None and t[5] for t in turns))
check("runs 表落了 final_answer", len(runs) == 1 and runs[0][5] == "sessions.py:752",
      str(runs[0][5]) if runs else "无")
check("runs 表 success 正确", len(runs) == 1 and runs[0][2] == 1)
os.remove(db)

# ============================================================ 7. 真实工具链
print("\n【7】真实工具：grep -> submit 走通，且沙箱拦得住越界")
install([
    {"tool_calls": [{"id": "c1", "name": "grep",
                     "args": {"pattern": "def should_strip_auth", "max_results": 5}}]},
    {"tool_calls": [{"id": "c2", "name": "read_file",
                     "args": {"path": "../../../../etc/passwd", "limit": 3}}]},
    {"tool_calls": [{"id": "c3", "name": "submit", "args": {"answer": "sessions.py:154"}}]},
])
r = loop.run_agent("找 should_strip_auth", task_id="T7", strategy="single")
check("越界访问被转成 tool 消息而非崩溃", r.success is True, repr(r))
check("越界计为错误调用", r.n_bad_calls == 1, f"n_bad_calls={r.n_bad_calls}")

# ============================================================
print("\n" + "=" * 60)
print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)
    sys.exit(1)
print("核心循环验收全部通过")
