# 评测程序说“我要跑 teams”，这个文件负责告诉它：teams 对应 teams.run 函数。
"""三种编排策略的统一入口。

评测脚本只需要按名字取策略，不关心内部实现 ——
这样加第四种策略时，eval/run.py 一个字都不用改。

## 为什么三者必须共用同一套基础工具

这是对照实验的基本要求：**只有「编排方式」这一个变量**。
如果 single 用 6 个工具、teams 用 8 个，跑出差异你没法归因 ——
到底是因为并行，还是因为工具多？

subagent 给主 agent 多加了一个 spawn，teams 多加了一层规划，
这些「多出来的能力」正是被测试的对象本身，不算额外变量。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrators import single, subagent, teams   # noqa: E402

STRATEGIES = {
    single.NAME: single.run,
    subagent.NAME: subagent.run,
    teams.NAME: teams.run,
}

DESCRIPTIONS = {
    "single": "单 agent 基线：所有上下文堆在一条 messages 里",
    "subagent": "主 agent 串行派生子 agent，只回收结论，上下文隔离",
    "teams": "拆成独立子任务并行推进，各自独立副本，最后汇总",
}


def get(name):
    if name not in STRATEGIES:
        raise KeyError(f"未知策略 {name!r}，可选：{', '.join(STRATEGIES)}")
    return STRATEGIES[name]


def list_all():
    return list(STRATEGIES)


if __name__ == "__main__":
    print("可用策略：")
    for name in list_all():
        print(f"  {name:10s} {DESCRIPTIONS[name]}")
