# 检查 LLM 生成的工具参数对不对；如果不对，不让 Agent 崩，而是把错误信息返回给 LLM，让 LLM 下一轮自己改
"""工具参数校验。参考 pi-mono 的 `agent-loop.ts:535` validateToolArguments 实现
（只支持 JSON Schema 子集，不引 jsonschema 依赖）。

## 为什么这一层必须有

模型吐出来的 JSON 参数**经常是瞎编的**：
  - 缺必填字段（调 read_file 不给 path）
  - 类型错（limit 给个字符串 "20"）
  - 幻觉出根本不存在的参数名（给 grep 传一个 recursive=True）
  - 枚举值超出范围

不校验的话，你的 `TOOLS[name](**args)` 会直接 TypeError，
然后整个 agent 崩掉 —— 前面十几轮的上下文和 token 全白烧。

## 关键设计：校验失败不抛异常

返回 (False, 错误描述)，由调用方把这个描述**变成一条 tool 消息喂回模型**。
模型看到「参数 recursive 不存在，可用的参数是 pattern/path/max_results」，
下一轮多半就自己改对了。

**对模型犯的错，最好的处理是把它翻译成模型能读懂的反馈。**

只支持 JSON Schema 的一个子集（object + properties + required + enum + type），
够用，且不需要引入 jsonschema 依赖。
"""

# 把Json换成Python类型
_TYPES = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}

# 对比LLM给的参数和工具说明书里要求的参数
def validate_tool_arguments(schema, args):
    """校验模型给的参数。

    参数
        schema: 该工具 parameters 字段（JSON Schema）
        args:   模型给的 dict

    返回
        (ok: bool, error: str | None)
        ok=True 时 error 为 None
    """
    # 模型连 JSON 都没吐对 —— llm.py 里打了这个标记
    # 这甚至还没到“参数是否正确”的阶段，JSON 本身就坏了
    if "__parse_error__" in args:
        return False, args["__parse_error__"]

    # args 必须是 dict
    '''示例
    {
    "path": "main.py"
    }
    '''
    if not isinstance(args, dict):
        return False, f"参数必须是对象，收到的是 {type(args).__name__}"

    # 从schema中的参数，和必须有的参数
    properties = schema.get("properties", {}) or {}
    required = schema.get("required", []) or []

    # 1. 缺必填字段
    # llm给的参数没有达到required的要求
    missing = [k for k in required if k not in args]
    if missing:
        return False, (
            f"缺少必填参数: {', '.join(missing)}。"
            f"该工具需要的参数是: {', '.join(properties) or '（无）'}"
        )

    # 2. 幻觉出不存在参数名 —— 这一条最常见，也最值得报给模型
    # llm给了一个properties中没有的参数
    unknown = [k for k in args if k not in properties]
    if unknown:
        return False, (
            f"参数名不存在: {', '.join(unknown)}。"
            f"可用的参数只有: {', '.join(properties) or '（无，该工具不需要参数）'}"
        )

    # 3. 类型错
    for key, value in args.items():
        expected = properties[key].get("type")
        if not expected:
            continue

        # JSON Schema 允许 type 是数组，这里取第一个做宽松判定
        if isinstance(expected, list):
            expected = expected[0] if expected else None
        py_type = _TYPES.get(expected)
        if py_type is None:
            continue

        # bool 是 int 的子类，这里要挡掉：传 True 给 integer 字段是错的
        if py_type is int and isinstance(value, bool):
            return False, f"参数 {key} 需要整数，收到布尔值 {value!r}"
        if not isinstance(value, py_type):
            got = type(value).__name__
            return False, f"参数 {key} 类型错误：需要 {expected}，收到 {got}（{value!r}）"

        # 4. 枚举越界
        enum = properties[key].get("enum")
        if enum and value not in enum:
            return False, f"参数 {key} 的取值必须是 {enum} 之一，收到 {value!r}"

    return True, None
    '''
    JSON 没问题
    ↓
    args 是 dict
    ↓
    必填参数都有
    ↓
    没有不存在的参数
    ↓
    类型正确
    ↓
    enum 正确
    ↓
    OK
    '''

if __name__ == "__main__":
    schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "max_results": {"type": "integer"},
        },
        "required": ["pattern"],
    }

    cases = [
        ({"pattern": "def send"}, True),
        ({}, False),                                   # 缺必填
        ({"pattern": "a", "recursive": True}, False),  # 幻觉参数名
        ({"pattern": "a", "max_results": "20"}, False),# 类型错
        ({"pattern": "a", "max_results": True}, False),# bool 冒充 int
        ({"__parse_error__": "not json"}, False),      # JSON 都没吐对
    ]
    for args, expect_ok in cases:
        ok, err = validate_tool_arguments(schema, args)
        status = "OK " if ok == expect_ok else "FAIL"
        print(f"[{status}] {str(args)[:45]:47} -> ok={ok} {err or ''}")
