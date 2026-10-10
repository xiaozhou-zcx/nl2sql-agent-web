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

import json
import logging
import os
import re
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from typing import Any, Iterator

import pymysql
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

# ---------------------------------------------------------------- 日志
# 原始报错（表名 / 库名 / 连接串）只进日志文件，前端只拿友好提示。
# 详见 README「安全」一节。

_here = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(_here, "logs")
LOG_FILE = os.path.join(LOG_DIR, "app.log")


def _setup_logging() -> logging.Logger:
    logger = logging.getLogger("nl2sql")
    if logger.handlers:  # 避免 uvicorn --reload 多次导入时重复加 handler
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s")
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        fh = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024,
                                 backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError:  # 日志目录不可写时不能让服务起不来
        pass
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


log = _setup_logging()

# ---------------------------------------------------------------- 配置

# 只在这里加载一次 .env：先找项目根目录，再找上一级目录（项目曾被放在 web/ 子目录里），
# 第一个命中的生效；dotenv 是可依赖项，缺失时静默降级为读系统环境变量。
try:
    from dotenv import load_dotenv

    for _env_path in (
        os.path.join(_here, ".env"),
        os.path.join(os.path.dirname(_here), ".env"),
    ):
        if os.path.exists(_env_path):
            load_dotenv(_env_path, override=False)
            break
except ImportError:
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
SQL_HARD_LIMIT = int(os.getenv("SQL_HARD_LIMIT", "1000"))   # 单次查询强制截断的行数上限
MAX_SQL_RETRY = int(os.getenv("MAX_SQL_RETRY", "2"))        # SQL 自愈重试次数
SCHEMA_TTL = 60                                             # 表结构缓存秒数
VALUE_SAMPLE_MAX_DISTINCT = 20                              # 低于这个基数的字符串列，取值写进提示词
FEEDBACK_FILE = os.path.join(_here, "feedback.jsonl")       # 👍/👎 反馈落盘位置

# 运营台参数
RECOMMEND_LIMIT = 5          # /api/recommend 返回的候选商品个数（加购未购买优先，不足用热销补齐）

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
    r"\binto\s+outfile\b|\binto\s+dumpfile\b|\bcall\b|\bset\s+global\b|"
    # 这几个不是写操作，但能读写服务器文件 / 拖垮数据库，一并拦掉
    r"\bload_file\s*\(|\bsleep\s*\(|\bbenchmark\s*\(|\buser\s*\(\s*\)|"
    r"\binformation_schema\.user_privileges\b"
    r")",
    re.I,
)


class SqlGuardError(ValueError):
    """SQL 未通过只读安全检查。"""


# 匹配 LIMIT 后面第一个数字：LIMIT 10 / LIMIT 999999 / LIMIT 0, 20 / LIMIT 20 OFFSET 5
_LIMIT_NUM = re.compile(r"\blimit\s+(\d+)", re.I)
# LIMIT 后面带逗号的写法（MySQL 的 LIMIT offset, count）
_LIMIT_COMMA = re.compile(r"(\blimit\s+\d+)(\s*,\s*)(\d+)", re.I)
_LIMIT_OFFSET = re.compile(r"(\blimit\s+\d+)(\s+offset\s+)(\d+)", re.I)


def _has_top_level_limit(sql: str) -> bool:
    """判断整条 SQL 的"最外层"有没有 LIMIT（括号里的 LIMIT 属于子查询，不算）。"""
    depth = 0
    for m in re.finditer(r"[()]|\blimit\b", sql, re.I):
        tok = m.group(0)
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            return True
    return False


def _clamp_free_limit(m: re.Match) -> str:
    """自由写的 LIMIT n / LIMIT n OFFSET m，把 n 压到上限内。"""
    n = int(m.group(1))
    return f"LIMIT {min(n, SQL_HARD_LIMIT)}"


def _clamp_offset_limit(m: re.Match) -> str:
    """LIMIT offset, count 写法：offset 不动（用户要看的是那一段），count 压到上限内。"""
    return f"{m.group(1)}{m.group(2)}{min(int(m.group(3)), SQL_HARD_LIMIT)}"


def _ensure_readonly_limit(body: str) -> str:
    """给只读查询加上"绝不超过 SQL_HARD_LIMIT 行"的硬约束。

    只判「有没有 LIMIT」是不够的：模型写 LIMIT 999999 就等于没有上限，
    照样能把整张表拉回来。所以这里做两件事：
      1. 括号外的 LIMIT 全部压到 SQL_HARD_LIMIT 以内（多个 LIMIT 取最严的）
      2. 一个 LIMIT 都没有时，补一个 LIMIT SQL_HARD_LIMIT
    括号内的 LIMIT 属于子查询语义，不动它。
    """
    # 顺序很关键：先处理 LIMIT offset, count 这种带逗号的写法，
    # 否则会被下面按第一个数字处理的逻辑解读成别的东西。
    body = _LIMIT_COMMA.sub(_clamp_offset_limit, body)
    body = _LIMIT_OFFSET.sub(_clamp_offset_limit, body)
    body = _LIMIT_NUM.sub(_clamp_free_limit, body)
    if not _has_top_level_limit(body):
        body = f"{body}\nLIMIT {SQL_HARD_LIMIT}"
    return body


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

    # 只读查询强制行数上限（含"模型自己写了超大 LIMIT"的情况）
    if re.match(r"^\s*(select|with|show|desc|describe|explain)\b", body, re.I):
        body = _ensure_readonly_limit(body)

    return body


def clean_sql(text: str) -> str:
    """去掉模型可能输出的 markdown 代码块标记。"""
    t = (text or "").strip()
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    return t.strip()


# ---------------------------------------------------------------- 错误脱敏


def friendly_error(exc: Exception, doing: str) -> str:
    """把原始异常写进日志，只把可公开的提示返回给前端。"""
    log.error("%s 失败 | %s: %s", doing, type(exc).__name__, exc, exc_info=True)
    if isinstance(exc, pymysql.err.OperationalError):
        return f"{doing}失败：数据库连接异常，请确认 MySQL 已启动、.env 里的账号密码正确（详情见 logs/app.log）"
    return f"{doing}失败：系统内部错误，已记录日志（详情见 logs/app.log）"


def error_for_model(exc: Exception) -> str:
    """回传给模型用于自愈重试的错误：只给 MySQL 错误码与消息。

    模型要改 SQL 只需要知道"哪里错了"，完整异常（含表名、库名、连接信息）
    留在日志里，不进提示词、也不进前端。
    """
    if isinstance(exc, pymysql.MySQLError):
        code = exc.args[0] if exc.args else ""
        msg = str(exc.args[1]) if len(exc.args) > 1 else str(exc)
        return f"MySQL Error {code}: {msg}"
    return f"{type(exc).__name__}"


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


_SAMPLE_TYPES = ("char", "varchar", "enum", "set")


def _quote_ident(name: str) -> str:
    """反引号包裹标识符；名字里的反引号翻倍转义。"""
    return "`" + str(name).replace("`", "``") + "`"


def sample_low_cardinality_values(cur, table: str, column: str, ctype: str,
                                  limit: int = VALUE_SAMPLE_MAX_DISTINCT) -> list[str]:
    """对低基数列采样取值，写进提示词。

    为什么值得做：模型不知道 behavior_type 里到底是 pv/buy 还是 view/purchase，
    只能猜；把真实取值摆给它，比在提示词里反复强调"不要臆造"有效得多。
    只处理字符串列、且 distinct 数足够小（默认 ≤ 20），避免把高基数列拉回来。
    """
    base = ctype.split("(")[0].strip().lower()
    if base not in _SAMPLE_TYPES or column.lower().endswith("_id"):
        return []
    tbl, col = _quote_ident(table), _quote_ident(column)
    try:
        cur.execute(
            f"SELECT COUNT(DISTINCT {col}) FROM {tbl} WHERE {col} IS NOT NULL AND {col} <> ''"
        )
        distinct = cur.fetchone()[0]
        if not distinct or distinct > limit:
            return []
        cur.execute(
            f"SELECT DISTINCT {col} FROM {tbl} WHERE {col} IS NOT NULL AND {col} <> '' "
            f"ORDER BY {col} LIMIT 6"
        )
        return [str(r[0]) for r in cur.fetchall()]
    except Exception as exc:  # noqa: BLE001
        log.warning("列值采样失败（跳过）| %s.%s | %s", table, column, exc)
        return []


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
                    cur2.execute(f"SELECT COUNT(*) FROM {_quote_ident(table)}")
                    total = cur2.fetchone()[0]
                lines.append(f"  数据量: {total:,} 行")
            except Exception:
                pass
            lines.append("  字段:")
            with conn.cursor() as cur3:
                for column, ctype, comment in cols:
                    suffix = f" —— {comment}" if comment else ""
                    values = sample_low_cardinality_values(cur3, table, column, ctype)
                    if values:
                        shown = " / ".join(values)
                        if len(values) >= 6:
                            shown += " …"
                        suffix += f"（取值样例: {shown}）"
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
7. 如果用户的问题信息不足，选择最合理的一种口径直接给出 SQL，不要反问。
8. 【拒答】只有当问题需要的字段在当前表结构里**完全不存在**时（比如手机号、身份证、收货地址、支付金额），
   才输出一行：__无法回答__: 缺少字段 xxx，表结构里没有这类信息
   除此之外一律给出 SQL。宁可换一个最接近的口径，也不要拒答；拒答是最后手段，不是省事手段。"""

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
        raise RuntimeError("未配置 DEEPSEEK_API_KEY，请在项目根目录的 .env 里填写")
    from openai import OpenAI

    return OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)


# ---------------------------------------------------------------- token 统计
# 一次提问会调用模型 1~3 次（生成 SQL 可能重试 + 生成洞察），
# 所以用一个可变累加器把每次调用的 usage 汇总起来，交给日志记录。

_USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")


def new_usage() -> dict[str, int]:
    return {"calls": 0, **{k: 0 for k in _USAGE_KEYS}}


def track_usage(acc: dict[str, int] | None, resp: Any) -> None:
    """把一次 API 响应的 usage 累加进 acc（拿不到 usage 时静默跳过）。"""
    if acc is None:
        return
    usage = getattr(resp, "usage", None)
    if usage is None:
        return
    acc["calls"] = acc.get("calls", 0) + 1
    for k in _USAGE_KEYS:
        acc[k] = acc.get(k, 0) + int(getattr(usage, k, 0) or 0)


def generate_sql(question: str, schema: str, history: list[dict],
                 last_error: str = "", usage: dict[str, int] | None = None) -> str:
    """让模型生成 SQL；last_error 非空时表示这是自愈重试。"""
    parts = [f"数据库表结构：\n{schema}", FEW_SHOT]
    if history:
        lines = ["【本次对话上文】"]
        for h in history[-3:]:
            lines.append(f"用户：{h.get('question', '')}")
            if h.get("sql"):
                lines.append(f"已执行的 SQL：{h['sql']}")
            # 把上一轮的结果也带上：不然模型不知道"再按类目拆一下"是在拆什么
            summary = h.get("result_summary")
            if summary:
                lines.append(f"该 SQL 的结果：{summary}")
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
    track_usage(usage, resp)
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


def result_digest(columns: list[str], rows: list[tuple], total: int,
                  head: int = 3) -> str:
    """给下一轮对话用的极简结果摘要（列名 + 行数 + 前几行）。

    和 summarize_for_llm 的区别：那个是给"写洞察"用的，这个是塞进历史上下文里的，
    必须短，否则多轮对话的提示词会迅速膨胀。
    """
    cols = " / ".join(columns[:8]) + (" …" if len(columns) > 8 else "")
    if total == 0:
        return f"{cols}（0 行）"
    sample = json.dumps(
        [dict(zip(columns, r)) for r in rows[:head]], ensure_ascii=False, default=str
    )
    return f"{cols}（{total} 行）前 {min(head, total)} 行：{sample}"


def generate_insight(question: str, sql: str, columns: list[str], rows: list[tuple],
                     total: int, usage: dict[str, int] | None = None) -> str:
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
    track_usage(usage, resp)
    return resp.choices[0].message.content or ""


# ---------------------------------------------------------------- 拒答

_REFUSE_RE = re.compile(r"^\s*(?:\*\*)?\s*__无法回答__\s*[:：]?\s*", re.I)
_REFUSE_HINTS = ("手机号", "手机号码", "电话号码", "身份证", "收货地址", "家庭住址",
                 "支付金额", "银行卡", "真实姓名", "密码", "邮箱")


def parse_refusal(raw_sql: str) -> str:
    """识别模型输出的拒答标记，返回拒答原因；不是拒答则返回空串。

    宁可不执行 SQL 也不要硬编一条：比如问"用户手机号是多少"，
    表里根本没有这个字段，硬编出来的 SQL 只会给出一个看起来像答案的假数字。
    """
    text = (raw_sql or "").strip()
    m = _REFUSE_RE.match(text)
    if m:
        reason = text[m.end():].strip().strip("*").strip()
        return reason or "当前表结构里没有回答该问题所需的字段"

    # 兜底：模型没按格式输出，但输出里明显没有 SQL、又在说无法回答
    if not re.search(r"\bselect\b|\bwith\b", text, re.I) and (
        "无法回答" in text or any(h in text for h in _REFUSE_HINTS)
    ):
        return "当前表结构里没有回答该问题所需的字段"
    return ""


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
        out["error"] = friendly_error(exc, "读取数据库信息")
    return JSONResponse(out)


@app.get("/api/schema")
def schema():
    """把模型看到的表结构原样回显，方便确认口径。"""
    try:
        return JSONResponse({"ok": True, "schema": build_schema_text(force=True)})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": friendly_error(exc, "读取表结构")})


def pipeline(question: str, history: list[dict]) -> Iterator[str]:
    """三步链路，逐步通过 SSE 推给前端。"""
    t0 = time.time()
    usage = new_usage()
    attempts = 0
    outcome = "ok"

    # ---- 1. 表结构 ----
    yield sse("stage", {"stage": "schema", "text": "读取数据库结构…"})
    try:
        schema_text = build_schema_text()
    except Exception as exc:  # noqa: BLE001
        outcome = "schema_error"
        yield sse("error", {"text": friendly_error(exc, "读取数据库结构")})
        yield sse("done", {"elapsed": round(time.time() - t0, 2)})
        log.info("ask | outcome=%s | question=%r | elapsed=%.2fs",
                 outcome, question, time.time() - t0)
        return

    # ---- 2. 生成 SQL（失败自愈） ----
    yield sse("stage", {"stage": "sql", "text": "正在生成 SQL…"})
    sql = ""
    columns: list[str] = []
    rows: list[tuple] = []
    last_error = ""

    for attempt in range(MAX_SQL_RETRY + 1):
        attempts = attempt + 1
        try:
            raw = generate_sql(question, schema_text, history, last_error, usage)
        except Exception as exc:  # noqa: BLE001
            outcome = "llm_error"
            yield sse("error", {"text": friendly_error(exc, "调用大模型")})
            yield sse("done", {"elapsed": round(time.time() - t0, 2)})
            log.info("ask | outcome=%s | question=%r | elapsed=%.2fs | usage=%s",
                     outcome, question, time.time() - t0, usage)
            return

        # 超纲问题：模型主动拒答，不硬编 SQL
        refuse_reason = parse_refusal(raw)
        if refuse_reason:
            outcome = "refused"
            yield sse("refusal", {"reason": refuse_reason})
            yield sse("done", {"elapsed": round(time.time() - t0, 2)})
            log.info("ask | outcome=%s | question=%r | reason=%s | elapsed=%.2fs | usage=%s",
                     outcome, question, refuse_reason, time.time() - t0, usage)
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
            outcome = "blocked"
            yield sse("error", {"text": last_error, "blocked": True})
            yield sse("done", {"elapsed": round(time.time() - t0, 2)})
            log.info("ask | outcome=%s | question=%r | sql=%s | elapsed=%.2fs | usage=%s",
                     outcome, question, sql, time.time() - t0, usage)
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
            # 完整异常进日志；给模型的只是 MySQL 错误码 + 消息，够它改 SQL 就行
            last_error = error_for_model(exc)
            log.error("SQL 执行失败（第 %s 次尝试）| %s: %s | SQL: %s",
                      attempt + 1, type(exc).__name__, exc, sql, exc_info=True)
            if attempt < MAX_SQL_RETRY:
                yield sse("stage", {"stage": "sql", "text": f"SQL 执行失败，正在自愈重试（第 {attempt + 1} 次）…"})
                continue
            outcome = "sql_error"
            yield sse("error", {"text": friendly_error(exc, "执行查询")})
            yield sse("done", {"elapsed": round(time.time() - t0, 2)})
            log.info("ask | outcome=%s | question=%r | sql=%s | attempts=%s | elapsed=%.2fs | usage=%s",
                     outcome, question, sql, attempts, time.time() - t0, usage)
            return
    else:
        outcome = "exhausted"
        yield sse("error", {"text": "多次重试后仍未得到可执行的 SQL"})
        yield sse("done", {"elapsed": round(time.time() - t0, 2)})
        log.info("ask | outcome=%s | question=%r | elapsed=%.2fs | usage=%s",
                 outcome, question, time.time() - t0, usage)
        return

    total = len(rows)
    yield sse(
        "result",
        {
            "columns": columns,
            "rows": [list(r) for r in rows[:MAX_ROWS_RETURN]],
            "total": total,
            "truncated": total > MAX_ROWS_RETURN,
            # 给前端存多轮上下文用：下一轮只需要"列名 + 行数 + 前几行"
            "digest": result_digest(columns, rows, total),
        },
    )

    # ---- 4. 业务洞察 ----
    if total == 0:
        yield sse("insight", {"text": "**分析：** 该条件下没有查询到任何数据，可能是筛选口径过窄或该时间段无记录。\n\n**运营建议：**\n1. 放宽时间范围或行为类型条件后重试。\n2. 确认字段取值口径，例如 behavior_type 只取 pv / fav / cart / buy。"})
    else:
        yield sse("stage", {"stage": "insight", "text": "生成业务洞察…"})
        try:
            yield sse("insight", {"text": generate_insight(question, sql, columns, rows, total, usage)})
        except Exception as exc:  # noqa: BLE001
            yield sse("error", {"text": friendly_error(exc, "生成业务洞察")})

    yield sse("done", {"elapsed": round(time.time() - t0, 2)})

    # 一次提问的完整日志：问题 / SQL / 重试次数 / 耗时 / 行数 / token
    log.info(
        "ask | outcome=ok | question=%r | sql=%s | attempts=%s | rows=%s | elapsed=%.2fs | "
        "tokens(prompt=%s, completion=%s, total=%s, calls=%s)",
        question, sql, attempts, total, time.time() - t0,
        usage["prompt_tokens"], usage["completion_tokens"], usage["total_tokens"], usage["calls"],
    )


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


class FeedbackRequest(BaseModel):
    question: str = ""
    sql: str = ""
    rating: str = ""          # up / down
    comment: str = ""


@app.post("/api/feedback")
def feedback(req: FeedbackRequest):
    """👍/👎 反馈落盘（JSONL，一行一条）。

    这是最便宜的效果度量：不用等评估集，真实用户点一次就多一条标注，
    之后可以拿去做 bad case 分析、补进 eval/cases.json。
    """
    rating = (req.rating or "").strip().lower()
    if rating not in {"up", "down"}:
        return JSONResponse({"ok": False, "error": "rating 只能是 up 或 down"}, status_code=400)
    record = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "rating": rating,
        "question": (req.question or "")[:500],
        "sql": (req.sql or "")[:2000],
        "comment": (req.comment or "")[:500],
    }
    try:
        with open(FEEDBACK_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        log.error("写反馈文件失败 | %s: %s", type(exc).__name__, exc)
        return JSONResponse({"ok": False, "error": "反馈写入失败，已记录日志"}, status_code=500)
    log.info("feedback | rating=%s | question=%r", rating, record["question"])
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------- 高价值用户运营台
# 与问答链路共用同一套连接、日志、错误脱敏；这里只是把"取数"固定成几条确定的查询，
# 让分析结果能直接落到人身上（下发服务），而不是停在"看完就走"。

USER_TAGS_FILE = os.path.join(_here, "user_tags.jsonl")   # 运营动作落盘位置

# 高价值分层规则（写死在代码里，改口径就改这里）
#   buy_count >= 20 → 核心
#   buy_count >= 5  → 成长
#   其余            → 普通
TIER_CORE_MIN = 20
TIER_GROW_MIN = 5

# 推荐理由提示词：把候选商品 + 用户行为摘要交给模型，只让它写一句话
RECOMMEND_SYSTEM = """你是电商用户运营专家。根据用户的行为画像和推荐候选商品，写一句中文推荐理由。

要求：
1. 只输出一句话，不超过 60 字，不要分点、不要标题、不要 markdown。
2. 必须点出推荐依据（例如"加购未购买""偏好某类目"），不许编造没给出的数字。
3. 语气像运营同事在解释"为什么给这个人推这些"，不要写成广告文案。"""


def append_jsonl(path: str, record: dict) -> None:
    """追加写一行 JSON（feedback / user_tags 共用；由调用方处理异常）。"""
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def rules_reason(user_id: int, items: list[dict], behavior: dict) -> str:
    """规则兜底推荐理由：不依赖模型也能说清"为什么推给这个人"。

    触发场景：没配 DEEPSEEK_API_KEY、模型超时、返回空内容。
    接口宁可给一句朴素但正确的话，也不能 500。
    """
    if not items:
        return "该用户还没有可推荐的候选商品"
    未购买 = [it for it in items if it.get("from_cart")]
    if 未购买:
        主推 = "、".join(it["item_name"] for it in 未购买[:2])
        依据 = (f"该用户把 {主推} 加进购物车或收藏了但还没下单，"
                f"且累计购买 {behavior['buy_count']} 次、"
                f"最常浏览「{behavior['top_category']}」类目")
    else:
        主推 = "、".join(it["item_name"] for it in items[:2])
        依据 = (f"该用户最常浏览「{behavior['top_category']}」类目，"
                f"累计购买 {behavior['buy_count']} 次")
    return f"{依据}，可优先推送 {主推}，承接其既有偏好并提高转化概率。"


def count_jsonl(path: str) -> int:
    """统计 JSONL 行数；文件不存在算 0，读失败只记日志不抛。"""
    try:
        with open(path, encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    except FileNotFoundError:
        return 0
    except OSError as exc:
        log.warning("读取 %s 失败 | %s: %s", os.path.basename(path), type(exc).__name__, exc)
        return 0


def tier_of(buy_count: int) -> str:
    """按购买次数分层。"""
    if buy_count >= TIER_CORE_MIN:
        return "核心"
    if buy_count >= TIER_GROW_MIN:
        return "成长"
    return "普通"


def fetch_items(cur, item_ids: list[int]) -> dict[int, dict]:
    """批量取商品信息（一次 SQL，避免在循环里逐个查）。"""
    if not item_ids:
        return {}
    placeholders = ",".join(["%s"] * len(item_ids))
    cur.execute(
        f"""
        SELECT p.item_id, p.item_name, p.price, COALESCE(c.name, '未分类') AS category_name
        FROM products p
        LEFT JOIN categories c ON c.category_id = p.category_id
        WHERE p.item_id IN ({placeholders})
        """,
        item_ids,
    )
    return {
        r[0]: {"item_id": r[0], "item_name": r[1], "category_name": r[3], "price": float(r[2] or 0)}
        for r in cur.fetchall()
    }


def build_recommend_reason(user_id: int, items: list[dict], behavior: dict) -> str:
    """让模型写一句推荐理由；失败时用规则兜底，保证接口永远有话说。"""
    if not DEEPSEEK_API_KEY:
        # 明确没配 Key 时直接走规则，省掉一次必然失败的调用
        return rules_reason(user_id, items, behavior)

    brief = (
        f"用户 {user_id} 的行为概况：浏览 {behavior['view_count']} 次、"
        f"收藏 {behavior['fav_count']} 次、加购 {behavior['cart_count']} 次、"
        f"购买 {behavior['buy_count']} 次；最常浏览的类目是「{behavior['top_category']}」。"
    )
    candidates = "\n".join(
        f"- {it['item_name']}（{it['category_name']}，{it['price']:.2f} 元）"
        f"{'【该用户加购/收藏过但未购买】' if it['from_cart'] else '【同偏好类目热销】'}"
        for it in items
    )
    try:
        resp = _llm().chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": RECOMMEND_SYSTEM},
                {"role": "user", "content": f"{brief}\n\n推荐候选商品：\n{candidates}\n\n请写一句推荐理由。"},
            ],
            temperature=0.4,
        )
        reason = (resp.choices[0].message.content or "").strip().replace("\n", " ")
        if reason:
            return reason
    except Exception as exc:  # noqa: BLE001
        log.warning("推荐理由生成失败，改用规则兜底 | user_id=%s | %s: %s",
                    user_id, type(exc).__name__, exc)

    return rules_reason(user_id, items, behavior)


@app.get("/api/high-value-users")
def high_value_users(limit: int = 50):
    """高价值用户列表：一条 SQL 聚合出全部指标，不做 N+1 查询。"""
    limit = max(1, min(int(limit or 50), 500))
    try:
        # 一条 SQL 出全部指标：tier 之外的字段（浏览/加购/类目数/最近活跃）也在同一次聚德里算完
        # 说明：这里用 f-string 拼 limit 是为了让 SQL 保持单行可读；limit 已被强制转成受控整数
        columns, rows = execute_sql(
            "SELECT b.user_id, "
            "       SUM(b.behavior_type = 'buy')  AS buy_count, "
            "       SUM(b.behavior_type = 'pv')   AS view_count, "
            "       SUM(b.behavior_type = 'cart') AS cart_count, "
            "       COUNT(DISTINCT b.category_id) AS category_count, "
            "       DATE_FORMAT(MAX(b.date), '%Y-%m-%d') AS last_active "
            "FROM user_behavior b "
            "GROUP BY b.user_id "
            f"ORDER BY buy_count DESC, view_count DESC LIMIT {limit}"
        )
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": friendly_error(exc, "读取高价值用户")}, status_code=500)

    users = []
    for row in rows:
        d = dict(zip(columns, row))
        buy_count = int(d["buy_count"] or 0)
        users.append({
            "user_id": d["user_id"],
            "buy_count": buy_count,
            "view_count": int(d["view_count"] or 0),
            "cart_count": int(d["cart_count"] or 0),
            "category_count": int(d["category_count"] or 0),
            "last_active": d["last_active"] or "",
            "tier": tier_of(buy_count),
        })

    valuable = [u for u in users if u["tier"] != "普通"]
    avg_buy = round(sum(u["buy_count"] for u in users) / len(users), 2) if users else 0
    out = {
        "ok": True,
        "users": users,
        "summary": {
            "high_value_count": len(valuable),
            "core_count": sum(1 for u in users if u["tier"] == "核心"),
            "grow_count": sum(1 for u in users if u["tier"] == "成长"),
            "avg_buy_count": avg_buy,
            "tagged_count": count_jsonl(USER_TAGS_FILE),
        },
        "tier_rules": {
            "核心": f"购买次数 ≥ {TIER_CORE_MIN}",
            "成长": f"购买次数 ≥ {TIER_GROW_MIN}",
            "普通": f"购买次数 < {TIER_GROW_MIN}",
        },
    }
    log.info("high_value_users | limit=%s | users=%s | high_value=%s",
             limit, len(users), len(valuable))
    return JSONResponse(out)


class TagUserRequest(BaseModel):
    user_id: int
    tier: str = ""
    action: str = ""


@app.post("/api/tag-user")
def tag_user(req: TagUserRequest):
    """记录"给某用户下发了某项服务"（JSONL，一行一条）。"""
    action = (req.action or "").strip()
    if not req.user_id or not action:
        return JSONResponse({"ok": False, "error": "user_id 与 action 不能为空"}, status_code=400)
    record = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "user_id": req.user_id,
        "tier": (req.tier or "").strip()[:8],
        "action": action[:64],
    }
    try:
        append_jsonl(USER_TAGS_FILE, record)
    except OSError as exc:
        log.error("写运营动作失败 | %s: %s", type(exc).__name__, exc)
        return JSONResponse({"ok": False, "error": "写入失败，已记录日志"}, status_code=500)
    log.info("tag_user | user_id=%s | tier=%s | action=%s", req.user_id, record["tier"], record["action"])
    return JSONResponse({"ok": True, "tagged_count": count_jsonl(USER_TAGS_FILE)})


@app.get("/api/recommend")
def recommend(user_id: int = 0):
    """给某个用户算推荐商品 + 一句推荐理由。

    候选池分两步（都是纯 SQL，不做 N+1）：
      a. 该用户加购/收藏过、但从未购买的商品 —— 意图最强；
      b. 不足 5 个时，用其最常浏览类目下的热销商品补齐。
    LLM 只负责写理由，失败时走规则兜底，接口不返回 500。
    """
    if not user_id:
        return JSONResponse({"ok": False, "error": "缺少 user_id"}, status_code=400)

    try:
        conn = db_connect()
        try:
            with conn.cursor() as cur:
                # ---- 用户行为摘要 ----
                cur.execute(
                    """
                    SELECT SUM(behavior_type = 'buy'), SUM(behavior_type = 'pv'),
                           SUM(behavior_type = 'fav'), SUM(behavior_type = 'cart'),
                           MAX(date)
                    FROM user_behavior WHERE user_id = %s
                    """,
                    (user_id,),
                )
                buy_n, view_n, fav_n, cart_n, last_dt = cur.fetchone()
                if not any([buy_n, view_n, fav_n, cart_n]):
                    return JSONResponse({"ok": False, "error": f"用户 {user_id} 没有任何行为记录"},
                                        status_code=404)

                cur.execute(
                    """
                    SELECT COALESCE(c.name, '未分类') AS category_name, COUNT(*) AS n
                    FROM user_behavior b
                    LEFT JOIN categories c ON c.category_id = b.category_id
                    WHERE b.user_id = %s AND b.behavior_type = 'pv'
                    GROUP BY category_name ORDER BY n DESC LIMIT 1
                    """,
                    (user_id,),
                )
                top_row = cur.fetchone()
                top_category = top_row[0] if top_row else "未分类"

                # ---- a. 加购/收藏过但未购买（意图最强，按加购次数降序）----
                cur.execute(
                    """
                    SELECT item_id, cart_n, fav_n FROM (
                        SELECT item_id,
                               SUM(behavior_type = 'cart') AS cart_n,
                               SUM(behavior_type = 'fav')  AS fav_n
                        FROM user_behavior
                        WHERE user_id = %s AND behavior_type IN ('cart', 'fav')
                          AND item_id NOT IN (
                              SELECT item_id FROM user_behavior
                              WHERE user_id = %s AND behavior_type = 'buy'
                          )
                        GROUP BY item_id
                        ORDER BY cart_n DESC, fav_n DESC
                        LIMIT %s
                    ) t
                    """,
                    (user_id, user_id, RECOMMEND_LIMIT),
                )
                cart_rows = cur.fetchall()

                # ---- b. 不足 RECOMMEND_LIMIT 个时，用其最常浏览类目下的热销商品补齐 ----
                need = RECOMMEND_LIMIT - len(cart_rows)
                hot_rows: list[tuple] = []
                if need > 0:
                    cur.execute(
                        """
                        SELECT b.item_id
                        FROM user_behavior b
                        JOIN categories c ON c.category_id = b.category_id
                        WHERE c.name = %s AND b.behavior_type = 'buy'
                          AND b.item_id NOT IN (
                              SELECT item_id FROM user_behavior
                              WHERE user_id = %s AND behavior_type = 'buy'
                          )
                        GROUP BY b.item_id
                        ORDER BY COUNT(*) DESC
                        LIMIT %s
                        """,
                        (top_category, user_id, need),
                    )
                    hot_rows = cur.fetchall()

                cart_ids = [r[0] for r in cart_rows]
                hot_ids = [r[0] for r in hot_rows]
                item_map = fetch_items(cur, cart_ids + hot_ids)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": friendly_error(exc, "生成推荐")}, status_code=500)

    items = []
    for iid in cart_ids[:RECOMMEND_LIMIT]:
        it = item_map.get(iid)
        if it:
            items.append({**it, "from_cart": True})
    for iid in hot_ids[:max(0, RECOMMEND_LIMIT - len(items))]:
        it = item_map.get(iid)
        if it:
            items.append({**it, "from_cart": False})

    behavior = {
        "user_id": user_id,
        "buy_count": int(buy_n or 0),
        "view_count": int(view_n or 0),
        "fav_count": int(fav_n or 0),
        "cart_count": int(cart_n or 0),
        "top_category": top_category,
        "last_active": last_dt.strftime("%Y-%m-%d") if last_dt else "",
    }

    if not items:
        return JSONResponse({
            "ok": True,
            "items": [],
            "reason": "该用户还没有可推荐的候选商品（既没有加购/收藏未购买的商品，同偏好类目下也没有热销商品）",
            "action": "先观察，暂不打扰",
            "behavior": behavior,
        })

    reason = build_recommend_reason(user_id, items, behavior)
    action = f"向该用户推送 {len(items)} 个商品（{behavior['top_category']}偏好）并发放专属优惠券"
    log.info("recommend | user_id=%s | items=%s | from_cart=%s",
             user_id, len(items), len(cart_ids))
    return JSONResponse({"ok": True, "items": items, "reason": reason,
                         "action": action, "behavior": behavior})


@app.get("/console")
def console():
    """高价值用户运营台页面。"""
    return FileResponse(os.path.join(_here, "static", "console.html"))


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    print(f"\n  电商数据智能分析 Agent 已启动 ->  http://127.0.0.1:{port}\n")
    uvicorn.run(app, host="127.0.0.1", port=port)
