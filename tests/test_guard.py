"""SQL 护栏与错误脱敏的单元测试。

护栏是"模型可能生成写操作"这条风险路径上唯一的静态防线，
它的行为必须能被一条命令回归，所以单独测：

    python -m pytest tests -q
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402

HARD = app.SQL_HARD_LIMIT


# ---------------------------------------------------------------- 拦截写操作

@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE user_behavior",
        "DELETE FROM user_behavior WHERE user_id = 1",
        "UPDATE user_behavior SET behavior_count = 1",
        "INSERT INTO users (user_id) VALUES (1)",
        "TRUNCATE TABLE user_behavior",
        "ALTER TABLE user_behavior ADD COLUMN x INT",
        "CREATE TABLE t (id INT)",
        "GRANT ALL ON *.* TO 'x'@'%'",
        "SELECT * FROM user_behavior INTO OUTFILE '/tmp/a.csv'",
        "SELECT LOAD_FILE('/etc/passwd')",
    ],
)
def test_写操作与危险函数被拦截(sql: str) -> None:
    with pytest.raises(app.SqlGuardError):
        app.guard_sql(sql)


def test_多语句被拦截() -> None:
    with pytest.raises(app.SqlGuardError):
        app.guard_sql("SELECT 1; DELETE FROM user_behavior")


def test_非查询语句被拦截() -> None:
    with pytest.raises(app.SqlGuardError):
        app.guard_sql("SET GLOBAL max_connections = 1000")


# ---------------------------------------------------------------- 强制行数上限

def test_没写LIMIT时自动补上() -> None:
    assert app.guard_sql("SELECT * FROM user_behavior").endswith(f"LIMIT {HARD}")


def test_超大LIMIT被压到上限() -> None:
    out = app.guard_sql("SELECT * FROM user_behavior LIMIT 999999")
    assert out.endswith(f"LIMIT {HARD}")
    assert "999999" not in out


def test_合理LIMIT保持不变() -> None:
    assert app.guard_sql("SELECT * FROM user_behavior LIMIT 500").endswith("LIMIT 500")


def test_LIMIT逗号写法只压count() -> None:
    out = app.guard_sql("SELECT * FROM user_behavior LIMIT 20, 999999")
    assert out.endswith(f"LIMIT 20, {HARD}")


def test_LIMIT_OFFSET写法只压count() -> None:
    out = app.guard_sql("SELECT * FROM user_behavior LIMIT 999999 OFFSET 30")
    assert out.endswith(f"LIMIT {HARD} OFFSET 30")


def test_子查询里的LIMIT也会被压() -> None:
    out = app.guard_sql("SELECT * FROM (SELECT * FROM user_behavior LIMIT 999999) t LIMIT 50")
    assert f"LIMIT {HARD}" in out
    assert out.endswith("LIMIT 50")


def test_只有子查询LIMIT时外层会补一个() -> None:
    out = app.guard_sql("SELECT * FROM (SELECT * FROM user_behavior LIMIT 5) t")
    assert out.count("LIMIT 5") == 1
    assert out.endswith(f"LIMIT {HARD}")


def test_结尾分号被去掉() -> None:
    assert ";" not in app.guard_sql("SELECT COUNT(*) FROM user_behavior;")


def test_markdown代码块被清洗() -> None:
    out = app.guard_sql("```sql\nSELECT 1\n```")
    assert out.startswith("SELECT 1")
    assert "```" not in out


def test_SHOW不补LIMIT也不报错() -> None:
    assert app.guard_sql("SHOW TABLES").startswith("SHOW TABLES")


# ---------------------------------------------------------------- 拒答识别

@pytest.mark.parametrize(
    "text",
    [
        "__无法回答__: 缺少字段 手机号，表结构里没有这类信息",
        "**__无法回答__**：表里没有金额字段",
    ],
)
def test_拒答标记能被识别(text: str) -> None:
    assert app.parse_refusal(text)


def test_正常SQL不会被误判为拒答() -> None:
    assert app.parse_refusal("SELECT COUNT(*) FROM user_behavior") == ""


def test_非SQL的推诿也会被识别为拒答() -> None:
    assert app.parse_refusal("抱歉，当前表结构里无法回答这个问题")


# ---------------------------------------------------------------- 错误脱敏

def test_友好报错不泄露库名与账号() -> None:
    import pymysql

    exc = pymysql.err.OperationalError(1049, "Unknown database 'taobao_analysis'")
    text = app.friendly_error(exc, "读取数据库结构")
    assert "taobao_analysis" not in text
    assert "1049" not in text
    assert "logs/app.log" in text


def test_回传给模型的错误只保留错误码与消息() -> None:
    import pymysql

    exc = pymysql.err.ProgrammingError(1054, "Unknown column 'foo' in 'field list'")
    text = app.error_for_model(exc)
    assert "1054" in text
    assert "Unknown column" in text


# ---------------------------------------------------------------- 结果摘要

def test_上一轮结果摘要够短且含行数() -> None:
    digest = app.result_digest(["a", "b"], [(1, 2), (3, 4)], 2)
    assert "a / b" in digest and "2 行" in digest


def test_空结果摘要不报错() -> None:
    assert "0 行" in app.result_digest(["a"], [], 0)
