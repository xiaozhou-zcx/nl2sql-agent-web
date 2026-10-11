"""工具层测试：验证三个工具能被单独调用、返回字符串、出错不抛异常。

跑法：python -m pytest tests -q
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tools  # noqa: E402

# 这些用例要有数据才能验证，本地用 sql/00_init.sql 导入的样例库
os.environ.setdefault("DB_NAME", "demo_ops")


def test_工具清单里的每个工具都有名字和说明() -> None:
    # 工具是逐步加上来的，所以这里只校验"结构完整"，不锁死具体数量
    assert len(tools.TOOLS) >= 3
    for t in tools.TOOLS:
        assert t["name"] and t["description"]


def test_查表结构返回字段说明() -> None:
    text = tools.query_schema()
    assert "user_behavior" in text
    assert "behavior_type" in text


def test_查列值能拿到行为类型() -> None:
    text = tools.query_column_values("behavior_type")
    assert "pv" in text
    # 取值只可能是这四种之一
    assert any(k in text for k in ("buy", "cart", "fav"))


def test_查列值支持表名点字段名写法() -> None:
    assert "pv" in tools.query_column_values("user_behavior.behavior_type")


def test_非法列名被拒绝而不是拼进SQL() -> None:
    text = tools.query_column_values("behavior_type; DROP TABLE users")
    assert "不合法" in text
    text2 = tools.query_column_values("user_behavior.behavior_type--")
    assert "不合法" in text2


def test_不存在的字段给出提示() -> None:
    text = tools.query_column_values("not_exist_column")
    assert "没有找到" in text


def test_跑SQL能查到数据() -> None:
    text = tools.run_sql("SELECT behavior_type, COUNT(*) AS n FROM user_behavior "
                         "GROUP BY behavior_type ORDER BY n DESC")
    assert "执行成功" in text
    assert "总行数" in text
    assert "pv" in text


def test_写操作被护栏拦截() -> None:
    text = tools.run_sql("DROP TABLE user_behavior")
    assert "拦截" in text


def test_多语句被拦截() -> None:
    assert "拦截" in tools.run_sql("SELECT 1; DELETE FROM user_behavior")


def test_空SQL给出参数提示() -> None:
    assert "不能为空" in tools.run_sql("")


def test_超大LIMIT被收敛到上限() -> None:
    import app

    text = tools.run_sql("SELECT * FROM user_behavior LIMIT 999999")
    assert "执行成功" in text
    assert int(app.SQL_HARD_LIMIT) <= 1000  # 护栏上限没被改坏


def test_工具结果超长会被截断() -> None:
    long_sql = "SELECT user_id, item_id, category_id, behavior_type FROM user_behavior"
    text = tools.run_sql(long_sql)
    assert len(text) <= tools.MAX_TOOL_CHARS + 60


def test_未知工具不抛异常() -> None:
    text = tools.run_tool("not_a_tool", {})
    assert "没有名为" in text


def test_缺参数不抛异常() -> None:
    assert "缺少参数" in tools.run_tool("run_sql", {})
    assert "缺少参数" in tools.run_tool("query_column_values", {})


def test_分发器能正确路由() -> None:
    assert "user_behavior" in tools.run_tool("query_schema")


@pytest.mark.parametrize("column", ["", "   ", "."])
def test_空列名被拒绝(column: str) -> None:
    assert "不合法" in tools.query_column_values(column)
