"""NL2SQL 链路评估脚本。

它跑的是和 Web 界面完全相同的链路（读表结构 → 生成 SQL → 护栏 → 执行），
只是不经过浏览器，方便批量统计：

    python eval/run_eval.py            # 全量 25 条
    python eval/run_eval.py --limit 5  # 先跑 5 条，省 API 成本
    python eval/run_eval.py --no-insight   # 不生成业务洞察（只测取数，最省）

判分口径（刻意保持朴素，够用就好）：
    执行成功   ：生成的 SQL 通过了护栏并且 MySQL 没报错
    语义命中   ：case 里 must_have 的关键词全部出现在生成的 SQL 里
    正确拒答   ：超纲问题（expect_refusal）走了拒答通道，没有硬编 SQL
关键词而非逐字比对：模型对同一口径的别名、排序列、大小写每次都不同，
逐字比对会把正确的 SQL 判成错，衡量"语义是否对"比"字符是否一样"更有意义。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

# 让脚本能直接 import 到项目根目录的 app.py
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import app  # noqa: E402

CASES_FILE = os.path.join(ROOT, "eval", "cases.json")


def normalize(sql: str) -> str:
    """压掉空白差异，只留下关键内容，便于做子串匹配。"""
    return " ".join((sql or "").split()).upper()


def semantic_hit(sql: str, must_have: list[str]) -> bool:
    if not must_have:
        return True
    body = normalize(sql)
    return all(normalize(k) in body for k in must_have)


def run_case(case: dict, schema: str, with_insight: bool, resume: bool = False) -> dict:
    """跑单条用例，返回判分结果。

    resume=True 时按"模型先答、失败再重试"的顺序跑：先完整走一遍正常链路，
    只有失败才把 MySQL 报错回传给模型重试。这样统计出的重试次数才代表真实成本。
    """
    out = {
        "id": case["id"],
        "question": case["question"],
        "expect_refusal": bool(case.get("expect_refusal")),
        "sql": "",
        "rows": None,
        "executed": False,
        "hit": False,
        "refused": False,
        "retries": 0,
        "elapsed": 0.0,
        "tokens": 0,
        "note": "",
    }
    t0 = time.time()
    usage = app.new_usage()

    try:
        raw = app.generate_sql(case["question"], schema, [], "", usage)
    except Exception as exc:  # noqa: BLE001
        out["note"] = app.friendly_error(exc, "调用大模型")
        out["elapsed"] = round(time.time() - t0, 2)
        return out

    # 超纲问题：正确答案是拒答
    reason = app.parse_refusal(raw)
    if reason:
        out["refused"] = True
        out["hit"] = out["expect_refusal"]
        out["note"] = f"拒答：{reason}"
        out["elapsed"] = round(time.time() - t0, 2)
        out["tokens"] = usage["total_tokens"]
        return out

    sql = app.clean_sql(raw)
    out["sql"] = sql

    last_error = ""
    for attempt in range(app.MAX_SQL_RETRY + 1):
        out["retries"] = attempt
        try:
            safe_sql = app.guard_sql(sql)
            if safe_sql != sql:
                out["note"] = "已自动收敛 LIMIT"
                sql = safe_sql
                out["sql"] = sql
        except app.SqlGuardError as exc:
            last_error = f"安全检查未通过：{exc}"
            out["note"] = last_error
            if attempt < app.MAX_SQL_RETRY:
                continue
            out["elapsed"] = round(time.time() - t0, 2)
            out["tokens"] = usage["total_tokens"]
            return out

        try:
            app.execute_sql(sql)
            out["executed"] = True
            break
        except Exception as exc:  # noqa: BLE001
            last_error = app.error_for_model(exc)
            out["note"] = last_error
            if not resume:
                # 非 resume 模式：发现执行失败就重发一次（旧行为，用于对比）
                try:
                    raw = app.generate_sql(case["question"], schema, [], last_error, usage)
                except Exception as exc2:  # noqa: BLE001
                    out["note"] = app.friendly_error(exc2, "调用大模型")
                    break
                if app.parse_refusal(raw):
                    out["refused"] = True
                    out["hit"] = out["expect_refusal"]
                    break
                sql = app.clean_sql(raw)
                out["sql"] = sql
                continue
            # resume 模式：正常失败只记一次，不再自动重试（重试留给下一轮真正失败时）
            out["elapsed"] = round(time.time() - t0, 2)
            out["tokens"] = usage["total_tokens"]
            return out

    if out["executed"] and not out["expect_refusal"]:
        out["hit"] = semantic_hit(out["sql"], case.get("must_have", []))
        if not out["hit"]:
            out["note"] = out["note"] or "关键词未命中"

    # 额外跑一次业务洞察（只在需要统计 token / 端到端耗时的时候）
    if with_insight and out["executed"]:
        try:
            columns, rows = app.execute_sql(out["sql"])
            out["rows"] = len(rows)
            app.generate_insight(case["question"], out["sql"], columns, rows, len(rows), usage)
        except Exception as exc:  # noqa: BLE001
            out["note"] = (out["note"] + " | " + app.friendly_error(exc, "生成业务洞察")).strip(" |")

    out["elapsed"] = round(time.time() - t0, 2)
    out["tokens"] = usage["total_tokens"]
    return out


def render_markdown(results: list[dict], with_insight: bool) -> str:
    total = len(results)
    expect_refusal = [r for r in results if r["expect_refusal"]]
    normal = [r for r in results if not r["expect_refusal"]]

    exec_ok = sum(1 for r in normal if r["executed"])
    hit_ok = sum(1 for r in normal if r["hit"])
    refuse_ok = sum(1 for r in expect_refusal if r["refused"])
    avg_elapsed = sum(r["elapsed"] for r in results) / total if total else 0
    tokens = sum(r["tokens"] for r in results)

    def rate(a: int, b: int) -> str:
        return f"{a / b * 100:.1f}%" if b else "-"

    lines = [
        "## 评估结果",
        "",
        f"- 运行时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- 用例数：{total}（取数类 {len(normal)}，超纲类 {len(expect_refusal)}）",
        f"- 是否生成业务洞察：{'是' if with_insight else '否（--no-insight）'}",
        "",
        "| 指标 | 结果 |",
        "|---|---|",
        f"| SQL 执行成功率 | {exec_ok}/{len(normal)} = **{rate(exec_ok, len(normal))}** |",
        f"| 语义正确率（must_have 全命中） | {hit_ok}/{len(normal)} = **{rate(hit_ok, len(normal))}** |",
        f"| 超纲问题正确拒答率 | {refuse_ok}/{len(expect_refusal)} = **{rate(refuse_ok, len(expect_refusal))}** |",
        f"| 平均端到端耗时 | {avg_elapsed:.2f}s |",
        f"| 总 token 消耗 | {tokens:,} |",
        "",
        "| 用例 | 问题 | 执行 | 语义命中 | 拒答 | 重试 | 耗时(s) | token | 备注 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r['id']} | {r['question']} | {'✅' if r['executed'] else '❌'} | "
            f"{'✅' if r['hit'] else '❌'} | {'✅' if r['refused'] else '—'} | "
            f"{r['retries']} | {r['elapsed']:.2f} | {r['tokens']} | {r['note'][:60]} |"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="NL2SQL 链路评估")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条，0 表示全量")
    parser.add_argument("--no-insight", action="store_true", help="不生成业务洞察（省 token）")
    parser.add_argument("--out", default="", help="把 Markdown 结果写到指定文件")
    parser.add_argument("--cases", default=CASES_FILE, help="用例集路径，默认 eval/cases.json")
    parser.add_argument("--resume", action="store_true",
                        help="失败先记一次、不自动重试（按真实重试成本统计）")
    args = parser.parse_args()

    with open(args.cases, encoding="utf-8") as f:
        cases = json.load(f)["cases"]
    if args.limit:
        cases = cases[: args.limit]

    os.environ.setdefault("DB_NAME", "demo_ops")

    schema = app.build_schema_text(force=True)
    results = []
    for i, case in enumerate(cases, 1):
        print(f"[{i}/{len(cases)}] {case['id']} {case['question']}", flush=True)
        r = run_case(case, schema, with_insight=not args.no_insight, resume=args.resume)
        results.append(r)
        print(f"    -> 执行={'OK' if r['executed'] else 'FAIL'} 命中={r['hit']} "
              f"拒答={r['refused']} 重试={r['retries']} 耗时={r['elapsed']}s tokens={r['tokens']}", flush=True)

    md = render_markdown(results, with_insight=not args.no_insight)
    print("\n" + md)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(md + "\n")
        # 同时落一份原始结果（JSON），方便后续写简历/README 时引用具体数字
        raw_path = os.path.splitext(args.out)[0] + ".json"
        with open(raw_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "run_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "cases_file": os.path.basename(args.cases),
                    "resume_mode": bool(args.resume),
                    "with_insight": not args.no_insight,
                    "results": results,
                    "summary": {
                        "total": len(results),
                        "exec_ok": sum(1 for r in results if r["executed"]),
                        "hit_ok": sum(1 for r in results if r["hit"]),
                        "refused_ok": sum(1 for r in results
                                          if r["expect_refusal"] and r["refused"]),
                        "avg_elapsed": round(sum(r["elapsed"] for r in results) / len(results), 2),
                        "total_tokens": sum(r["tokens"] for r in results),
                    },
                },
                f, ensure_ascii=False, indent=2,
            )
        print(f"\n已写入 {args.out}")
        print(f"已写入 {raw_path}")


if __name__ == "__main__":
    main()
