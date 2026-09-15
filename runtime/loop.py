"""Agent 核心循环 —— 整个项目唯一的核心文件。

每一处设计选择都写在下面的注释里。

================================================================================
## 消息协议（OpenAI 兼容，DeepSeek 一样）

messages 里四种角色：

    {"role": "system",    "content": "..."}
    {"role": "user",      "content": "任务描述"}
    {"role": "assistant", "content": "...", "tool_calls": [ {...} ]}
    {"role": "tool",      "tool_call_id": "...", "content": "工具结果"}

关键的坑：**assistant 的 tool_calls 和后面的 role=tool 消息是绑定的**。
每一条 tool_calls 必须有一条 tool_call_id 对应的 tool 消息紧随其后，
否则 API 直接报错（这是 OpenAI 消息协议的边界对齐要求）。

================================================================================
## 借鉴 pi-mono 的三处设计

① **参数校验**   → validate.validate_tool_arguments(schema, args)
② **错误转反馈** → 工具不存在 / 参数错 / 执行抛异常，全部变成一条
                   role=tool 的消息喂回模型，**绝不能让异常冒出循环**
③ **terminate**  → submit 返回 terminate=True 让循环停下
                   （让工具自己决定「行了别再跑了」，不在循环外加特判）

另外加了 max_turns 和 timeout_s 双重保险。跑 180 次任务时，
没有这个你一觉醒来发现烧了一晚上。

================================================================================
## 一个刻意的设计：没有 tool_calls 时不算完成

模型「只是说话」不算完成任务 —— 它必须调 submit。
否则会出现一种很隐蔽的假成功：模型在最后一轮输出一段看起来像答案的文字，
你的循环以为它说完了就退出，判成成功，但它根本没定位到正确位置。
所以这里会 nudge（提醒）它，连续 MAX_NUDGES 次还不提交才判失败。
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runtime.llm import call_llm                      # 把 messages + tools 发给大模型，让大模型决定下一步干什么。
from runtime.tools import TOOLS, TOOL_SCHEMAS, ToolError  # noqa: E402
from runtime.validate import validate_tool_arguments   # noqa: E402

SYSTEM_PROMPT = """你是一个代码库分析助手，工作目录是一个 Python 代码仓库。

工作方式：
1. 先用 find_files / grep 定位，再用 read_file 读具体片段。不要一上来就读整个文件。
2. 信息够了就调用 submit 提交答案 —— 这是结束任务的唯一方式。
3. 答案要具体：给出文件路径、函数名、行号。不要描述你的搜索过程。

如果工具返回错误，读清楚错误原因，换一个参数或换一个工具重试。"""
# 提醒LLM回答了就提交
NUDGE = "你还没有提交答案。请调用 submit 工具提交最终答案，不要只回复文字。"

# ---------------------------------------------------------------- 为什么是 20
# 太大 → 失败任务拖很久且烧钱；太小 → 复杂任务被误杀，实验数据失真。
# 这个值不是拍脑袋定的：跑完 20 条 A/B 类试点任务后看轮次分布，
# 90% 在 8 轮内完成，最长的 13 轮。20 轮留出约 55% 余量，
# 同时保证「跑偏了的任务」不会无限空转烧钱。
# 改这个值之前，先看 results/runs.db 里的 n_turns 分布：
#   SELECT strategy, MAX(n_turns), AVG(n_turns) FROM runs GROUP BY strategy
DEFAULT_MAX_TURNS = 20  # 最多让 Agent 和 LLM 交互 20 轮

# 连着提醒几次还不提交，就判失败
MAX_NUDGES = 3

# 保存一次 Agent 运行的最终结果
class AgentResult:
    """一次运行的最终结果。所有编排策略都返回这个，方便统一统计。"""

    def __init__(self, success, answer="", n_turns=0, n_tool_calls=0,
                 n_bad_calls=0, prompt_tokens=0, completion_tokens=0,
                 peak_context_chars=0, wall_ms=0, error=""):
        self.success = success # 任务成功了吗
        self.answer = answer # 最终答案
        self.n_turns = n_turns # 运行了几轮
        self.n_tool_calls = n_tool_calls # 调用了多少次工具
        self.n_bad_calls = n_bad_calls # 模型调用了不存在的工具的次数
        self.prompt_tokens = prompt_tokens # prompt用了多少 token
        self.completion_tokens = completion_tokens # 总共用了多少token
        self.peak_context_chars = peak_context_chars # 上下文最多有多少字符
        self.wall_ms = wall_ms # 运行了多久
        self.error = error # 如果失败，显示原因

    def as_dict(self): # 变dict格式
        return vars(self).copy()

    def __repr__(self): # print格式
        flag = "OK  " if self.success else "FAIL"
        return (f"<{flag} turns={self.n_turns} calls={self.n_tool_calls} "
                f"bad={self.n_bad_calls} tok={self.prompt_tokens + self.completion_tokens} "
                f"err={self.error!r}>")

# 根据工具名字，找到这个工具对应的参数 schema
def _schema_of(tool_name, tool_schemas):
    """从 tool_schemas 里取出某个工具的 parameters（给 validate 用）。"""
    for s in tool_schemas:
        if s["function"]["name"] == tool_name:
            return s["function"].get("parameters", {}) # 返回参数要求，检查模型给的参数对不对
    return None

# 计算当前 messages（主agent） 有多少字符
def _ctx_chars(messages):
    """上下文大小的代理指标。

    为什么用字符数而不是 token 数：token 要额外算，且 API 返回的 prompt_tokens
    是**当轮整份 messages 的量**，包含历史，无法反映「上下文涨到多大」。
    字符数是免费的、可跨策略比较的。peak_context_chars 反映的正是
    「这个编排策略会不会把上下文撑爆」——这是本项目要测的核心指标之一。
    """
    return sum(len(str(m.get("content") or "")) +
               len(str(m.get("tool_calls") or "")) for m in messages)


def _finish(result, tracer, run_id):
    """统一的收尾：记库 + 返回。别在四个退出点各写一遍。"""
    if tracer and run_id:
        tracer.finish_run(run_id, success=result.success,
                          final_answer=result.answer,
                          n_bad_calls=result.n_bad_calls,
                          error=result.error)
    return result


def run_agent(task, task_id="", strategy="single", max_turns=DEFAULT_MAX_TURNS,
              timeout_s=300, tracer=None, system_prompt=SYSTEM_PROMPT,
              tools=None, tool_schemas=None, model=None):
    """跑一个 agent 直到它 submit 或超限。

    参数
        task:          任务描述（字符串）
        task_id:       评测集里的编号，用于 tracer
        strategy:      编排策略名，只用于记录，不影响本函数逻辑
        tracer:        Tracer 实例，None 表示不记录
        tools:         工具函数字典。三种编排共用同一套（实验控制），
                       但 subagent 要传**去掉 spawn 的版本**。
        tool_schemas:  同上，给模型看的菜单

    返回
        AgentResult
    """

    tools = tools if tools is not None else TOOLS
    tool_schemas = tool_schemas if tool_schemas is not None else TOOL_SCHEMAS

    t_start = time.time() # 记录：Agent 从什么时候开始运行
    run_id = tracer.start_run(strategy, task_id) if tracer else None

    # 初始化
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task},
    ]

    n_turns = 0
    n_tool_calls = 0
    n_bad_calls = 0
    prompt_tokens = 0
    completion_tokens = 0
    peak_context = _ctx_chars(messages)
    nudges = 0
    final_answer = ""

    # 如果超时了，统一生成一个失败结果
    def _timeout_result():
        return AgentResult(
            success=False, answer=final_answer, n_turns=n_turns,
            n_tool_calls=n_tool_calls, n_bad_calls=n_bad_calls,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            peak_context_chars=peak_context,
            wall_ms=int((time.time() - t_start) * 1000),
            error=f"超时（{timeout_s}s）",
        )

    # ---------------------------------------------------------------- 外层循环
    while n_turns < max_turns: # 只要还没有超过最大轮数，就继续运行 Agent

        # 超时检查放在每轮开头，保证「刚超时」能被及时拦下，
        #         而不是等这一轮工具跑完（edit_file / run_tests 可能跑几十秒）。
        if time.time() - t_start > timeout_s:
            return _finish(_timeout_result(), tracer, run_id)

        n_turns += 1
        t_turn = time.time()

        # 把目前所有聊天历史 + 工具菜单交给 LLM，让 LLM 决定下一步。
        #         注意：这里没有 try/except，是刻意的 —— call_llm 内部已经退避重试
        #         3 次了，还失败说明是网络/鉴权级别的硬故障，
        #         与其吞掉异常让实验静默变脏，不如让它冒出来，你立刻能看见。
        reply = call_llm(messages, tools=tool_schemas, model=model)
        # 读取输入输出token使用量
        usage = reply.get("usage") or {}
        prompt_tokens += usage.get("prompt_tokens", 0)
        completion_tokens += usage.get("completion_tokens", 0)

        # 把 assistant 消息 append 回 messages。
        #         llm.py 把 tool_calls 简化成了 {id, name, args}，
        #         但 append 回去必须还原成 OpenAI 原始格式，否则下一轮 API 直接拒绝。
        # 把 LLM 的回复重新加入 messages
        assistant_msg = {"role": "assistant", "content": reply["content"] or ""}
        # 如果LLM说要调用工具，就转换标准格式
        if reply["tool_calls"]:
            assistant_msg["tool_calls"] = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": json.dumps(tc["args"], ensure_ascii=False),
                    },
                }
                for tc in reply["tool_calls"]
            ]
        # 更新message
        messages.append(assistant_msg)
        # 更新最大上下文
        peak_context = max(peak_context, _ctx_chars(messages))

        # 模型没喊人（只说话）不算完成。
        if not reply["tool_calls"]:
            nudges += 1
            if nudges > MAX_NUDGES:
                return _finish(AgentResult(
                    success=False, answer=final_answer, n_turns=n_turns,
                    n_tool_calls=n_tool_calls, n_bad_calls=n_bad_calls,
                    prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                    peak_context_chars=peak_context,
                    wall_ms=int((time.time() - t_start) * 1000),
                    error=f"模型连续 {nudges} 轮未调用 submit",
                ), tracer, run_id)
            # 提示LLM进行调用工具提交
            messages.append({"role": "user", "content": NUDGE})
            if tracer and run_id:
                tracer.log_turn(run_id, turn=n_turns, tool_name=None,
                                tool_args=None,
                                tool_result=f"[无工具调用，第 {nudges} 次提醒]",
                                is_error=True, usage=usage,
                                context_chars=_ctx_chars(messages),
                                elapsed_ms=int((time.time() - t_turn) * 1000))
            continue

        # 遍历每一个 tool_call。
        #         一轮可能返回多个调用（并行工具调用），必须**逐个**处理并各配一条
        #         tool 消息，tool_call_id 一一对齐，否则 API 报「孤儿 tool 消息」。
        for tc in reply["tool_calls"]:
            name = tc["name"]
            args = tc["args"]
            tc_id = tc["id"]
            n_tool_calls += 1
            is_error = False

            # 6a. 工具不存在（模型幻觉出工具名）
            # 工具不存在 / 参数错误 / 执行异常，都变成 role=tool 消息，再喂回模型
            if name not in tools:
                n_bad_calls += 1
                is_error = True
                result_text = (
                    f"错误：不存在名为 {name!r} 的工具。"
                    f"可用的工具只有: {', '.join(sorted(tools))}"
                )

            else:
                # 6b. 参数校验（抄 pi 的第①样）
                # 检查：LLM 给的参数是不是符合要求，不符合要求变成 role=tool 消息，再喂回模型
                schema = _schema_of(name, tool_schemas) or {}
                ok, err = validate_tool_arguments(schema, args)
                if not ok:
                    n_bad_calls += 1
                    is_error = True
                    result_text = f"参数错误：{err}"

                else:
                    # 6c. 执行（抄 pi 的第②样：所有异常都变成文本，绝不冒出去）--except中的result_text
                    try:
                        out = tools[name](**args)
                    except ToolError as e:
                        n_bad_calls += 1
                        is_error = True
                        result_text = f"执行失败：{e}"
                    except TypeError as e:
                        # 校验层漏掉的签名不匹配 —— 兜底，同样不能崩
                        n_bad_calls += 1
                        is_error = True
                        result_text = f"参数与工具签名不匹配：{e}"
                    except Exception as e:
                        n_bad_calls += 1
                        is_error = True
                        result_text = f"执行异常：{type(e).__name__}: {e}"
                    else:
                        result_text = out.text
                        if out.is_error:
                            n_bad_calls += 1
                            is_error = True

                        # 6d. terminate（抄 pi 的第③样）—— submit 返回的:让terminate变成True
                        # 这个工具告诉 Agent：任务可以结束了
                        if out.terminate:
                            final_answer = args.get("answer", "") or ""
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc_id,
                                "content": result_text,
                            })
                            if tracer and run_id:
                                tracer.log_turn(
                                    run_id, turn=n_turns, tool_name=name,
                                    tool_args=args, tool_result=result_text,
                                    is_error=False, usage=usage,
                                    context_chars=_ctx_chars(messages),
                                    elapsed_ms=int((time.time() - t_turn) * 1000),
                                )
                            return _finish(AgentResult(
                                success=True, answer=final_answer, n_turns=n_turns,
                                n_tool_calls=n_tool_calls, n_bad_calls=n_bad_calls,
                                prompt_tokens=prompt_tokens,
                                completion_tokens=completion_tokens,
                                peak_context_chars=peak_context,
                                wall_ms=int((time.time() - t_start) * 1000),
                            ), tracer, run_id)

            # 6e. append 回 messages —— tool_call_id 必须和上面那条的 id 对齐
            messages.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": result_text,
            })

            # 6f. 记 tracer
            if tracer and run_id:
                tracer.log_turn(
                    run_id, turn=n_turns, tool_name=name,
                    tool_args=args, tool_result=result_text,
                    is_error=is_error, usage=usage,
                    context_chars=_ctx_chars(messages),
                    elapsed_ms=int((time.time() - t_turn) * 1000),
                )

        # 每轮结束再查一次超时（可能刚跑完一个很慢的 run_tests）
        if time.time() - t_start > timeout_s:
            return _finish(_timeout_result(), tracer, run_id)

    # 跑满 max_turns 判失败，不是成功。
    return _finish(AgentResult(
        success=False, answer=final_answer, n_turns=n_turns,
        n_tool_calls=n_tool_calls, n_bad_calls=n_bad_calls,
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
        peak_context_chars=peak_context,
        wall_ms=int((time.time() - t_start) * 1000),
        error=f"超过最大轮次 {max_turns}",
    ), tracer, run_id)


# ---------------------------------------------------------------- 自测

if __name__ == "__main__":
    root = os.environ.get(
        "ARENA_REPO",
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..",
                                     "sandbox", "requests_src")),
    )
    from runtime.tools import set_repo_root
    set_repo_root(root)

    TASK = ("requests 里判断重定向时是否要去掉 Authorization 头的函数叫什么，"
            "在哪个文件第几行？")

    print("=" * 68)
    print("核心循环自测")
    print("=" * 68)
    print("REPO_ROOT =", root)
    print("任务:", TASK)
    print("-" * 68)

    r = run_agent(TASK, task_id="SMOKE", strategy="single",
                  max_turns=DEFAULT_MAX_TURNS, timeout_s=180)

    print()
    print("success     :", r.success)
    print("n_turns     :", r.n_turns)
    print("n_tool_calls:", r.n_tool_calls)
    print("n_bad_calls :", r.n_bad_calls)
    print("tokens      :", r.prompt_tokens + r.completion_tokens)
    print("peak_ctx    :", r.peak_context_chars, "字符")
    print("wall_ms     :", r.wall_ms)
    print("error       :", r.error or "(无)")
    print("-" * 68)
    print("答案:", r.answer)
    print("=" * 68)

'''
                用户任务
                     │
                     ↓
              创建 messages
                     │
                     ↓
              ┌─────────────┐
              │   调用 LLM   │
              └──────┬──────┘
                     │
             LLM 要不要调用工具？
                /           \
              否             是
              │              │
              ↓              ↓
           nudge        遍历 tool_calls
              │              │
              │       ┌──────┴──────┐
              │       ↓             ↓
              │    工具不存在？   工具存在
              │       │             │
              │       ↓             ↓
              │     错误         参数校验
              │                     │
              │                     ↓
              │                  执行工具
              │                     │
              │              ┌──────┴──────┐
              │              ↓             ↓
              │          普通结果      terminate=True
              │              │             │
              │              ↓             ↓
              │        role=tool        最终答案
              │              │             │
              └──────→ messages ←─────────┘
                             │
                             ↓
                       下一轮 LLM
'''