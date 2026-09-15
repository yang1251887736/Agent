'''定义工具+TOOL(工具本体)+Tool Sechema（工具说明书）：给LLM看的'''

"""六个工具 + 它们的菜单。

工具定义与实现放在同一个文件，改函数签名时菜单跟着改。模型只看得见 name/description/parameters，
看不到函数体，所以 description 写得好不好直接决定模型会不会用、用得对不对。

## 为什么工具定义要和函数放在一起

放一起才能保证「改了函数签名，菜单跟着改」。分两个文件的话，
你改了 read_file 的参数却忘了改 schema，模型就会一直传错参数，
而且错得很隐蔽 —— 只表现为「成功率莫名下降」。

## 工具集为什么是这 6 个（实验控制的关键）

三种编排共用**同一套工具**。这是对照实验的基本要求：
只有「编排方式」这一个变量，其他全部固定。
否则你没法说清楚差异来自编排还是来自工具。

其中 run_tests / edit_file 只有 C 类任务用得上，但对 A/B 类任务同样开放 ——
如果只在 C 类开放，各编排面对的工具集就不一样了，实验就不干净。

## 关于 submit

submit 是唯一的出口，它返回 terminate=True 让循环停下。
这个设计借鉴 pi-mono 的 terminate 机制：
**让工具自己决定「行了别再跑了」**，而不是在循环外面加特判。

## 为什么 REPO_ROOT 是线程局部的

teams 策略要并行跑多个 worker，每个 worker 用**独立的代码库副本**做隔离
（不然两个 worker 同时 edit_file 会互相踩）。

如果 REPO_ROOT 是个普通的模块级全局变量，A 线程刚 set 成自己的副本，
B 线程一 set，A 的后续文件操作就全跑到 B 的副本上去了 ——
这种 bug 极难查，因为它是随机出现的，取决于线程调度。

用 threading.local 之后，每个线程看到的是自己那份，互不干扰。
"""

import glob
import os
import re
import subprocess
import threading

# ---------------------------------------------------------------- 返回值契约


class ToolOutput: # 规定所有工具最后都返回一种统一格式
    """工具执行结果。

    text      喂回模型的文本
    terminate True 表示这个工具要求结束循环（目前只有 submit）
    is_error  True 表示这是一次失败调用，会被计入「错误调用率」
    """

    def __init__(self, text, terminate=False, is_error=False):
        self.text = text
        self.terminate = terminate
        self.is_error = is_error

    def __str__(self):
        return self.text


class ToolError(Exception):  # loop.py 第283行
    """工具执行失败。由调用方转成一条 tool 消息喂回模型，不让程序崩掉。"""


# ---------------------------------------------------------------- 沙箱根目录

# 所有文件操作都被限制在这个目录内。模型可能会传 ../../etc/passwd，必须挡掉。
# 环境变量 ARENA_REPO 指向 requests 的一份**副本**。
'''
“给每个 Agent 划定自己的文件操作范围，并防止它越界。
'''
# Agent 的“活动范围”
DEFAULT_REPO_ROOT = os.path.abspath(os.environ.get("ARENA_REPO", os.path.join(
    os.path.dirname(__file__), "..", "sandbox", "requests_src")))

# 线程局部：每个线程可以有自己的仓库根目录（teams 的 worker 各用一份独立副本）。
# 主线程 set 一次后，新线程不会自动继承 —— 这是刻意的，

# 每个 worker 必须显式声明自己操作哪个副本，避免「忘了设就继承了别人的」。
_local = threading.local()

# 获取当前线程正在使用的仓库
def current_repo_root():
    return getattr(_local, "root", DEFAULT_REPO_ROOT)

# 设置当前线程的仓库
def set_repo_root(path):
    """设置**当前线程**的仓库根目录。"""
    _local.root = os.path.abspath(path)
    return _local.root


def reset_repo_root():
    """把当前线程恢复成默认根目录。"""
    _local.root = DEFAULT_REPO_ROOT

# current_repo_root的简写
def _root():
    return current_repo_root()


# 检查路径有没有越界
def _safe(path):
    """把相对路径解析到当前线程的 REPO_ROOT 内，越界直接拒绝。"""
    root = current_repo_root()
    joined = os.path.abspath(os.path.join(root, path))
    if not (joined == root or joined.startswith(root + os.sep)):
        raise ToolError(f"拒绝访问：{path} 超出仓库目录范围")
    return joined


# ---------------------------------------------------------------- 工具实现

def tool_find_files(pattern):
    """按文件名找文件。"""
    # _root()-仓库根目录, **-所有子目录, pattern-文件匹配规则
    matches = sorted(glob.glob(os.path.join(_root(), "**", pattern), recursive=True))
    # 把绝对目录变成相对目录
    rel = [os.path.relpath(m, _root()) for m in matches]
    rel = [r for r in rel if not r.startswith("..")]
    if not rel:
        return ToolOutput(f"没有匹配 {pattern!r} 的文件")
    shown = rel[:50]
    tail = f"\n... 还有 {len(rel) - 50} 个" if len(rel) > 50 else ""
    return ToolOutput("\n".join(shown) + tail)


def tool_grep(pattern, path="", max_results=30):
    """按正则搜索文件内容，返回 文件:行号:内容。"""
    try:
        # 把字符串变成正则表达式
        regex = re.compile(pattern)
    except re.error as e:
        raise ToolError(f"正则表达式不合法: {e}")

    root = _safe(path) if path else _root()
    if os.path.isfile(root):
        files = [root]
    else:
        # 找所有的py文件
        files = sorted(glob.glob(os.path.join(root, "**", "*.py"), recursive=True))

    hits = []
    # 一个个文件看
    for fp in files:
        try:
            with open(fp, encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f, 1):
                    # 当前这一行有没有匹配到目标，找到保存下来
                    if regex.search(line):
                        hits.append(f"{os.path.relpath(fp, _root())}:{i}:{line.rstrip()}")
                        if len(hits) >= max_results:
                            break
        except OSError:
            continue
        if len(hits) >= max_results:
            break

    if not hits:
        return ToolOutput(f"没有找到匹配 {pattern!r} 的内容")
    tail = f"\n... 已截断（上限 {max_results} 条），请缩小 pattern 或指定 path" if len(hits) >= max_results else ""
    return ToolOutput("\n".join(hits) + tail)


def tool_read_file(path, offset=0, limit=200):
    """读文件内容，带行号。offset=0 且 limit=0 表示读整个文件。"""
    # offset从第几行开始，limit读几行
    fp = _safe(path)
    if not os.path.isfile(fp):
        raise ToolError(f"文件不存在: {path}（先用 find_files 确认路径）")

    with open(fp, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    if limit and limit > 0:
        chunk = lines[offset: offset + limit]
    else:
        chunk = lines[offset:]
    # 给每一行加行号
    out = [f"{offset + i + 1:5d} | {l.rstrip()}" for i, l in enumerate(chunk)]
    head = f"[共 {len(lines)} 行，显示第 {offset + 1}-{offset + len(chunk)} 行]\n"
    return ToolOutput(head + "\n".join(out))


def tool_edit_file(path, old, new):
    """把文件中第一次出现的 old 精确替换成 new。

    要求 old 在文件中**唯一或至少你确认识别的是第一处** ——
    不唯一时报错并告诉你出现了几次，让你自己收窄 old。
    """
    fp = _safe(path)
    if not os.path.isfile(fp):
        raise ToolError(f"文件不存在: {path}")

    with open(fp, encoding="utf-8") as f:
        src = f.read()

    n = src.count(old)
    if n == 0:
        raise ToolError(f"没找到要替换的内容（old 不在文件中）。old 前 80 字符: {old[:80]!r}")
    if n > 1:
        raise ToolError(
            f"old 在文件中出现了 {n} 次，无法安全替换。"
            f"请把 old 写得更长一些（多包含几行上下文），确保唯一。"
        )

    # 只替换1次
    with open(fp, "w", encoding="utf-8") as f:
        f.write(src.replace(old, new, 1))
    return ToolOutput(f"已替换成功: {path}")


def tool_run_tests(test_path=""):
    """跑 pytest，只返回通过/失败摘要（不返回完整输出，太长会撑爆上下文）。"""
    target = _safe(test_path) if test_path else _root()
    try:
        proc = subprocess.run(
            ["python", "-m", "pytest", target, "-q", "--no-header", "-x"],
            capture_output=True, text=True, timeout=180, cwd=_root(),
        )
    except subprocess.TimeoutExpired:
        return ToolOutput("ERROR: 测试超时（180s）", is_error=True)
    except FileNotFoundError:
        return ToolOutput("ERROR: 找不到 pytest，先 pip install pytest", is_error=True)

    out = (proc.stdout or "") + (proc.stderr or "")
    # 只留最后 25 行 —— 失败信息一般在最后
    lines = [l for l in out.strip().splitlines() if l.strip()]
    summary = "\n".join(lines[-25:])
    ok = proc.returncode == 0
    return ToolOutput(f"退出码 {proc.returncode}\n{summary}", is_error=not ok)


def tool_submit(answer):
    """提交最终答案并结束任务。

    这是唯一能让循环正常结束的工具 —— 不调它，跑到 max_turns 会被判失败。
    """
    return ToolOutput("已提交", terminate=True)


# ---------------------------------------------------------------- 菜单 + 注册表
# TOOLS：把名字和真正函数对应起来
TOOLS = {
    "find_files": tool_find_files,
    "grep": tool_grep,
    "read_file": tool_read_file,
    "edit_file": tool_edit_file,
    "run_tests": tool_run_tests,
    "submit": tool_submit,
}
# 给LLM看的工具菜单
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "find_files",
            "description": (
                "按文件名模式查找文件，支持通配符。"
                "当你还不确定目标代码在哪个文件里时，先用这个缩小范围。"
                "例如 find_files(pattern=\"*.py\") 列出所有 Python 文件。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "文件名通配符，如 \"*.py\"、\"sessions.py\"",
                    }
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": (
                "在整个代码库里搜索匹配正则表达式的内容，返回 文件:行号:行内容。"
                "当你已知函数/变量名、错误码、类名时，用这个最快 —— 比逐个读文件高效得多。"
                "结果超过 max_results 会被截断，此时应缩小 pattern 或指定 path。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "正则表达式，如 \"def send\"、\"class Session\"",
                    },
                    "path": {
                        "type": "string",
                        "description": "可选。限定在某个子目录或文件内搜索，如 \"requests/sessions.py\"",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "最多返回多少条，默认 30",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "读取文件内容，带行号。路径相对于仓库根目录。"
                "先用 grep 定位到大致行号，再用 offset/limit 只读相关片段 —— "
                "不要一次读整个大文件，会浪费上下文。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对路径，如 \"requests/sessions.py\""},
                    "offset": {"type": "integer", "description": "从第几行开始（0 基），默认 0"},
                    "limit": {"type": "integer", "description": "读多少行，默认 200；传 0 表示读完"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "把文件中第一次出现的 old 文本精确替换成 new。"
                "old 必须在文件中唯一，否则会拒绝执行并告诉你出现了几次。"
                "修改前必须先用 read_file 读到原文，逐字照抄 —— 凭记忆写 old 一定会失败。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对路径"},
                    "old": {"type": "string", "description": "要被替换的原文，必须逐字一致（含缩进）"},
                    "new": {"type": "string", "description": "替换后的新文本"},
                },
                "required": ["path", "old", "new"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": (
                "运行 pytest 验证你的改动是否破坏了现有功能。"
                "改完代码后必须调用它 —— 不验证就提交答案，改动很可能是错的。"
                "只返回摘要，输出已被截断。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "test_path": {
                        "type": "string",
                        "description": "可选。指定测试文件或目录；不传则跑整个仓库",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit",
            "description": (
                "提交最终答案。这是结束任务的唯一方式 —— "
                "任何任务都必须在信息足够后调用它，不调用会一直循环到超时并被判失败。"
                "答案要具体：给出文件路径、函数名、行号，不要只描述过程。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "answer": {
                        "type": "string",
                        "description": "最终答案。A/B 类任务要包含具体文件路径和符号名；C 类要说明改了哪些文件",
                    }
                },
                "required": ["answer"],
            },
        },
    },
]


if __name__ == "__main__":
    set_repo_root(os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "requests", "src")))
    print("repo_root =", current_repo_root())
    print("\n--- find_files('*.py') 前 5 个 ---")
    print("\n".join(str(tool_find_files("*.py")).splitlines()[:5]))
    print("\n--- grep('def cert_verify') ---")
    print(tool_grep("def cert_verify", max_results=3))
    print("\n--- read_file('requests/exceptions.py', 0, 4) ---")
    print(tool_read_file("requests/exceptions.py", 0, 4))
    print("\n--- 越界访问测试 ---")
    print(tool_read_file("../../../../etc/passwd", 0, 2))
'''
第一层：安全
_safe()
REPO_ROOT
threading.local()

        ↓

第二层：工具实现
find_files
grep
read_file
edit_file
run_tests
submit

        ↓

第三层：统一返回
ToolOutput
ToolError

        ↓

第四层：工具注册
TOOLS
TOOL_SCHEMAS

        ↓

第五层：给 Agent/LLM 使用
LLM 选择工具
        ↓
Python 执行工具
        ↓
结果返回 LLM
        ↓
继续 or submit
'''

'''
TOOL_SCHEMAS
    ↓
告诉 LLM：“我有什么工具、怎么调用”

TOOLS
    ↓
告诉 Python：“工具名字对应哪个函数”

ToolOutput
    ↓
告诉 Agent：“工具执行结果是什么、是否报错、是否结束”
'''