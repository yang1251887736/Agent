"""执行追踪 —— 每一轮的工具、参数、结果、token、耗时、上下文长度全落 SQLite。

执行追踪把每一轮的工具、参数、结果、token、耗时、上下文长度落进 SQLite，解决两个问题：

1. **实验数据源** —— 你最后那张对比表的所有数字都从这里出，
   不落库的话 180 次运行（3 编排 × 60 任务）的结果只能靠 print 抓，迟早丢。
2. 提供一张真实被查询、被聚合的表，正好能当作 SQL 能力的项目佐证。

## 表结构

runs:  一次运行的汇总（谁跑的、什么策略、哪条任务、成功没有、总共烧了多少）
turns: 每一轮的明细（第几轮、调了什么工具、参数、结果、token、耗时、上下文长度）

## 用法

    tracer = Tracer("results/runs.db")
    run_id = tracer.start_run(strategy="single", task_id="A01")
    tracer.log_turn(run_id, turn=1, ...)
    tracer.finish_run(run_id, success=True, final_answer="...", wall_ms=12345)
"""

import contextlib
import json
import os
import sqlite3
import time
import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    strategy      TEXT NOT NULL,
    task_id       TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT,
    wall_ms       INTEGER,
    n_turns       INTEGER,
    n_tool_calls  INTEGER,
    n_bad_calls   INTEGER,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    peak_context_chars INTEGER,
    success       INTEGER,
    error         TEXT,          -- 失败原因：超时 / 超过最大轮次 / 始终未 submit
    final_answer  TEXT
);

CREATE TABLE IF NOT EXISTS turns (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    turn          INTEGER,
    tool_name     TEXT,
    tool_args     TEXT,
    tool_result   TEXT,
    is_error      INTEGER DEFAULT 0,
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    context_chars INTEGER,
    elapsed_ms    INTEGER,
    ts            TEXT
);

CREATE INDEX IF NOT EXISTS idx_turns_run ON turns(run_id);
CREATE INDEX IF NOT EXISTS idx_runs_strategy ON runs(strategy);
"""


class Tracer:
    def __init__(self, db_path="results/runs.db"):
        d = os.path.dirname(db_path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.db_path = db_path
        self._t0 = {}
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextlib.contextmanager
    def _conn(self):
        """连接用完必须显式 close，否则 Windows 上删不掉 .db 文件。"""
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def start_run(self, strategy, task_id):
        run_id = f"{strategy}_{task_id}_{uuid.uuid4().hex[:6]}"
        with self._conn() as c:
            c.execute(
                "INSERT INTO runs (run_id, strategy, task_id, started_at) VALUES (?,?,?,?)",
                (run_id, strategy, task_id, time.strftime("%Y-%m-%d %H:%M:%S")),
            )
        self._t0[run_id] = time.time()
        return run_id

    def log_turn(
        self,
        run_id,
        turn,
        tool_name=None,
        tool_args=None,
        tool_result=None,
        is_error=False,
        usage=None,
        context_chars=None,
        elapsed_ms=None,
    ):
        usage = usage or {}
        with self._conn() as c:
            c.execute(
                """INSERT INTO turns
                   (run_id, turn, tool_name, tool_args, tool_result, is_error,
                    prompt_tokens, completion_tokens, context_chars, elapsed_ms, ts)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    turn,
                    tool_name,
                    json.dumps(tool_args, ensure_ascii=False) if tool_args is not None else None,
                    (tool_result or "")[:4000],   # 截断，别把整个文件塞进库
                    1 if is_error else 0,
                    usage.get("prompt_tokens", 0),
                    usage.get("completion_tokens", 0),
                    context_chars,
                    elapsed_ms,
                    time.strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )

    def finish_run(self, run_id, success, final_answer="", n_bad_calls=0, error=""):
        started = self._t0.pop(run_id, None)
        wall_ms = int((time.time() - started) * 1000) if started else None

        with self._conn() as c:
            agg = c.execute(
                """SELECT COUNT(*), SUM(is_error),
                          SUM(prompt_tokens), SUM(completion_tokens),
                          MAX(context_chars)
                   FROM turns WHERE run_id=?""",
                (run_id,),
            ).fetchone()
            n_tool = c.execute(
                "SELECT COUNT(*) FROM turns WHERE run_id=? AND tool_name IS NOT NULL",
                (run_id,),
            ).fetchone()[0]
            n_turns = c.execute(
                "SELECT COUNT(DISTINCT turn) FROM turns WHERE run_id=?", (run_id,)
            ).fetchone()[0]

            c.execute(
                """UPDATE runs SET
                     finished_at=?, wall_ms=?, n_turns=?, n_tool_calls=?, n_bad_calls=?,
                     prompt_tokens=?, completion_tokens=?, peak_context_chars=?,
                     success=?, error=?, final_answer=?
                   WHERE run_id=?""",
                (
                    time.strftime("%Y-%m-%d %H:%M:%S"),
                    wall_ms,
                    n_turns or 0,
                    n_tool or 0,
                    n_bad_calls,
                    agg[2] or 0,
                    agg[3] or 0,
                    agg[4] or 0,
                    1 if success else 0,
                    (error or "")[:500],
                    (final_answer or "")[:8000],
                    run_id,
                ),
            )
        return wall_ms

    # ---------- 出报告用的聚合查询 ----------

    def summary_by_strategy(self):
        """返回每种编排策略的汇总。这就是你 README 里那张表的来源。"""
        sql = """
        SELECT
            strategy,
            COUNT(*)                          AS n_tasks,
            ROUND(100.0 * SUM(success) / COUNT(*), 1)                AS completion_pct,
            ROUND(1.0 * SUM(n_bad_calls) / MAX(SUM(n_tool_calls),1), 4) AS bad_call_rate,
            ROUND(AVG(n_turns), 2)            AS avg_turns,
            ROUND(AVG(prompt_tokens + completion_tokens)) AS avg_tokens,
            ROUND(AVG(wall_ms) / 1000.0, 1)   AS avg_seconds,
            ROUND(AVG(peak_context_chars))    AS avg_peak_ctx_chars
        FROM runs
        GROUP BY strategy
        """
        with self._conn() as c:
            cur = c.execute(sql)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def summary_by_strategy_and_type(self, task_type_of):
        """按「策略 × 任务类型」细分 —— 边界条件结论从这里出。

        task_type_of: 函数，task_id -> "A" / "B" / "C"
        """
        rows = []
        with self._conn() as c:
            cur = c.execute(
                """SELECT strategy, task_id, success, n_tool_calls, n_bad_calls,
                          n_turns, prompt_tokens + completion_tokens AS tokens,
                          wall_ms, peak_context_chars
                   FROM runs"""
            )
            for r in cur.fetchall():
                rows.append(
                    dict(zip([d[0] for d in cur.description], r))
                )
        for r in rows:
            r["task_type"] = task_type_of(r["task_id"])
        return rows


if __name__ == "__main__":
    import tempfile

    db = os.path.join(tempfile.gettempdir(), "tracer_smoke.db")
    if os.path.exists(db):
        os.remove(db)

    t = Tracer(db)
    rid = t.start_run("single", "A01")
    t.log_turn(rid, 1, tool_name="grep", tool_args={"pattern": "def send"},
               tool_result="sessions.py:752", usage={"prompt_tokens": 100,
               "completion_tokens": 20}, context_chars=800, elapsed_ms=1200)
    t.log_turn(rid, 1, tool_name="submit", tool_args={"answer": "sessions.py:752"},
               usage={"prompt_tokens": 300, "completion_tokens": 30},
               context_chars=1200, elapsed_ms=900)
    t.finish_run(rid, success=True, final_answer="sessions.py:752")

    for row in t.summary_by_strategy():
        print(row)
    os.remove(db)
    print("tracer 冒烟测试通过")
