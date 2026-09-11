"""重新判定 —— 从数据库里读答案，用断言重新判一遍，回写 passed。

## 为什么要有这个

判定是**零成本的纯断言**，不该和「跑实验」绑在一起。

第一次全量跑到 120/180 时 API 余额耗尽，进程被中断，
`run.py` 里那个「最后统一判定」的 `_save_judgments()` 根本没执行 ——
60 条有效运行的答案全在库里，却一条都没判分。

有了这个脚本，任何时刻都能补判定，不用重跑实验（重跑要花钱和时间，
补判定只要几毫秒）。

这也是「断言判定」相对「LLM 打分」的一个实际好处：
判定可以随时离线重放，结果永远一致。

用法：
    python -m eval.rejudge                       # 重判所有
    python -m eval.rejudge --strategy single     # 只重判某个策略
    python -m eval.rejudge --dry                 # 只看不写
"""

import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eval.judge import judge          # noqa: E402
from eval.run import load_tasks       # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_DB = os.path.join(ROOT, "results", "runs.db")


def rejudge(db, tasks_by_id, strategy=None, dry=False):
    conn = sqlite3.connect(db)
    # 兼容旧库：没有 passed / failed_check 列就补上
    for col, decl in [("passed", "INTEGER"), ("failed_check", "TEXT"),
                      ("error", "TEXT")]:
        try:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {col} {decl}")
        except sqlite3.OperationalError:
            pass

    sql = "SELECT run_id, strategy, task_id, success, final_answer FROM runs"
    args = ()
    if strategy:
        sql += " WHERE strategy=?"
        args = (strategy,)

    n = n_pass = n_skip = 0
    for run_id, strat, task_id, success, answer in conn.execute(sql, args).fetchall():
        task = tasks_by_id.get(task_id)
        if task is None:
            n_skip += 1
            continue
        r = judge(answer or "", task)
        n += 1
        n_pass += 1 if r["pass"] else 0
        if not dry:
            conn.execute(
                "UPDATE runs SET passed=?, failed_check=? WHERE run_id=?",
                (1 if r["pass"] else 0,
                 (r["failed"][0] if r["failed"] else "")[:500], run_id),
            )
    if not dry:
        conn.commit()
    conn.close()
    return n, n_pass, n_skip


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--strategy", default=None)
    ap.add_argument("--tasks", default=os.path.join(HERE, "tasks.jsonl"))
    ap.add_argument("--dry", action="store_true", help="只看结果不写库")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"数据库不存在: {args.db}")
        return 1

    tasks = load_tasks(args.tasks)
    tasks_by_id = {t["id"]: t for t in tasks}

    n, n_pass, n_skip = rejudge(args.db, tasks_by_id, args.strategy, args.dry)

    print(f"重新判定：{n} 条"
          + (f"（策略 {args.strategy}）" if args.strategy else ""))
    print(f"  通过 {n_pass} / {n}"
          + (f"  ({100.0 * n_pass / max(n, 1):.1f}%)" if n else ""))
    if n_skip:
        print(f"  跳过 {n_skip} 条（评测集里没有对应任务）")
    if args.dry:
        print("  (dry run，未写入)")

    # 按策略 × 类型汇总
    print()
    conn = sqlite3.connect(args.db)
    rows = conn.execute("""
        SELECT strategy, UPPER(SUBSTR(task_id,1,1)) AS ttype,
               COUNT(*) n, SUM(passed) p
        FROM runs WHERE passed IS NOT NULL
        GROUP BY strategy, ttype ORDER BY strategy, ttype
    """).fetchall()
    conn.close()
    if rows:
        print(f"{'策略':<10}{'类型':<6}{'条数':>5}{'通过':>6}{'完成率':>9}")
        print("-" * 40)
        for strat, ttype, n, p in rows:
            print(f"{strat:<10}{ttype:<6}{n:>5}{p or 0:>6}"
                  f"{100.0 * (p or 0) / n:>8.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
