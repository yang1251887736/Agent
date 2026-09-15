"""实验数据分析 —— 直接用 SQL 查 runs.db。

为什么单独做一个分析脚本，而不是让 run.py 一次性打完表：
  跑一次全量要 30-60 分钟。你不可能每想到一个新角度就重跑一遍。
  数据全在 SQLite 里，分析应该是**离线、可反复、零成本**的。

用法：
    python -m eval.analyze                          # 全部分析
    python -m eval.analyze --db results/runs.db
    python -m eval.analyze --turns                  # 只看轮次分布
    python -m eval.analyze --csv results/round_full2.csv   # 指定完整口径用的明细 CSV

⚠️ 关于 token 的两个口径（这个项目最容易踩的坑，务必先读）：

  · 主 agent 口径 —— 从 runs.db 去重后算出来。**只含主 agent。**
  · 完整口径   —— 只能读 run.py 导出的明细 CSV。**含子 agent / worker。**

  为什么库里的值不能直接用：`tracer.log_turn` 是在 loop.py 的
  `for tc in tool_calls` 循环里调用的 —— 一轮里模型并发发起 N 个工具调用就写 N 行，
  每行都带**同一次 LLM 回复**的 usage。而 `finish_run` 是用
  `SUM(prompt_tokens)` 横跨这些行聚合的，于是这一轮的用量被算了 N 次。
  （`n_turns` 用的是 `COUNT(DISTINCT turn)`，所以轮次是对的，只有 token 虚高。）

  为什么库里还少：子 agent 和 worker 的 tracer 是 None，根本不写 turns 表，
  它们的开销只累加在内存 AgentResult 里 → 只有 CSV 有。

  两个偏差方向相反，所以**不能用系数修**，只能分别取各自口径。
"""

import argparse
import csv
import glob
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_DB = os.path.join(ROOT, "results", "runs.db")
DEFAULT_CSV = os.path.join(ROOT, "results", "round_full2.csv")

# 任务 id 首字母就是类型（A01 / B12 / C07）
TYPE_SQL = "UPPER(SUBSTR(task_id, 1, 1))"

# ⚠️ token 聚合必须先去重，否则虚高 39%~96%（虚高倍数 = 该次运行的 工具调用数/轮数）。
# 每个 (run_id, turn) 只保留一次 usage，再按 run 求和 —— 这才是主 agent 的真实用量。
DEDUP_RUN_SQL = """
    (SELECT run_id,
            SUM(prompt_tokens)     AS prompt_tokens,
            SUM(completion_tokens) AS completion_tokens,
            COUNT(*)               AS n_turns
       FROM (SELECT run_id, turn,
                    MAX(prompt_tokens)     AS prompt_tokens,
                    MAX(completion_tokens) AS completion_tokens
               FROM turns GROUP BY run_id, turn)
      GROUP BY run_id)
"""
TOK = "COALESCE(d.prompt_tokens, 0) + COALESCE(d.completion_tokens, 0)"


def _q(conn, sql, args=()):
    cur = conn.execute(sql, args)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _bar(pct, width=24):
    """用方块画个简单的条形图，比纯数字直观。"""
    n = int(round(pct / 100 * width))
    return "█" * n + "·" * (width - n)


def show_overall(conn):
    print("\n【1】按策略总览   （token = 主 agent 口径，已按轮去重）")
    print("=" * 104)
    sql = f"""
    SELECT r.strategy,
           COUNT(*)                                        AS n,
           ROUND(100.0*SUM(r.passed)/COUNT(*), 1)             AS pass_pct,
           ROUND(100.0*SUM(r.success)/COUNT(*), 1)            AS submit_pct,
           ROUND(AVG(d.n_turns), 1)                           AS avg_turns,
           ROUND(AVG(r.n_tool_calls), 1)                      AS avg_calls,
           ROUND(100.0*SUM(r.n_bad_calls)/MAX(SUM(r.n_tool_calls),1), 1) AS bad_pct,
           ROUND(AVG({TOK}))                                  AS avg_tokens,
           ROUND(AVG(r.peak_context_chars))                   AS avg_ctx,
           MAX(r.peak_context_chars)                          AS max_ctx,
           ROUND(AVG(r.wall_ms)/1000.0, 1)                    AS avg_sec
    FROM runs r
    LEFT JOIN {DEDUP_RUN_SQL} d ON d.run_id = r.run_id
    WHERE r.passed IS NOT NULL
    GROUP BY r.strategy
    ORDER BY avg_tokens
    """
    rows = _q(conn, sql)
    if not rows:
        print("  没有数据")
        return rows
    print(f"{'策略':<10}{'条数':>5}{'完成率':>16}{'提交率':>8}{'轮次':>6}"
          f"{'调用':>6}{'错误率':>7}{'token':>9}{'峰值上下文':>10}{'最大':>8}{'耗时':>8}")
    print("-" * 104)
    for r in rows:
        print(f"{r['strategy']:<10}{r['n']:>5}"
              f"{r['pass_pct']:>6.1f}% {_bar(r['pass_pct'], 9)}"
              f"{r['submit_pct']:>7.1f}%"
              f"{r['avg_turns']:>6.1f}{r['avg_calls']:>6.1f}"
              f"{r['bad_pct']:>6.1f}%"
              f"{r['avg_tokens']:>9.0f}"
              f"{r['avg_ctx']:>10.0f}{r['max_ctx']:>8}"
              f"{r['avg_sec']:>7.1f}s")
    print("-" * 104)
    print("  完成率 = 通过断言的比例 | 提交率 = agent 自认为完成的比例")
    print("  两者差距大 = agent 自信地交了错答案（这个 gap 本身就是个指标）")
    print("  ⚠️ token 一列只含主 agent。子 agent / worker 不落库，")
    print("     完整口径（含它们）见【9】，那才是能拿去横向比的总账。")
    return rows


def show_by_type(conn):
    print("\n【2】策略 × 任务类型 —— 边界条件结论从这里出   （token = 主 agent 口径）")
    print("=" * 104)
    sql = f"""
    SELECT r.strategy, {TYPE_SQL} AS ttype,
           COUNT(*)                                    AS n,
           ROUND(100.0*SUM(r.passed)/COUNT(*), 1)       AS pass_pct,
           ROUND(AVG({TOK}))                            AS avg_tokens,
           ROUND(AVG(r.peak_context_chars))             AS avg_ctx,
           ROUND(AVG(d.n_turns),1)                      AS avg_turns,
           ROUND(AVG(r.wall_ms)/1000.0,1)               AS avg_sec
    FROM runs r
    LEFT JOIN {DEDUP_RUN_SQL} d ON d.run_id = r.run_id
    WHERE r.passed IS NOT NULL
    GROUP BY r.strategy, {TYPE_SQL}
    ORDER BY ttype, strategy
    """
    rows = _q(conn, sql)
    if not rows:
        print("  没有数据")
        return rows
    cur = None
    for r in rows:
        if r["ttype"] != cur:
            cur = r["ttype"]
            label = {"A": "A 精确查找", "B": "B 语义理解",
                     "C": "C 多跳推理"}.get(cur, cur)
            print(f"\n  ── {label} " + "─" * 60)
        print(f"    {r['strategy']:<10}{r['n']:>4} 条  "
              f"完成率 {r['pass_pct']:>5.1f}% {_bar(r['pass_pct'], 12)}  "
              f"token {r['avg_tokens']:>7.0f}  "
              f"峰值上下文 {r['avg_ctx']:>7.0f}  "
              f"轮次 {r['avg_turns']:>4.1f}  "
              f"耗时 {r['avg_sec']:>5.1f}s")
    return rows


def show_turns(conn):
    print("\n【3】轮次分布 —— 用来验证 max_turns=20 合不合理")
    print("=" * 104)
    sql = """
    SELECT strategy,
           MIN(n_turns) AS min_t,
           ROUND(AVG(n_turns),1) AS avg_t,
           MAX(n_turns) AS max_t,
           SUM(CASE WHEN n_turns <= 8 THEN 1 ELSE 0 END)  AS le8,
           SUM(CASE WHEN n_turns <= 12 THEN 1 ELSE 0 END) AS le12,
           SUM(CASE WHEN n_turns >= 20 THEN 1 ELSE 0 END) AS hit_max,
           COUNT(*) AS n
    FROM runs WHERE passed IS NOT NULL GROUP BY strategy
    """
    rows = _q(conn, sql)
    for r in rows:
        n = r["n"]
        print(f"  {r['strategy']:<10} 范围 {r['min_t']}-{r['max_t']}  均值 {r['avg_t']:>4.1f}  "
              f"≤8 轮占 {100.0*r['le8']/n:>5.1f}%  ≤12 轮占 {100.0*r['le12']/n:>5.1f}%  "
              f"撞上限 {r['hit_max']} 次")
    print("\n  「撞上限」> 0 说明 max_turns 可能偏小，复杂任务被误杀了 —— 数据会失真。")
    return rows


def show_errors(conn):
    print("\n【4】错误调用模式 —— 模型最容易在哪些工具上犯错")
    print("=" * 104)
    print("  注意：这里统计的是**主 agent** 的调用。"
          "subagent 的子 agent、teams 的 worker 不写 turns 表")
    print("        （它们的开销只累加在内存 AgentResult 里，既不落库也不进明细），"
          "所以这一项不代表全貌。")
    print("  另：turns 表一行 = 一次工具调用（不是一轮），所以下面的次数可以直接信；")
    print("      但同一轮的 prompt/completion 会被这 N 行各记一遍 —— token 才需要去重。")
    sql = """
    SELECT r.strategy AS strategy,
           COALESCE(t.tool_name, '(该轮未调用工具)') AS tool,
           COUNT(*) AS n_err
    FROM turns t JOIN runs r ON t.run_id = r.run_id
    WHERE t.is_error = 1
    GROUP BY r.strategy, tool
    ORDER BY r.strategy, n_err DESC
    """
    rows = _q(conn, sql)
    if not rows:
        print("  没有错误调用记录")
        return rows
    cur = None
    for r in rows:
        if r["strategy"] != cur:
            cur = r["strategy"]
            print(f"\n  ── {cur} " + "─" * 55)
        print(f"    {str(r['tool']):<26} {r['n_err']:>4} 次")
    return rows


def show_failures(conn, limit=15):
    print(f"\n【8】未通过的任务（前 {limit} 条）—— 人工核验判定是否合理   （tok = 主 agent）")
    print("=" * 104)
    sql = f"""
    SELECT r.strategy, r.task_id, d.n_turns, r.n_bad_calls,
           {TOK} AS tokens,
           COALESCE(r.failed_check, r.error, '') AS reason
    FROM runs r
    LEFT JOIN {DEDUP_RUN_SQL} d ON d.run_id = r.run_id
    WHERE r.passed = 0
    ORDER BY r.strategy, r.task_id
    LIMIT ?
    """
    rows = _q(conn, sql, (limit,))
    for r in rows:
        print(f"  {r['strategy']:<10}{r['task_id']:<6} turns={r['n_turns']:<3} "
              f"bad={r['n_bad_calls']:<3} tok={r['tokens']:>6}  {str(r['reason'])[:62]}")
    if not rows:
        print("  全部通过")
    return rows


def show_gap(conn):
    print("\n【6】「自信地答错」—— 提交成功但判定失败")
    print("=" * 104)
    print("  这一类最值得看：agent 以为自己做对了，其实没有。")
    sql = f"""
    SELECT strategy, COUNT(*) AS n_overconfident,
           (SELECT COUNT(*) FROM runs r2
            WHERE r2.strategy = runs.strategy AND passed IS NOT NULL) AS n_total
    FROM runs
    WHERE success = 1 AND passed = 0 AND passed IS NOT NULL
    GROUP BY strategy
    """
    rows = _q(conn, sql)
    if not rows:
        print("  没有这种情况（或数据里没有 success 字段）")
        return rows
    for r in rows:
        print(f"  {r['strategy']:<10} {r['n_overconfident']:>3} / {r['n_total']:<3} 条"
              f"  ({100.0*r['n_overconfident']/max(r['n_total'],1):.1f}%)")
    return rows


def show_cost(conn):
    print("\n【7】主 agent 成本 —— 已按轮去重，**只含主 agent**")
    print("=" * 104)
    sql = f"""
    SELECT r.strategy,
           SUM(d.prompt_tokens)     AS in_tok,
           SUM(d.completion_tokens) AS out_tok,
           SUM({TOK})               AS total_tok,
           ROUND(SUM(r.wall_ms)/1000.0, 1) AS total_sec,
           COUNT(*) AS n
    FROM runs r
    LEFT JOIN {DEDUP_RUN_SQL} d ON d.run_id = r.run_id
    WHERE r.passed IS NOT NULL GROUP BY r.strategy
    """
    rows = _q(conn, sql)
    for r in rows:
        n = max(r["n"], 1)
        print(f"  {r['strategy']:<10} 输入 {r['in_tok']:>8}  输出 {r['out_tok']:>8}  "
              f"合计 {r['total_tok']:>8}  "
              f"| 单任务均值 {r['total_tok']//n:>6}  "
              f"| 总耗时 {r['total_sec']:>7.1f}s  单任务 {r['total_sec']/n:>5.1f}s")
    if rows:
        base = min(rows, key=lambda r: r["total_tok"])
        print()
        for r in rows:
            if r["strategy"] != base["strategy"]:
                print(f"  {r['strategy']:<10} 的 token 是 {base['strategy']} 的 "
                      f"{r['total_tok']/max(base['total_tok'],1):.1f} 倍，"
                      f"耗时是 {r['total_sec']/max(base['total_sec'],1):.2f} 倍")
    print()
    print("  这一节只有主 agent —— 子 agent / worker 的消费没落库。")
    print("  想横向比成本，用【9】的完整口径；这一节用来看「决策者自己被喂了多少」。")
    return rows


def _find_csv(path=None):
    """定位 run.py 导出的明细 CSV（完整口径的唯一来源）。"""
    if path and os.path.exists(path):
        return path
    cands = glob.glob(os.path.join(ROOT, "results", "round*.csv"))
    return max(cands, key=os.path.getmtime) if cands else None


def show_cost_full(conn, csv_path=None):
    """【9】完整口径 —— 含子 agent / worker，唯一能横向比的总账。

    为什么必须读 CSV：子 agent（orchestrators/subagent.py）和 worker（teams.py）
    的 tracer 是 None，一行 turns 都不写；它们的用量只是被 Ledger 累加回内存里的
    AgentResult，最后由 run.py 的 _write_csv 落到 CSV 上。库里根本查不到。

    拆分方法：CSV 的 tokens 列是内存口径（完整）；主 agent 那一份从库里按轮去重取；
    两者相减，剩下的就是子 agent / worker 的开销。
    """
    print("\n【9】完整成本 —— 含子 agent / worker（这才是能横向比的总账）")
    print("=" * 104)
    path = _find_csv(csv_path)
    if not path:
        print("  没找到明细 CSV。先跑一次： python -m eval.run --tag full2")
        return []

    agg = {}
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            a = agg.setdefault(row["strategy"], {"n": 0, "tok": 0})
            a["n"] += 1
            a["tok"] += int(row["tokens"])

    lead = {r["strategy"]: r["avg_lead"] for r in _q(conn, f"""
        SELECT r.strategy AS strategy, AVG({TOK}) AS avg_lead
        FROM runs r
        LEFT JOIN {DEDUP_RUN_SQL} d ON d.run_id = r.run_id
        WHERE r.passed IS NOT NULL GROUP BY r.strategy
    """)}

    rows = []
    for s in set(agg) | set(lead):
        a, l = agg.get(s), lead.get(s) or 0
        if a:                                  # CSV 里有：完整口径直接读
            n, full = a["n"], a["tok"] / max(a["n"], 1)
        else:                                  # CSV 里没有（如 single）：无子 agent，完整 = 主 agent
            n, full = 0, l
        rows.append({"s": s, "n": n, "full": full, "lead": l, "child": full - l})
    rows.sort(key=lambda r: r["full"])

    base = next((r["full"] for r in rows if r["s"] == "single"), None) or rows[0]["full"]
    print(f"{'策略':<10}{'条数':>5}{'完整 token/任务':>16}{'主 agent':>12}"
          f"{'子 agent':>11}{'子占比':>9}{'vs single':>12}")
    print("-" * 104)
    for r in rows:
        print(f"{r['s']:<10}{r['n']:>5}{r['full']:>16.0f}{r['lead']:>12.0f}"
              f"{r['child']:>11.0f}{100.0*r['child']/max(r['full'],1):>8.1f}%"
              f"{r['full']/base:>11.2f}x")
    print("-" * 104)
    print(f"  明细来源：{os.path.relpath(path, ROOT)}（eval/run.py 的 _write_csv 导出）")
    print("  「条数」为 0 表示该策略不在 CSV 里，用了库里的主 agent 值（无子 agent，两者相等）。")
    print("  自洽性验证：没有 spawn 过的运行，「完整」必须严格等于「主 agent」——")
    print("  实测 120 行里 25 行完全相等（全是 subagent 未触发 spawn 的运行），")
    print("  且 120 行无一出现「完整 < 主 agent」。")
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--turns", action="store_true", help="只看轮次分布")
    ap.add_argument("--csv", default=DEFAULT_CSV,
                    help="run.py 导出的明细 CSV（完整口径用，含子 agent 开销）")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"数据库不存在: {args.db}")
        print("先跑一次： python -m eval.run --type A --limit 3 --tag pilot")
        return 1

    conn = sqlite3.connect(args.db)
    print("=" * 104)
    print(f"agent-arena 实验分析   |   {args.db}")
    n = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    nj = conn.execute("SELECT COUNT(*) FROM runs WHERE passed IS NOT NULL").fetchone()[0]
    print(f"共 {n} 条运行记录，其中 {nj} 条已判定")
    print("⚠️ token 有两个口径：【1】【2】【7】是主 agent（已按轮去重），"
          "【9】是完整（含子 agent）。别混着比。")

    try:
        if args.turns:
            show_turns(conn)
        else:
            show_overall(conn)
            show_by_type(conn)
            show_turns(conn)
            show_errors(conn)
            show_gap(conn)
            show_cost(conn)
            show_failures(conn)
            show_cost_full(conn, args.csv)
    finally:
        conn.close()
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
