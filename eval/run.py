"""批量评测运行器。

一次跑：策略 × 任务，结果全部落 SQLite，最后出对比表。

用法：
    # 跑全部（3 策略 × 60 任务 = 180 次，约 1-2 小时）
    python -m eval.run

    # 只跑某几个策略
    python -m eval.run --strategies single subagent

    # 只跑某类任务 / 指定条数（先小规模验证用）
    python -m eval.run --type A --limit 5

    # 断点续跑：已完成的任务会跳过
    python -m eval.run --resume

## 关于实验公平性

1. **同一套工具**：三种编排共用 tools.py 里那 6 个基础工具。
   subagent 多一个 spawn、teams 多一层规划 —— 这些「多出来的能力」
   正是被测试的对象本身，不算额外变量。
2. **temperature = 0**：llm.py 里写死。否则同一条任务两次跑出不同结果，
   对比数据不成立。
3. **同一份评测集、同一个仓库副本、同一个 commit**。
4. **成本口径统一**：subagent 的子 agent、teams 的 worker 开销全部计入总账
   （但 peak_context_chars 只记主/lead 的 —— 那个指标衡量的正是
   「决策者的上下文有没有被撑爆」）。
5. **判定用断言不用 LLM**：判定标准跑之前就定死，跑完之后改不了。
"""

import argparse
import csv
import json
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runtime.tools import set_repo_root, current_repo_root  # noqa: E402
from runtime.tracer import Tracer                            # noqa: E402
from eval.judge import judge                                 # noqa: E402
from orchestrators import STRATEGIES, list_all               # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TASKS = os.path.join(HERE, "tasks.jsonl")
DB = os.path.join(ROOT, "results", "runs.db")
RESULTS_DIR = os.path.join(ROOT, "results")


def load_tasks(path=TASKS, task_type=None, limit=None, only_ids=None):
    tasks = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            t = json.loads(line)
            if task_type and t.get("type") != task_type:
                continue
            if only_ids and t["id"] not in only_ids:
                continue
            tasks.append(t)
    if limit:
        tasks = tasks[:limit]
    return tasks


def task_type_of(task_id, tasks_by_id):
    return tasks_by_id.get(task_id, {}).get("type", "?")


# ---------------------------------------------------------------- 主流程


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategies", nargs="*", default=list_all(),
                    help=f"要跑的策略，可选：{list_all()}")
    ap.add_argument("--type", default=None, help="只跑某类任务：A / B / C")
    ap.add_argument("--limit", type=int, default=None, help="只跑前 N 条")
    ap.add_argument("--ids", nargs="*", default=None, help="只跑指定任务 id")
    ap.add_argument("--repo", default=None, help="仓库副本路径")
    ap.add_argument("--db", default=DB, help="结果数据库路径")
    ap.add_argument("--resume", action="store_true", help="跳过已完成的 (策略,任务)")
    ap.add_argument("--timeout", type=int, default=600, help="单任务超时秒数")
    ap.add_argument("--workers", type=int, default=3, help="teams 的 worker 数")
    ap.add_argument("--tag", default="", help="实验批次标记，写进 CSV 文件名")
    args = ap.parse_args()

    repo = args.repo or os.path.join(ROOT, "sandbox", "requests_src")
    set_repo_root(repo)

    tasks = load_tasks(task_type=args.type, limit=args.limit, only_ids=args.ids)
    if not tasks:
        print("没有任务可跑，检查过滤条件")
        return 1
    tasks_by_id = {t["id"]: t for t in tasks}

    tracer = Tracer(args.db)

    done = set()
    if args.resume:
        import sqlite3
        conn = sqlite3.connect(args.db)
        try:
            for s, tid in conn.execute(
                    "SELECT strategy, task_id FROM runs WHERE success IS NOT NULL"):
                done.add((s, tid))
        finally:
            conn.close()
        if done:
            print(f"续跑模式：已有 {len(done)} 条记录，将跳过")

    total = len(args.strategies) * len(tasks)
    print("=" * 72)
    print("agent-arena 批量评测")
    print("=" * 72)
    print(f"  仓库副本  : {current_repo_root()}")
    print(f"  评测集    : {len(tasks)} 条"
          + (f"（类型 {args.type}）" if args.type else ""))
    print(f"  策略      : {', '.join(args.strategies)}")
    print(f"  预计运行  : {total} 次")
    print(f"  结果库    : {args.db}")
    print("=" * 72)
    print()

    n_done = 0
    n_skip = 0
    t_all = time.time()
    rows = []

    for si, strategy in enumerate(args.strategies, 1):
        fn = STRATEGIES[strategy]
        print(f"── 策略 {si}/{len(args.strategies)}: {strategy} "
              f"{'─' * (50 - len(strategy))}")

        for ti, task in enumerate(tasks, 1):
            tid = task["id"]
            if (strategy, tid) in done:
                n_skip += 1
                print(f"   [{ti:2d}/{len(tasks)}] {tid} 已跑过，跳过")
                continue

            label = f"   [{ti:2d}/{len(tasks)}] {tid}"
            try:
                kw = {}
                if strategy == "teams":
                    kw["n_workers"] = args.workers
                    kw["isolate"] = False     # 评测集全是只读分析任务，无需副本隔离
                r = fn(task["question"], task_id=tid, tracer=tracer,
                       timeout_s=args.timeout, **kw)
            except KeyboardInterrupt:
                print("\n\n用户中断。已经跑完的都在库里，加 --resume 可续跑。")
                return 130
            except Exception as e:
                print(f"{label} 崩溃: {type(e).__name__}: {e}")
                traceback.print_exc()
                rows.append(_row(strategy, task, None, str(e)))
                n_done += 1
                continue

            j = judge(r.answer, task)
            row = _row(strategy, task, r, "", j)
            rows.append(row)

            mark = "OK " if j["pass"] else "MISS"
            extra = ""
            if getattr(r, "n_spawns", 0):
                extra = f" spawn={r.n_spawns}"
            if getattr(r, "n_workers", 0):
                extra = f" workers={r.n_workers}"
            print(f"{label} {mark} turns={r.n_turns:2d} calls={r.n_tool_calls:2d} "
                  f"bad={r.n_bad_calls} tok={r.prompt_tokens + r.completion_tokens:6d} "
                  f"ctx={r.peak_context_chars:6d} {r.wall_ms:6d}ms{extra}")
            if not j["pass"] and j["failed"]:
                print(f"        判错原因: {j['failed'][0][:90]}")

            n_done += 1

        print()

    # ---------------------------------------------------------- 判定结果入库
    _save_judgments(args.db, rows)

    # ---------------------------------------------------------- 出表
    elapsed = time.time() - t_all
    print("=" * 72)
    print(f"完成 {n_done} 次运行（跳过 {n_skip}），耗时 {elapsed / 60:.1f} 分钟")
    print("=" * 72)

    _write_csv(rows, args.tag)
    _print_tables(rows, tasks_by_id)

    return 0


def _row(strategy, task, r, error="", j=None):
    """把一次运行整理成一行的统计。r 为 None 表示崩溃。"""
    if r is None:
        return {
            "strategy": strategy, "task_id": task["id"], "task_type": task["type"],
            "passed": False, "agent_success": False,
            "n_turns": 0, "n_tool_calls": 0, "n_bad_calls": 0,
            "tokens": 0, "peak_context_chars": 0, "wall_ms": 0,
            "error": error, "failed_check": error,
        }
    j = j or judge(r.answer, task)
    return {
        "strategy": strategy,
        "task_id": task["id"],
        "task_type": task["type"],
        "passed": j["pass"],
        "agent_success": r.success,
        "n_turns": r.n_turns,
        "n_tool_calls": r.n_tool_calls,
        "n_bad_calls": r.n_bad_calls,
        "tokens": r.prompt_tokens + r.completion_tokens,
        "peak_context_chars": r.peak_context_chars,
        "wall_ms": r.wall_ms,
        "error": r.error,
        "failed_check": j["failed"][0] if j["failed"] else "",
    }


# ---------------------------------------------------------------- 输出


def _save_judgments(db, rows):
    """判定结果回写数据库 —— 这样 runs 表里既有运行指标也有对错。"""
    import sqlite3
    os.makedirs(os.path.dirname(db), exist_ok=True)
    conn = sqlite3.connect(db)
    # 旧库可能没有这几列（评定/失败原因是后来加的），补上；已存在就忽略
    for col, decl in [("passed", "INTEGER"), ("failed_check", "TEXT"),
                      ("error", "TEXT")]:
        try:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {col} {decl}")
        except sqlite3.OperationalError:
            pass
    for r in rows:
        conn.execute(
            "UPDATE runs SET passed=?, failed_check=? WHERE strategy=? AND task_id=?",
            (1 if r["passed"] else 0, r["failed_check"][:500],
             r["strategy"], r["task_id"]),
        )
    conn.commit()
    conn.close()


def _write_csv(rows, tag=""):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    name = f"round{'_' + tag if tag else ''}.csv"
    path = os.path.join(RESULTS_DIR, name)
    cols = ["strategy", "task_id", "task_type", "passed", "agent_success",
            "n_turns", "n_tool_calls", "n_bad_calls", "tokens",
            "peak_context_chars", "wall_ms", "error", "failed_check"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"明细已写入: {path}")
    return path


def _agg(rows):
    """按策略聚合。"""
    out = {}
    for r in rows:
        s = out.setdefault(r["strategy"], {"n": 0, "pass": 0, "turns": 0,
                                           "calls": 0, "bad": 0, "tokens": 0,
                                           "ctx": 0, "peak_ctx": 0, "wall": 0,
                                           "agent_ok": 0})
        s["n"] += 1
        s["pass"] += 1 if r["passed"] else 0
        s["agent_ok"] += 1 if r["agent_success"] else 0
        s["turns"] += r["n_turns"]
        s["calls"] += r["n_tool_calls"]
        s["bad"] += r["n_bad_calls"]
        s["tokens"] += r["tokens"]
        s["ctx"] += r["peak_context_chars"]
        s["peak_ctx"] = max(s["peak_ctx"], r["peak_context_chars"])
        s["wall"] += r["wall_ms"]
    return out


def _agg_by_type(rows):
    out = {}
    for r in rows:
        k = (r["strategy"], r["task_type"])
        s = out.setdefault(k, {"n": 0, "pass": 0, "tokens": 0, "ctx": 0,
                               "calls": 0, "bad": 0, "turns": 0, "wall": 0})
        s["n"] += 1
        s["pass"] += 1 if r["passed"] else 0
        s["tokens"] += r["tokens"]
        s["ctx"] += r["peak_context_chars"]
        s["calls"] += r["n_tool_calls"]
        s["bad"] += r["n_bad_calls"]
        s["turns"] += r["n_turns"]
        s["wall"] += r["wall_ms"]
    return out


def _print_tables(rows, tasks_by_id):
    if not rows:
        return

    agg = _agg(rows)
    print()
    print("【总表】按策略")
    print("-" * 92)
    hdr = (f"{'策略':<10}{'完成率':>9}{'提交率':>8}{'平均轮次':>9}"
           f"{'平均调用':>9}{'错误率':>8}{'平均token':>11}{'平均峰值上下文':>15}{'平均耗时':>10}")
    print(hdr)
    print("-" * 92)
    for s, d in agg.items():
        n = d["n"]
        print(f"{s:<10}"
              f"{100.0 * d['pass'] / n:>8.1f}%"
              f"{100.0 * d['agent_ok'] / n:>7.1f}%"
              f"{d['turns'] / n:>9.1f}"
              f"{d['calls'] / n:>9.1f}"
              f"{100.0 * d['bad'] / max(d['calls'], 1):>7.1f}%"
              f"{d['tokens'] / n:>11.0f}"
              f"{d['ctx'] / n:>15.0f}"
              f"{d['wall'] / n / 1000:>9.1f}s")
    print("-" * 92)
    print("  完成率 = 答案通过断言的比例 | 提交率 = agent 自己认为完成了的比例")
    print("  错误率 = 无效调用 / 总调用（工具不存在、参数错、执行失败）")

    by_type = _agg_by_type(rows)
    types = sorted({k[1] for k in by_type})
    if len(types) > 1:
        print()
        print("【细分表】策略 × 任务类型 —— 边界条件结论从这里出")
        print("-" * 92)
        print(f"{'策略':<10}{'类型':<6}{'条数':>5}{'完成率':>9}"
              f"{'平均token':>11}{'平均峰值上下文':>15}{'平均轮次':>9}{'平均耗时':>10}")
        print("-" * 92)
        for s in agg:
            for t in types:
                d = by_type.get((s, t))
                if not d:
                    continue
                n = d["n"]
                print(f"{s:<10}{t:<6}{n:>5}"
                      f"{100.0 * d['pass'] / n:>8.1f}%"
                      f"{d['tokens'] / n:>11.0f}"
                      f"{d['ctx'] / n:>15.0f}"
                      f"{d['turns'] / n:>9.1f}"
                      f"{d['wall'] / n / 1000:>9.1f}s")
        print("-" * 92)

    # 失败样例，方便人工核验判定是否合理
    bad = [r for r in rows if not r["passed"]]
    if bad:
        print()
        print(f"【未通过 {len(bad)} 条】前 10 条（用于人工核验判定是否合理）")
        for r in bad[:10]:
            reason = r["failed_check"] or r["error"] or "?"
            print(f"  {r['strategy']:<10} {r['task_id']:<5} {reason[:70]}")


if __name__ == "__main__":
    sys.exit(main())
