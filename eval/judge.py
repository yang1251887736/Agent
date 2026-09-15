"""判定器 —— 用断言判对错，不用 LLM 打分。

## 为什么不用 LLM 打分

|  | LLM 打分 | 断言 |
|---|---|---|
| 可复现 | ❌ 同一个答案两次打分可能不同 | ✅ 永远一致 |
| 成本 | 每条任务还要再花一次调用 | ✅ 零 |
| 可审计 | ❌ "模型觉得它对" | ✅ 能指出缺了哪个符号 |
| 被质疑时 | "评分 prompt 我调过几次" | "标准答案里就有这个符号" |

判定标准写在评测集里，跑之前就定死了，跑完之后改不了 —— 这是评测客观性的来源。

这不是偷懒。业界正经的代码评测（SWE-bench、HumanEval）全是断言/测试判定，
LLM 打分只在**没有标准答案**的开放生成任务上才用。

## 判定的宽容度

不做精确字符串匹配（那样会因为多说一句话就误判），
而是检查「关键事实有没有出现」：
- 符号名（should_strip_auth）
- 文件路径（sessions.py）
- 行号（可选，A 类任务才要求）

路径分隔符、大小写、多余空白全部归一化。
"""

import re

# ---------------------------------------------------------------- 归一化


def normalize(text):
    r"""归一化：小写、统一路径分隔符、压缩空白。

    为什么统一分隔符：Windows 上工具输出是 requests\sessions.py，
    Linux 上是 requests/sessions.py。不归一化的话，
    同一份评测集在两个系统上判定结果不一样。
    """
    if not text:
        return ""
    t = str(text).lower()
    t = t.replace("\\", "/")
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def _norm_item(s):
    return normalize(s)


# ---------------------------------------------------------------- 断言类型


def check_must_contain(answer_n, items):
    """答案里必须出现每一项。最常用，用于符号名、文件名。"""
    failed = []
    for it in items:
        if _norm_item(it) not in answer_n:
            failed.append(f"缺少关键事实: {it!r}")
    return failed


def check_must_contain_any(answer_n, groups):
    """每个 group 里至少命中一项。**组内是 OR，组间是 AND。**

    函数名里的 any 指的是**组内**任一写法命中即可，不是「任一组命中即可」——
    这个名字容易读反，用的时候务必看清。

    两种用法：

    1. 同一个东西有多种写法（一个 group 就够）：
       [["Session.send", "session.send"]]
    2. 一条完整答案要覆盖多个事实，每个事实允许有别名（多个 group）：
       [["init_poolmanager"], ["pool_connections"], ["pool_maxsize"]]
       上面这条例子的含义是：池创建函数要说、池数量参数要说、池容量参数要说，
       三个都要提到 —— 这正是「完整答案」的标准。

    判定偏严的代价：同义改写会漏判。B03 就是实例 ——
    agent 答出了 "num_pools=10 / maxsize=10"，但没写出参数名
    `pool_connections` / `pool_maxsize`，于是被判不及格。
    见 README「局限」一节。
    """
    failed = []
    for group in groups:
        if not any(_norm_item(it) in answer_n for it in group):
            failed.append(f"以下任意一项均未出现: {group}")
    return failed


def check_must_not_contain(answer_n, items):
    """答案里不能出现这些 —— 用于抓「张冠李戴」。

    比如问「重定向时剥 Authorization 的函数」，
    如果答案里出现 models.py 就说明它找错地方了。
    """
    failed = []
    for it in items:
        if _norm_item(it) in answer_n:
            failed.append(f"出现了不该有的内容: {it!r}")
    return failed


def check_regex(answer_n, patterns):
    """正则匹配。用于「必须给出行号」这类结构化要求。"""
    failed = []
    for p in patterns:
        try:
            if not re.search(p, answer_n):
                failed.append(f"未匹配到模式: {p}")
        except re.error as e:
            failed.append(f"评测集里的正则写错了（这是出题人的错）: {p} -> {e}")
    return failed


CHECKS = {
    "must_contain": check_must_contain,
    "must_contain_any": check_must_contain_any,
    "must_not_contain": check_must_not_contain,
    "regex": check_regex,
}


# ---------------------------------------------------------------- 主入口


def judge(answer, task):
    """判定一条任务的答案对不对。

    参数
        answer: agent 提交的答案文本（可能为空 —— 失败的运行答案就是空的）
        task:   评测集里的一条（dict），要有 "checks" 字段

    返回
        {
            "pass":      bool,
            "n_checks":  int,     总共断言了几条
            "failed":    list,    失败原因（人类可读）
        }
    """
    checks = task.get("checks") or {}
    answer_n = normalize(answer)

    failed = []
    n = 0

    for key, handler in CHECKS.items():
        items = checks.get(key)
        if not items:
            continue
        n += len(items)
        failed.extend(handler(answer_n, items))

    # 没配置任何断言 —— 这是出题人的错，必须显式报出来，
    # 不能让它悄悄通过，否则「完成率」这个数字就不可信了。
    if n == 0:
        return {
            "pass": False,
            "n_checks": 0,
            "failed": [f"任务 {task.get('id')} 没有配置任何断言，无法判定"],
        }

    # 答案为空时不必逐条报，一条说清楚
    if not answer_n:
        return {
            "pass": False,
            "n_checks": n,
            "failed": ["答案为空（agent 未能提交）"] + failed,
        }

    return {"pass": len(failed) == 0, "n_checks": n, "failed": failed}


def judge_many(results, tasks_by_id):
    """批量判定。results: [(task_id, answer), ...]"""
    out = {}
    for task_id, answer in results:
        out[task_id] = judge(answer, tasks_by_id.get(task_id, {"checks": {}}))
    return out


# ---------------------------------------------------------------- 自测

if __name__ == "__main__":
    task = {
        "id": "A01",
        "checks": {
            "must_contain": ["should_strip_auth", "sessions.py"],
            "must_contain_any": [["sessions.py:154", "154 行"]],
            "must_not_contain": ["models.py"],
            "regex": [r"sessions\.py"],
        },
    }

    cases = [
        ("函数 should_strip_auth 在 requests/sessions.py:154", True),
        ("函数 Should_Strip_Auth 在 requests\\sessions.py 第 154 行", True),  # 大小写+分隔符
        ("函数在 requests/sessions.py", False),                              # 缺行号
        ("函数 should_strip_auth 在 requests/models.py:154", False),         # 找错文件
        ("", False),                                                          # 空答案
        ("我不知道", False),
    ]

    print("断言判定器自测")
    print("-" * 62)
    ok = 0
    for answer, expect in cases:
        r = judge(answer, task)
        flag = "PASS" if r["pass"] == expect else "FAIL"
        ok += r["pass"] == expect
        print(f"  [{flag}] {str(answer)[:44]:46} -> pass={r['pass']} (期望 {expect})")
        if r["failed"]:
            print(f"         {r['failed'][0]}")
    print("-" * 62)
    print(f"{ok}/{len(cases)} 通过")

    # 没配断言必须报错而不是静默通过
    r = judge("anything", {"id": "X", "checks": {}})
    print("\n未配置断言时判定为:", r["pass"], "|", r["failed"][0])
'''
                    Agent 最终答案
                       │
                       ▼
                  normalize()
                       │
                       ▼
              ┌─────────────────┐
              │     judge()     │
              └─────────────────┘
                       │
         ┌─────────────┼─────────────┐
         ▼             ▼             ▼
   must_contain   must_contain_any   must_not_contain
         │             │             │
         └─────────────┼─────────────┘
                       │
                       ▼
                    regex
                       │
                       ▼
                 failed 列表
                       │
                ┌──────┴──────┐
                │             │
          failed == []   failed != []
                │             │
                ▼             ▼
              PASS           FAIL
'''