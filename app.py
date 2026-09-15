"""
电商数据智能分析 Agent —— Web 版后端

把命令行版 ai_sql_agent.py 升级为浏览器交互界面，保留原有的
「自然语言提问 -> 生成 SQL -> 执行查询 -> 输出业务洞察」三步链路，
并补充了工程上必须有的几件事：

  1. 自动读取 information_schema 生成表结构提示词（不用再手写 TABLE_SCHEMA）
  2. SQL 只读护栏：只放行 SELECT / WITH，自动补 LIMIT，拦截写操作
  3. SQL 执行失败时把错误回传给模型自愈重试
  4. SSE 流式推送，前端能逐步看到 SQL / 结果表 / 业务洞察
  5. 多轮对话上下文，支持"再按类目拆一下"这类追问

启动：
    python app.py
或：
    python -m uvicorn app:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations
from dotenv import load_dotenv
load_dotenv()
import json
import os
import re
import time
from typing import Any, Iterator

import pymysql
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

# ---------------------------------------------------------------- 配置

try:
    from dotenv import load_dotenv

    _here = os.path.dirname(os.path.abspath(__file__))
    for _p in (
        os.path.join(_here, ".env"),
        os.path.join(os.path.dirname(_here), ".env"),
    ):
        if os.path.exists(_p):
            load_dotenv(_p, override=False)
except ImportError:  # dotenv 是可选依赖
    pass


DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

DB_CONFIG: dict[str, Any] = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", ""),
    "database": os.getenv("DB_NAME", "taobao_analysis"),
    "charset": "utf8mb4",
    "connect_timeout": 8,
}

MAX_ROWS_RETURN = int(os.getenv("MAX_ROWS_RETURN", "200"))  # 回传前端的最大行数
SQL_HARD_LIMIT = int(os.getenv("SQL_HARD_LIMIT", "1000"))   # 自动追加的 LIMIT
MAX_SQL_RETRY = int(os.getenv("MAX_SQL_RETRY", "2"))        # SQL 自愈重试次数
SCHEMA_TTL = 60                                             # 表结构缓存秒数

# 不暴露给模型的列（格式 table.column，逗号分隔）。
# 有些历史遗留列全为 0 / 全为空，模型一旦选中就会得出完全错误的结论，
# 所以在生成提示词时直接屏蔽掉。
HIDDEN_COLUMNS = {
    c.strip()
    for c in os.getenv("HIDDEN_COLUMNS", "user_behavior.behavior_count").split(",")
    if c.strip()
}

# ---------------------------------------------------------------- SQL 护栏

_ALLOWED_START = re.compile(r"^\s*(select|with|show|desc|describe|explain)\b", re.I)

_WRITE_PATTERN = re.compile(
    r"("
    r"\binsert\s+into\b|\bdelete\s+from\b|\bupdate\s+[\w`\.]+\s+set\b|"
    r"\bdrop\s+(table|database|index|view)\b|\btruncate\s+table\b|"
    r"\balter\s+table\b|\bcreate\s+(table|database|index|view|user)\b|"
    r"\bgrant\b|\brevoke\b|\breplace\s+into\b|\bload\s+data\b|"
    r"\binto\s+outfile\b|\binto\s+dumpfile\b|\bcall\b|\bset\s+global\b"
    r")",
    re.I,
)


class SqlGuardError(ValueError):
    """SQL 未通过只读安全检查。"""


def guard_sql(raw_sql: str) -> str:
    """把模型输出的 SQL 清洗成可安全执行的只读单条语句。"""
    sql = clean_sql(raw_sql)

    # 去掉结尾分号，便于判断是否有多条语句
    body = sql.rstrip().rstrip(";").strip()
    if ";" in body:
        raise SqlGuardError("只允许执行单条 SQL 语句")

    if not _ALLOWED_START.match(body):
        raise SqlGuardError("只允许 SELECT / WITH 等只读查询，已拦截")

    hit = _WRITE_PATTERN.search(body)
    if hit:
        raise SqlGuardError(f"检测到写操作关键字「{hit.group(0)}」，已拦截")

    # 聚合类查询自动兜底 LIMIT，防止把整张表拉回来
    if re.match(r"^\s*(select|with)\b", body, re.I) and not re.search(r"\blimit\b", body, re.I):
        body = f"{body}\nLIMIT {SQL_HARD_LIMIT}"

    return body


def clean_sql(text: str) -> str:
    """去掉模型可能输出的 markdown 代码块标记。"""
    t = (text or "").strip()
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    return t.strip()


# ---------------------------------------------------------------- 数据库

_schema_cache: dict[str, Any] = {"text": "", "at": 0.0}


def db_connect():
    return pymysql.connect(**DB_CONFIG)


def execute_sql(sql: str) -> tuple[list[str], list[tuple]]:
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            columns = [d[0] for d in (cur.description or [])]
            rows = cur.fetchall()
        return columns, rows
    finally:
        conn.close()


def build_schema_text(force: bool = False) -> str:
    """从 information_schema 读取表结构，拼成给模型看的提示词。"""
    now = time.time()
    if not force and _schema_cache["text"] and now - _schema_cache["at"] < SCHEMA_TTL:
        return _schema_cache["text"]

    conn = db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, COLUMN_COMMENT
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = %s
                ORDER BY TABLE_NAME, ORDINAL_POSITION
                """,
                (DB_CONFIG["database"],),
            )
            col_rows = cur.fetchall()

            cur.execute(
                """
                SELECT TABLE_NAME, TABLE_COMMENT
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = %s
                """,
                (DB_CONFIG["database"],),
            )
            table_comments = {r[0]: (r[1] or "") for r in cur.fetchall()}

        if not col_rows:
            raise RuntimeError(f"数据库 {DB_CONFIG['database']} 里没有表")

        grouped: dict[str, list[tuple[str, str, str]]] = {}
        for table, column, ctype, comment in col_rows:
            if f"{table}.{column}" in HIDDEN_COLUMNS:
                continue
            grouped.setdefault(table, []).append((column, ctype, comment or ""))

        lines: list[str] = []
        for table, cols in grouped.items():
            comment = table_comments.get(table, "")
            lines.append(f"表名: {table}" + (f"（{comment}）" if comment else ""))
            # 带上行数，方便模型判断聚合代价
            try:
                with conn.cursor() as cur2:
                    cur2.execute(f"SELECT COUNT(*) FROM `{table}`")
                    total = cur2.fetchone()[0]
                lines.append(f"  数据量: {total:,} 行")
            except Exception:
                pass
            lines.append("  字段:")
            for column, ctype, comment in cols:
                suffix = f" —— {comment}" if comment else ""
                lines.append(f"  - {column} ({ctype}){suffix}")
            lines.append("")

        text = "\n".join(lines).strip()
        _schema_cache.update({"text": text, "at": now})
        return text
    finally:
        conn.close()


# ---------------------------------------------------------------- 提示词

SQL_SYSTEM = """你是资深 MySQL 数据分析专家。你的唯一任务是把用户的自然语言问题，转换成一条可直接执行的 MySQL 查询语句。

硬性要求：
1. 只输出 SQL 本身。不要任何解释、不要 markdown 代码块、不要分号结尾。
2. 只能使用 SELECT / WITH 等只读查询，绝对禁止任何写操作。
3. 只能使用下面提供的表和字段，不要臆造表名或字段名。
4. 聚合结果列请起有意义的中文别名，方便业务方直接阅读。
5. 结果按业务重要性排序，并加上合理的 LIMIT。
6. 涉及时间筛选时使用 date_only（日期）或 date（日期时间）字段。
7. 如果用户的问题信息不足，选择最合理的一种口径直接给出 SQL，不要反问。"""

FEW_SHOT = """【示例 1】
问题：帮我找出购买次数前十的用户
SQL：SELECT user_id, COUNT(*) AS 购买次数 FROM user_behavior WHERE behavior_type = 'buy' GROUP BY user_id ORDER BY 购买次数 DESC LIMIT 10

【示例 2】
问题：各行为类型的数量分布
SQL：SELECT behavior_type AS 行为类型, COUNT(*) AS 数量 FROM user_behavior GROUP BY behavior_type ORDER BY 数量 DESC

【示例 3】
问题：每天的活跃用户数趋势
SQL：SELECT date_only AS 日期, COUNT(DISTINCT user_id) AS 活跃用户数 FROM user_behavior GROUP BY date_only ORDER BY 日期"""

ANALYSIS_SYSTEM = """你是电商数据分析专家。根据用户的业务问题、实际执行的 SQL 和查询结果，输出业务洞察与可执行建议。

要求：
1. 用中文，固定结构：
   **分析：** 一段话讲清数据说明什么
   **运营建议：**
   1. 第一条
   2. 第二条
2. 先给结论再给依据，所有结论必须来自给定数据，禁止编造数字。
3. 建议要具体、可落地、能落到某个动作上；避免"加强运营""提升体验"这类空话。
4. 查询结果可能是「前 15 行 + 后 15 行 + 数值列统计」的摘要形式。此时**不要用局部数据推断整体趋势**——判断量级和趋势必须参考数值列统计里的最小值 / 最大值 / 总和 / 平均值，否则容易把大结果集误读成小数字。
5. 全文控制在 300 字以内。"""


def _llm():
    if not DEEPSEEK_API_KEY:
        raise RuntimeError("未配置 DEEPSEEK_API_KEY，请在 web/.env 里填写")
    from openai import OpenAI

    return OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)


def generate_sql(question: str, schema: str, history: list[dict], last_error: str = "") -> str:
    """让模型生成 SQL；last_error 非空时表示这是自愈重试。"""
    parts = [f"数据库表结构：\n{schema}", FEW_SHOT]
    if history:
        lines = ["【本次对话上文】"]
        for h in history[-3:]:
            lines.append(f"用户：{h.get('question', '')}")
            if h.get("sql"):
                lines.append(f"已执行的 SQL：{h['sql']}")
        parts.append("\n".join(lines))
    parts.append(f"【用户当前问题】\n{question}")
    if last_error:
        parts.append(
            "【重要】上一次生成的 SQL 执行报错如下，请修正后重新输出完整 SQL：\n"
            f"{last_error}"
        )

    resp = _llm().chat.completions.create(
        model=DEEPSEEK_MODEL,
        messages=[
            {"role": "system", "content": SQL_SYSTEM},
            {"role": "user", "content": "\n\n".join(parts)},
        ],
        temperature=0.1,
    )
    return resp.choices[0].message.content or ""


def summarize_for_llm(columns: list[str], rows: list[tuple], total: int) -> str:
    """把查询结果压缩成给模型看的摘要。

    只截前 10 行是个陷阱：趋势类查询（比如按天看活跃用户）前 10 行
    往往只是冷启动期的小数字，模型据此会得出完全相反的结论。
    所以小结果集全给，大结果集给「头 + 尾 + 数值列统计」。
    """

    def rec(row: tuple) -> dict:
        return dict(zip(columns, row))

    if total <= 30:
        return f"完整结果（{total} 行）：\n" + json.dumps(
            [rec(r) for r in rows], ensure_ascii=False, default=str
        )

    parts = [
        f"结果共 {total} 行，下面给出前 15 行、后 15 行，以及数值列的统计：",
        "前 15 行：" + json.dumps([rec(r) for r in rows[:15]], ensure_ascii=False, default=str),
        "后 15 行：" + json.dumps([rec(r) for r in rows[-15:]], ensure_ascii=False, default=str),
    ]

    stats = []
    for i, col in enumerate(columns):
        vals: list[float] = []
        for r in rows:
            v = r[i]
            if v is None or isinstance(v, bool):
                continue
            try:
                vals.append(float(v))
            except (TypeError, ValueError):
                pass
        # 只有「大多数行都能转成数字」的列才当作数值列
        if len(vals) >= max(2, int(len(rows) * 0.8)):
            stats.append(
                {
                    "列": col,
                    "最小值": min(vals),
                    "最大值": max(vals),
                    "总和": round(sum(vals), 2),
                    "平均值": round(sum(vals) / len(vals), 2),
                }
            )
    if stats:
        parts.append("数值列统计：" + json.dumps(stats, ensure_ascii=False))
    return "\n".join(parts)


def generate_insight(question: str, sql: str, columns: list[str], rows: list[tuple], total: int) -> str:
    summary = summarize_for_llm(columns, rows, total)
    resp = _llm().chat.completions.create(
        model=DEEPSEEK_MODEL,
        messages=[
            {"role": "system", "content": ANALYSIS_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"用户问题：{question}\n\n"
                    f"执行的 SQL：\n{sql}\n\n"
                    f"查询结果：\n{summary}"
                ),
            },
        ],
        temperature=0.7,
    )
    return resp.choices[0].message.content or ""


# ---------------------------------------------------------------- FastAPI

app = FastAPI(title="电商数据智能分析 Agent")

# 允许本机来源跨域访问。
# 场景：index.html 被别的静态服务器打开时（比如 IDE 的静态预览、file://），
# 页面自身没有后端，需要回连到本机的 8000 端口。只放行 localhost 来源。
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$",
    allow_methods=["*"],
    allow_headers=["*"],
)


class AskRequest(BaseModel):
    question: str
    history: list[dict] = []


def sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"


@app.get("/")
def index():
    return FileResponse(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "index.html"))


@app.get("/api/health")
def health():
    """探活：数据库 + 模型 Key 是否就绪。"""
    out: dict[str, Any] = {
        "api_key": bool(DEEPSEEK_API_KEY),
        "database": False,
        "database_name": DB_CONFIG["database"],
        "tables": [],
        "table_count": 0,
        "rows": 0,
        "error": "",
    }
    try:
        conn = db_connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SHOW TABLES")
                tables = [r[0] for r in cur.fetchall()]
                out["tables"] = tables
                out["table_count"] = len(tables)
                total = 0
                for t in tables:
                    try:
                        cur.execute(f"SELECT COUNT(*) FROM `{t}`")
                        total += cur.fetchone()[0]
                    except Exception:  # noqa: BLE001
                        pass
                out["rows"] = total
        finally:
            conn.close()
        out["database"] = True
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    return JSONResponse(out)


@app.get("/api/schema")
def schema():
    """把模型看到的表结构原样回显，方便确认口径。"""
    try:
        return JSONResponse({"ok": True, "schema": build_schema_text(force=True)})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"})


def pipeline(question: str, history: list[dict]) -> Iterator[str]:
    """三步链路，逐步通过 SSE 推给前端。"""
    t0 = time.time()

    # ---- 1. 表结构 ----
    yield sse("stage", {"stage": "schema", "text": "读取数据库结构…"})
    try:
        schema_text = build_schema_text()
    except Exception as exc:  # noqa: BLE001
        yield sse("error", {"text": f"读取数据库结构失败：{type(exc).__name__}: {exc}"})
        yield sse("done", {"elapsed": round(time.time() - t0, 2)})
        return

    # ---- 2. 生成 SQL（失败自愈） ----
    yield sse("stage", {"stage": "sql", "text": "正在生成 SQL…"})
    sql = ""
    columns: list[str] = []
    rows: list[tuple] = []
    last_error = ""

    for attempt in range(MAX_SQL_RETRY + 1):
        try:
            raw = generate_sql(question, schema_text, history, last_error)
        except Exception as exc:  # noqa: BLE001
            yield sse("error", {"text": f"调用模型失败：{type(exc).__name__}: {exc}"})
            yield sse("done", {"elapsed": round(time.time() - t0, 2)})
            return

        sql = clean_sql(raw)

        try:
            safe_sql = guard_sql(sql)
        except SqlGuardError as exc:
            # 先把被拦截的 SQL 展示出来，用户才知道模型想干什么
            yield sse("sql", {"sql": sql, "attempt": attempt + 1, "blocked": True})
            last_error = f"安全检查未通过：{exc}"
            if attempt < MAX_SQL_RETRY:
                yield sse("stage", {"stage": "sql", "text": f"SQL 未通过安全检查，正在重写（第 {attempt + 1} 次）…"})
                continue
            yield sse("error", {"text": last_error})
            yield sse("done", {"elapsed": round(time.time() - t0, 2)})
            return

        adjusted = safe_sql != sql
        sql = safe_sql
        yield sse("sql", {"sql": sql, "attempt": attempt + 1, "adjusted": adjusted})

        # ---- 3. 执行 ----
        yield sse("stage", {"stage": "query", "text": "执行查询…"})
        try:
            columns, rows = execute_sql(sql)
            break
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < MAX_SQL_RETRY:
                yield sse("stage", {"stage": "sql", "text": f"SQL 执行失败，正在自愈重试（第 {attempt + 1} 次）…"})
                continue
            yield sse("error", {"text": f"SQL 执行失败：{last_error}"})
            yield sse("done", {"elapsed": round(time.time() - t0, 2)})
            return
    else:
        yield sse("error", {"text": "多次重试后仍未得到可执行的 SQL"})
        yield sse("done", {"elapsed": round(time.time() - t0, 2)})
        return

    total = len(rows)
    yield sse(
        "result",
        {
            "columns": columns,
            "rows": [list(r) for r in rows[:MAX_ROWS_RETURN]],
            "total": total,
            "truncated": total > MAX_ROWS_RETURN,
        },
    )

    # ---- 4. 业务洞察 ----
    if total == 0:
        yield sse("insight", {"text": "**分析：** 该条件下没有查询到任何数据，可能是筛选口径过窄或该时间段无记录。\n\n**运营建议：**\n1. 放宽时间范围或行为类型条件后重试。\n2. 确认字段取值口径，例如 behavior_type 只取 pv / fav / cart / buy。"})
    else:
        yield sse("stage", {"stage": "insight", "text": "生成业务洞察…"})
        try:
            yield sse("insight", {"text": generate_insight(question, sql, columns, rows, total)})
        except Exception as exc:  # noqa: BLE001
            yield sse("error", {"text": f"生成业务洞察失败：{type(exc).__name__}: {exc}"})

    yield sse("done", {"elapsed": round(time.time() - t0, 2)})


@app.post("/api/ask")
def ask(req: AskRequest):
    q = (req.question or "").strip()
    if not q:
        return JSONResponse({"error": "问题不能为空"}, status_code=400)
    return StreamingResponse(
        pipeline(q, req.history or []),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8080"))
    print(f"\n  电商数据智能分析 Agent 已启动 ->  http://127.0.0.1:{port}\n")
    uvicorn.run(app, host="127.0.0.1", port=port)
