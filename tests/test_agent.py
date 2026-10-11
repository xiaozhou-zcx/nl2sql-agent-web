"""Agent 相关测试：知识库检索 + 兜底逻辑。

跑法：python -m pytest tests -q
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent  # noqa: E402
import tools  # noqa: E402

os.environ.setdefault("DB_NAME", "demo_ops")


# ---------------------------------------------------------------- 知识库

def test_口径文档能按小节切开() -> None:
    sections = tools.load_docs()
    titles = [s["title"] for s in sections]
    assert len(sections) >= 5
    assert "复购率口径" in titles
    assert all(s["text"] for s in sections)


def test_检索转化率能命中对应小节() -> None:
    text = tools.run_tool("search_docs", {"keyword": "转化率"})
    assert "转化率口径" in text
    assert "用户去重" in text


def test_检索复购率能命中对应小节() -> None:
    text = tools.run_tool("search_docs", {"keyword": "复购率"})
    assert "复购率口径" in text


def test_检索高价值用户能命中分层规则() -> None:
    text = tools.run_tool("search_docs", {"keyword": "高价值用户"})
    assert "高价值用户分层口径" in text
    assert "核心" in text


def test_检索不到时给出已有章节列表() -> None:
    text = tools.run_tool("search_docs", {"keyword": "宇宙飞船"})
    assert "没有找到" in text
    assert "复购率口径" in text  # 列出现有章节，方便模型改关键词


def test_缺关键词不抛异常() -> None:
    assert "缺少参数" in tools.run_tool("search_docs", {})
    assert "不合法" in tools.search_docs("   ")


def test_工具清单现在是四个() -> None:
    assert set(tools.tool_names()) == {"query_schema", "query_column_values",
                                       "run_sql", "search_docs"}


def test_检索结果会被截断() -> None:
    text = tools.search_docs("口径 用户 数据 行为", limit=5)
    assert len(text) <= tools.MAX_TOOL_CHARS + 60


# ---------------------------------------------------------------- 兜底

def test_能从代码块里抽出SQL() -> None:
    answer = "可以这样查：\n```sql\nSELECT COUNT(*) FROM user_behavior\n```\n以上。"
    assert agent.extract_sql_block(answer).startswith("SELECT COUNT(*)")


def test_没有代码块时抽不出SQL() -> None:
    assert agent.extract_sql_block("这个问题的答案是 42") == ""


def test_兜底能执行回答里的SQL() -> None:
    answer = "```sql\nSELECT COUNT(*) AS n FROM user_behavior\n```"
    extra, sql = agent.fallback_run_sql(answer)
    assert sql.startswith("SELECT COUNT(*)")
    assert "共" in extra and "行" in extra


def test_兜底遇到写操作不执行() -> None:
    answer = "```sql\nDROP TABLE user_behavior\n```"
    extra, sql = agent.fallback_run_sql(answer)
    assert extra == "" and sql == ""


def test_兜底遇到查不到的字段不抛异常() -> None:
    answer = "```sql\nSELECT not_exist_column FROM user_behavior\n```"
    extra, sql = agent.fallback_run_sql(answer)
    assert extra == "" and sql == ""
