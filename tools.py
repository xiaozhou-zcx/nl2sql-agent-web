"""Agent 工具层：模型可以调用的几个"能力"，以及最简单的分发入口。

设计原则（刻意保持朴素）：
  1. 每个工具都是一个普通函数，输入输出都是字符串，不用类、不用装饰器；
  2. 工具永远返回一个字符串，出错也返回字符串，不往外抛异常 ——
     第 3 步的 Agent 循环会反复调用它们，一个异常就会中断整个对话；
  3. 工具本身不碰模型，可以单独用 pytest 测（见 tests/test_tools.py）；
  4. 返回结果统一截断到 MAX_TOOL_CHARS，防止把大表塞爆上下文和 token。

第 ② 阶段的编排循环会这样用它：
    先看 TOOLS 列表里每个工具的说明 -> 让模型决定调哪个 -> run_tool(名字, 参数)
"""

from __future__ import annotations

import json
import os
import re

from app import (DB_CONFIG, SqlGuardError, build_schema_text, clean_sql,
                 db_connect, execute_sql, guard_sql, log)

# 工具返回给模型的字数上限：太长会挤占上下文、也会多花 token
MAX_TOOL_CHARS = 2000

# 查询和 SQL 工具里最多返回多少行（不是最终展示给用户的行数）
QUERY_ROW_LIMIT = 20

# 表名 / 列名白名单：只允许字母、数字、下划线，防止把用户输入拼进 SQL
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# 口径文档：按 ## 小节切开检索，最多返回几节
DOCS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "口径文档.md")
DOCS_TOP_N = 2


def _cut(text: str, limit: int = MAX_TOOL_CHARS) -> str:
    """统一截断：超长就砍掉并注明，让模型知道信息不完整。"""
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n……（内容过长已截断，完整内容见后端日志）"


def _quote_ident(name: str) -> str:
    """反引号包裹表名 / 列名。"""
    return "`" + name.replace("`", "``") + "`"


def parse_column(column: str) -> tuple[str, str]:
    """把模型给的列名解析成 (表名, 列名)，表名可能是空字符串。

    允许两种写法："behavior_type" 或 "user_behavior.behavior_type"。
    不合法就抛 ValueError，由上层转成给模型的提示。
    """
    column = (column or "").strip().strip("`")
    if "." in column:
        table, col = column.split(".", 1)
    else:
        table, col = "", column
    table, col = table.strip().strip("`"), col.strip().strip("`")
    for part in (table, col):
        if part and not _IDENT_RE.match(part):
            raise ValueError(f"列名不合法：{column}")
    if not col:
        raise ValueError("列名不能为空")
    return table, col


# ---------------------------------------------------------------- 工具 1

def query_schema() -> str:
    """工具 1：查看数据库有哪些表、每个表有哪些字段、字段里有哪些取值。"""
    try:
        schema = build_schema_text()
    except Exception as exc:  # noqa: BLE001
        log.warning("工具 query_schema 失败 | %s: %s", type(exc).__name__, exc)
        return f"查询表结构失败：{type(exc).__name__}（详情见后端日志）"
    return _cut(schema)


# ---------------------------------------------------------------- 工具 2

def query_column_values(column: str, limit: int = 10) -> str:
    """工具 2：看某一列都有哪些取值。

    模型经常不知道 behavior_type 是 pv/buy 还是 view/purchase，
    问一句就能拿到真实取值，比在提示词里反复强调"不要臆造"有效。
    """
    try:
        table, col = parse_column(column)
        limit = max(1, min(int(limit or 10), 50))
    except (ValueError, TypeError) as exc:
        return f"参数不合法：{exc}"

    conn = None
    try:
        conn = db_connect()
        with conn.cursor() as cur:
            if not table:
                # 没给表名时先找到唯一拥有这个列的表；有多个就报错让模型写清楚
                cur.execute(
                    """
                    SELECT TABLE_NAME FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = %s AND COLUMN_NAME = %s
                    ORDER BY TABLE_NAME
                    """,
                    (DB_CONFIG["database"], col),
                )
                tables = [r[0] for r in cur.fetchall()]
                if not tables:
                    return f"没有找到名为 {col} 的字段，可先用 query_schema 查看表结构"
                if len(tables) > 1:
                    return (f"字段 {col} 出现在多张表里（{'、'.join(tables)}），"
                            f"请写明表名，例如 {tables[0]}.{col}")
                table = tables[0]
            cur.execute(
                f"SELECT DISTINCT {_quote_ident(col)} AS v FROM {_quote_ident(table)} "
                f"WHERE {_quote_ident(col)} IS NOT NULL LIMIT {limit}"
            )
            values = [str(r[0]) for r in cur.fetchall()]
    except Exception as exc:  # noqa: BLE001
        log.warning("工具 query_column_values 失败 | %s | %s: %s", column, type(exc).__name__, exc)
        return f"查询列值失败：{type(exc).__name__}（详情见后端日志）"
    finally:
        if conn is not None:
            conn.close()

    if not values:
        return f"{table}.{col} 没有取到任何取值（可能全为空）"
    return f"{table}.{col} 的取值（{len(values)} 个）：" + " / ".join(values)


# ---------------------------------------------------------------- 工具 3

def run_sql(sql: str) -> str:
    """工具 3：执行一条只读 SQL，返回列名 + 行数 + 前若干行。

    护栏（guard_sql）在这里再走一遍：即使模型绕过了提示词约束，
    写操作和多语句也会被拦下来。
    """
    sql = clean_sql(sql or "")
    if not sql:
        return "参数不合法：sql 不能为空"
    try:
        safe_sql = guard_sql(sql)
    except SqlGuardError as exc:
        return f"SQL 被安全护栏拦截：{exc}"
    try:
        columns, rows = execute_sql(safe_sql)
    except Exception as exc:  # noqa: BLE001
        log.warning("工具 run_sql 执行失败 | %s: %s | SQL: %s", type(exc).__name__, exc, safe_sql)
        return f"SQL 执行失败：{type(exc).__name__}（详情见后端日志）"

    preview = [dict(zip(columns, r)) for r in rows[:QUERY_ROW_LIMIT]]
    payload = {"执行成功": True, "列名": columns, "总行数": len(rows), "数据": preview}
    return _cut(json.dumps(payload, ensure_ascii=False, default=str))


# ---------------------------------------------------------------- 工具 4

def load_docs() -> list[dict]:
    """把口径文档按 ## 小节读成列表：每个元素是 {"title": 小标题, "text": 正文}。

    用最朴素的方式切分（按行扫描），不引任何依赖。
    """
    sections: list[dict] = []
    title, lines = "", []
    try:
        with open(DOCS_FILE, encoding="utf-8") as f:
            content = f.read()
    except OSError as exc:
        log.warning("读取口径文档失败 | %s: %s", type(exc).__name__, exc)
        return []

    for line in content.splitlines():
        if line.startswith("## "):
            if title:
                sections.append({"title": title, "text": "\n".join(lines).strip()})
            title, lines = line[3:].strip(), []
        elif title:
            lines.append(line)
    if title:
        sections.append({"title": title, "text": "\n".join(lines).strip()})
    return sections


def search_docs(keyword: str, limit: int = DOCS_TOP_N) -> str:
    """工具 4：按关键词检索运营口径文档。

    做法很朴素：看每个小节里关键词出现了几次，出现多的排前面。
    够用就好 —— 口径文档只有几篇，不需要上向量检索。
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return "参数不合法：keyword 不能为空"

    sections = load_docs()
    if not sections:
        return "口径文档读取失败（详情见后端日志）"

    # 按空格 / 顿号拆成多个词，任意一个命中就算相关
    words = [w for w in re.split(r"[\s、,，/]+", keyword) if w]
    scored = []
    for i, sec in enumerate(sections):
        body = sec["title"] + "\n" + sec["text"]
        hits = sum(body.count(w) for w in words)
        if hits:
            scored.append((hits, i, sec))

    if not scored:
        titles = "、".join(s["title"] for s in sections)
        return f"文档里没有找到和「{keyword}」相关的内容。现有口径章节：{titles}"

    scored.sort(key=lambda x: (-x[0], x[1]))
    parts = []
    for _, _, sec in scored[: max(1, min(int(limit or DOCS_TOP_N), 5))]:
        parts.append(f"【{sec['title']}】\n{sec['text']}")
    return _cut("\n\n".join(parts))


# ---------------------------------------------------------------- 工具清单

# 每个工具就是一个小字典：模型看 name 和 description 决定调哪个，
# parameters 用最简单的"参数名 -> 说明"形式，第 ② 阶段再翻译成模型要的格式。
TOOLS: list[dict] = [
    {
        "name": "query_schema",
        "description": "查看数据库里有哪些表、每张表有哪些字段和字段注释、字段有哪些取值。不确定该查哪张表时先调它。",
        "parameters": {},
    },
    {
        "name": "query_column_values",
        "description": "查看某一列都有哪些取值。不确定字段取值口径时（比如 behavior_type 到底是 pv 还是 view）调它。",
        "parameters": {
            "column": "列名，可以只写字段名（behavior_type），也可以写表名.字段名（user_behavior.behavior_type）",
            "limit": "最多返回几个取值，默认 10",
        },
    },
    {
        "name": "run_sql",
        "description": "执行一条只读的 SELECT 查询并返回结果。只能查询，不能修改数据。",
        "parameters": {
            "sql": "要执行的 SELECT 语句，不要加分号",
        },
    },
    {
        "name": "search_docs",
        "description": "检索运营口径文档，比如'转化率怎么算''活跃用户怎么定义''复购率口径''高价值用户分层标准'。问到指标定义、口径、规则时先调它。",
        "parameters": {
            "keyword": "要查的关键词，例如 转化率 / 活跃用户 / 复购率 / 加购未购买",
        },
    },
]


# ---------------------------------------------------------------- 分发

def run_tool(name: str, args: dict | None = None) -> str:
    """按名字执行工具。永远返回字符串，出错也返回字符串而不是抛异常。"""
    args = args or {}
    if not name:
        return "工具执行失败：没有指定工具名"
    try:
        if name == "query_schema":
            return query_schema()
        if name == "query_column_values":
            if "column" not in args:
                return "工具执行失败：query_column_values 缺少参数 column"
            return query_column_values(args["column"], args.get("limit", 10))
        if name == "run_sql":
            if "sql" not in args:
                return "工具执行失败：run_sql 缺少参数 sql"
            return run_sql(args["sql"])
        if name == "search_docs":
            if "keyword" not in args:
                return "工具执行失败：search_docs 缺少参数 keyword"
            return search_docs(args["keyword"])
        return f"工具执行失败：没有名为 {name} 的工具"
    except Exception as exc:  # noqa: BLE001  兜底：循环里不能因为工具异常而中断
        log.error("工具 %s 异常 | %s: %s", name, type(exc).__name__, exc, exc_info=True)
        return f"工具执行失败：{type(exc).__name__}（详情见后端日志）"


def tool_names() -> list[str]:
    """给日志和测试用：当前有哪些工具。"""
    return [t["name"] for t in TOOLS]


def to_openai_tools() -> list[dict]:
    """把 TOOLS 翻译成模型要的工具格式（OpenAI SDK 的 tools 参数）。

    只是格式转换：每个工具变成一个 function 定义，参数统一按"字符串"声明。
    参数说明用 description 写清楚，模型主要靠它判断该传什么。
    """
    result = []
    for tool in TOOLS:
        params = tool.get("parameters", {})
        result.append({
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": {
                    "type": "object",
                    "properties": {
                        key: {"type": "string", "description": desc}
                        for key, desc in params.items()
                    },
                    "required": list(params.keys()),
                },
            },
        })
    return result
