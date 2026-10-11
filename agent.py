"""Agent 编排：让模型自己决定调哪个工具、调几步。

和 app.py 里 pipeline() 的区别（这也是"流水线"和"Agent"的区别）：
  pipeline()  ：代码写死顺序 —— 生成 SQL → 校验 → 执行 → 出洞察，走几步是固定的；
  run_agent() ：只告诉模型"你有三个工具"，走几步、调哪个由模型决定。

这个文件刻意写得朴素：一个 while 循环 + 一个轨迹列表，不用类、不用框架。
每一步做了什么都能打印出来，方便调试和给用户看执行过程。
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from app import (DEEPSEEK_MODEL, _llm, clean_sql, execute_sql, guard_sql, log,
                 new_usage, track_usage, SqlGuardError)
from tools import run_tool, to_openai_tools

# 最多允许模型连续调几次工具（防止一直查下去烧 token）
MAX_STEPS = 3

# 单个工具结果塞回给模型时的字数上限（工具自己也会截断，这里再兜一层）
TOOL_RESULT_LIMIT = 2000

AGENT_SYSTEM = """你是一个电商数据分析助手，可以调用工具来回答用户的问题。

你有四个工具：
1. query_schema：查看有哪些表、字段和字段注释。不确定表结构时先调它；
2. query_column_values：查看某一列有哪些取值。不确定取值口径时（比如行为类型到底是 pv 还是 view）调它；
3. run_sql：执行一条只读 SELECT 查询并返回结果；
4. search_docs：检索运营口径文档。问到"转化率怎么算""活跃用户怎么定义""复购率""高价值用户分层"这类指标口径问题时，先调它，不要自己猜口径。

工作方式：
1. 需要数据时调用 run_sql，不要凭空编造数字；
2. 不确定表名、字段名或取值口径时，先调前两个工具确认，再写 SQL；
3. 问到指标定义、统计口径、分层规则时，先调 search_docs；
4. 一次工具调用只解决一个问题，拿到结果后判断是否还需要继续查；
5. 拿到足够信息后，直接用中文回答用户，给出结论和关键数字；
6. 如果用户的字段或数据根本不存在，直接说明"无法回答"以及缺什么，不要硬编 SQL。"""

# 模型偶尔会跳过工具、直接在回答里给一段 SQL，这时用这段兜底把它执行掉
_SQL_BLOCK_RE = re.compile(r"```sql\s*(.*?)```", re.I | re.S)


def _cut(text: str, limit: int = TOOL_RESULT_LIMIT) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "……（已截断）"


def extract_sql_block(text: str) -> str:
    """从回答里抠出 ```sql 代码块。

    用途是兜底：模型有时候不调工具，直接甩一段 SQL 出来。
    这时候把 SQL 抽出来真跑一遍，至少能给出真实数字。
    """
    match = _SQL_BLOCK_RE.search(text or "")
    return clean_sql(match.group(1)) if match else ""


def fallback_run_sql(text: str) -> tuple[str, str]:
    """兜底执行模型写在回答里的 SQL。

    返回 (要附加到回答末尾的文字, SQL)。没有可执行的 SQL 时返回 ("", "")。
    """
    sql = extract_sql_block(text)
    if not sql:
        return "", ""
    try:
        columns, rows = execute_sql(guard_sql(sql))
    except (SqlGuardError, Exception) as exc:  # noqa: BLE001
        log.warning("兜底执行 SQL 失败 | %s: %s | SQL: %s", type(exc).__name__, exc, sql)
        return "", ""
    if not rows:
        return "（系统兜底执行了上面的 SQL：查询结果为空）", sql
    preview = [dict(zip(columns, r)) for r in rows[:5]]
    text = (f"（系统兜底执行了上面的 SQL，共 {len(rows)} 行，前 5 行："
            f"{json.dumps(preview, ensure_ascii=False, default=str)}）")
    return _cut(text), sql


def _tool_call_args(raw_args: str) -> dict:
    """解析模型给的工具参数。解析失败就当作空参数，让工具自己去报参数问题。"""
    if not raw_args:
        return {}
    try:
        args = json.loads(raw_args)
        return args if isinstance(args, dict) else {}
    except json.JSONDecodeError:
        log.warning("工具参数不是合法 JSON，按空参数处理 | %s", raw_args[:200])
        return {}


def run_agent(question: str, max_steps: int = MAX_STEPS,
              on_step: Callable[[dict], None] | None = None) -> dict:
    """跑一次 Agent 对话。

    返回：{"answer": 最终中文回答, "steps": 执行轨迹列表, "usage": token 统计}
    轨迹里每一步是 {"step": 第几步, "tool": 工具名, "args": 参数, "result": 工具返回}

    on_step 是可选的：传进来一个函数，每执行完一步就回调一次，
    前端要「边跑边显示执行过程」就靠它（不传也不影响命令行使用）。
    """
    question = (question or "").strip()
    usage = new_usage()
    steps: list[dict[str, Any]] = []
    if not question:
        return {"answer": "问题不能为空", "steps": steps, "usage": usage}

    messages: list[dict] = [
        {"role": "system", "content": AGENT_SYSTEM},
        {"role": "user", "content": question},
    ]

    try:
        client = _llm()
    except Exception as exc:  # noqa: BLE001
        log.error("Agent 初始化模型失败 | %s: %s", type(exc).__name__, exc)
        return {"answer": f"调用模型失败：{type(exc).__name__}（详情见后端日志）",
                "steps": steps, "usage": usage}

    answer = ""
    step = 0
    while step <= max_steps:
        try:
            resp = client.chat.completions.create(
                model=DEEPSEEK_MODEL,
                messages=messages,
                tools=to_openai_tools(),
                temperature=0.1,
            )
        except Exception as exc:  # noqa: BLE001
            log.error("Agent 调用模型失败 | %s: %s", type(exc).__name__, exc)
            answer = f"调用模型失败：{type(exc).__name__}（详情见后端日志）"
            break

        track_usage(usage, resp)
        msg = resp.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None)

        # 模型没有再要工具，说明它给答案了
        if not tool_calls:
            answer = (msg.content or "").strip()
            # 兜底：模型一次工具都没调，却在回答里写了 SQL —— 帮它真的执行一遍
            if not steps:
                extra, sql = fallback_run_sql(answer)
                if extra:
                    answer = f"{answer}\n\n{extra}"
                    steps.append({"step": 0, "tool": "fallback_run_sql",
                                  "args": {"sql": sql}, "result": extra})
                    log.info("Agent 兜底执行了回答里的 SQL | SQL: %s", sql)
            break

        # 模型要调工具：把这一轮 assistant 消息原样放回对话，再逐条执行
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name,
                                 "arguments": tc.function.arguments},
                }
                for tc in tool_calls
            ],
        })

        for tc in tool_calls:
            step += 1
            name = tc.function.name
            args = _tool_call_args(tc.function.arguments)
            result = _cut(run_tool(name, args))
            item = {"step": step, "tool": name, "args": args, "result": result}
            steps.append(item)
            log.info("Agent 第 %s 步 | 工具=%s | 参数=%s | 结果长度=%s",
                     step, name, args, len(result))
            if on_step:
                try:
                    on_step(item)
                except Exception as exc:  # noqa: BLE001  回调出错不能影响主流程
                    log.warning("on_step 回调失败 | %s: %s", type(exc).__name__, exc)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })

        # 工具调用次数已经用完，就再问一次模型，让它基于现有信息作答
        if step >= max_steps:
            messages.append({
                "role": "user",
                "content": "工具调用次数已用完，请基于上面的结果直接回答，不要再调用工具。",
            })

    if not answer:
        answer = "达到工具调用次数上限，仍没有得出结论。可以缩小问题范围后重试。"

    log.info("Agent 完成 | 问题=%r | 步数=%s | tokens=%s | 回答长度=%s",
             question, len(steps), usage.get("total_tokens", 0), len(answer))
    return {"answer": answer, "steps": steps, "usage": usage}


if __name__ == "__main__":
    import sys
    import time

    q = sys.argv[1] if len(sys.argv) > 1 else "各行为类型的数量分布，数据范围 2017-11-25 至 2017-12-03"
    t0 = time.time()
    result = run_agent(q)
    print("=" * 60)
    print("问题：", q)
    print("-" * 60)
    for s in result["steps"]:
        preview = s["result"].replace("\n", " ")[:150]
        print(f"[第 {s['step']} 步] 调用 {s['tool']}  参数={s['args']}")
        print(f"          返回：{preview}")
    print("-" * 60)
    print("最终回答：")
    print(result["answer"])
    print("-" * 60)
    print(f"耗时 {time.time() - t0:.1f}s | 工具调用 {len(result['steps'])} 次 "
          f"| tokens={result['usage'].get('total_tokens', 0)}")
