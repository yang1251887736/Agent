"""LLM 调用封装 —— 只走 OpenAI 兼容协议，不依赖任何 Agent 框架。

call_llm 的返回值结构见下方字段说明，核心循环只依赖这几个字段。

返回：
    {
        "content":     str,   模型说的话（可能为空串）
        "tool_calls":  list,  模型喊人，已把 arguments 从 JSON 字符串解析成 dict
        "usage":       dict,  prompt_tokens / completion_tokens / total_tokens
        "stop_reason": str,   "stop" / "tool_calls" / "length" ...
    }
"""

import json
import os
import time

from openai import OpenAI

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = OpenAI(
            api_key=os.environ["OPENAI_API_KEY"],
            base_url=os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com"),
        )
    return _client


def call_llm(messages, tools=None, model=None, temperature=0.0, timeout=120, retries=3):
    """发一次请求给模型。

    temperature 默认 0 —— 做对照实验必须固定它，否则同一条任务两次跑出不同结果，
    对比数据就不可信。

    注意 arguments 解析失败时**不抛异常**，而是塞一个 __parse_error__ 标记。
    理由见 validate.py —— 模型的错误要变成模型能读懂的反馈，不是让程序崩掉。
    """
    model = model or os.environ.get("OPENAI_MODEL_ID", "deepseek-chat")

    kwargs = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "timeout": timeout,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"

    last_err = None
    for attempt in range(retries):
        try:
            resp = _get_client().chat.completions.create(**kwargs)
            break
        except Exception as e:  # 网络抖动 / 限流，退避重试
            last_err = e
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
    else:
        raise last_err

    msg = resp.choices[0].message

    tool_calls = []
    for tc in msg.tool_calls or []:
        raw = tc.function.arguments or "{}"
        try:
            args = json.loads(raw)
            if not isinstance(args, dict):
                args = {"__parse_error__": f"arguments 不是对象: {raw[:200]}"}
        except json.JSONDecodeError as e:
            args = {"__parse_error__": f"arguments 不是合法 JSON: {e}; 原文: {raw[:200]}"}
        tool_calls.append({"id": tc.id, "name": tc.function.name, "args": args})

    usage = {}
    if getattr(resp, "usage", None):
        usage = {
            "prompt_tokens": resp.usage.prompt_tokens,
            "completion_tokens": resp.usage.completion_tokens,
            "total_tokens": resp.usage.total_tokens,
        }

    return {
        "content": msg.content or "",
        "tool_calls": tool_calls,
        "usage": usage,
        "stop_reason": resp.choices[0].finish_reason,
    }


if __name__ == "__main__":
    # 冒烟测试：确认 key 通了、能拿到 usage
    out = call_llm([{"role": "user", "content": "只回复两个字：收到"}])
    print("content:", out["content"])
    print("usage  :", out["usage"])
    print("stop   :", out["stop_reason"])
