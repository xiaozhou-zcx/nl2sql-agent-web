"""检索评测：衡量 query_schema 之外的另一个工具 —— search_docs 的路由准确率。

为什么要单独评检索：
    整个 Agent 里，RAG 的收益几乎全部来自"检索对没对" ——
    检索到错的小节，模型再强也只能基于错的资料回答。
    所以检索质量必须能测，而不是靠感觉。

跑法：
    python eval/run_retrieval_eval.py                          # 评当前版本
    python eval/run_retrieval_eval.py --compare-baseline        # 顺便算改造前的基线

指标：
    hit@2      期望小节出现在返回的前 2 节里
    top1       期望小节排在第一位
    超纲正确率 文档里没有的问题，应当检索不到（不要硬凑一个答案）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import tools  # noqa: E402

CASES_FILE = os.path.join(ROOT, "eval", "retrieval_cases.json")

KIND_LABEL = {
    "exact": "原词",
    "definition": "问定义",
    "rephrase": "换种说法",
    "beyond": "超纲",
}


def baseline_titles(question: str, limit: int = 2, path: str | None = None) -> list[str]:
    """改造前的算法：按空格/顿号拆词，统计整段出现次数，次数多的排前面。

    保留这份实现是为了能跑出"改造前 vs 改造后"的对比数字，
    写在 README 里比只说"我优化了检索"可信得多。
    """
    words = [w for w in re.split(r"[\s、,，/]+", question) if w]
    scored = []
    for i, sec in enumerate(tools.load_docs(path)):
        body = sec["title"] + "\n" + sec["text"]
        hits = sum(body.count(w) for w in words)
        if hits:
            scored.append((hits, i, sec["title"]))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [s[2] for s in scored[:limit]]


def evaluate(use_baseline: bool = False) -> dict:
    cases = json.load(open(CASES_FILE, encoding="utf-8"))["cases"]
    rows = []
    per_kind: dict[str, dict[str, int]] = {}

    for case in cases:
        if use_baseline:
            got = baseline_titles(case["question"])
        else:
            got = [s["title"] for s in tools.search_sections(case["question"])]
        expect = case["expect"]
        hit = (expect in got) if expect else (not got)
        top1 = bool(expect) and bool(got) and got[0] == expect

        kind = case["kind"]
        stat = per_kind.setdefault(kind, {"n": 0, "hit": 0, "top1": 0})
        stat["n"] += 1
        stat["hit"] += 1 if hit else 0
        stat["top1"] += 1 if top1 else 0

        rows.append({**case, "got": got, "hit": hit, "top1": top1})

    normal = [r for r in rows if r["expect"]]
    beyond = [r for r in rows if not r["expect"]]
    return {
        "rows": rows,
        "per_kind": per_kind,
        "normal_hit": sum(1 for r in normal if r["hit"]),
        "normal_top1": sum(1 for r in normal if r["top1"]),
        "normal_total": len(normal),
        "beyond_ok": sum(1 for r in beyond if r["hit"]),
        "beyond_total": len(beyond),
    }


def pct(a: int, b: int) -> str:
    return f"{a / b * 100:.0f}%" if b else "-"


def render(new: dict, base: dict | None) -> str:
    lines = [
        "## 检索评测结果",
        "",
        f"- 运行时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- 用例数：{new['normal_total'] + new['beyond_total']}"
        f"（取数类 {new['normal_total']} + 超纲 {new['beyond_total']}）",
        f"- 文档规模：{len(tools.load_docs())} 个小节",
        "",
    ]

    if base:
        lines += [
            "| 指标 | 改造前 | 改造后 |",
            "|---|---|---|",
            f"| 命中率 hit@2 | {pct(base['normal_hit'], base['normal_total'])} "
            f"| **{pct(new['normal_hit'], new['normal_total'])}** |",
            f"| Top1 准确率 | {pct(base['normal_top1'], base['normal_total'])} "
            f"| **{pct(new['normal_top1'], new['normal_total'])}** |",
            f"| 超纲正确率 | {pct(base['beyond_ok'], base['beyond_total'])} "
            f"| {pct(new['beyond_ok'], new['beyond_total'])} |",
            "",
        ]
    else:
        lines += [
            "| 指标 | 结果 |",
            "|---|---|",
            f"| 命中率 hit@2 | {new['normal_hit']}/{new['normal_total']} = "
            f"**{pct(new['normal_hit'], new['normal_total'])}** |",
            f"| Top1 准确率 | {new['normal_top1']}/{new['normal_total']} = "
            f"**{pct(new['normal_top1'], new['normal_total'])}** |",
            f"| 超纲正确率 | {new['beyond_ok']}/{new['beyond_total']} = "
            f"**{pct(new['beyond_ok'], new['beyond_total'])}** |",
            "",
        ]

    lines += ["| 类型 | 条数 | 命中 | 说明 |", "|---|---|---|---|"]
    for kind, stat in new["per_kind"].items():
        lines.append(f"| {KIND_LABEL.get(kind, kind)} | {stat['n']} | "
                     f"{stat['hit']}/{stat['n']} | "
                     f"{'原词直接匹配' if kind == 'exact' else ''}"
                     f"{'需要理解“在问定义”' if kind == 'definition' else ''}"
                     f"{'用户说法和文档用词不一致，考同义词' if kind == 'rephrase' else ''}"
                     f"{'文档里没有，应该检索不到' if kind == 'beyond' else ''} |")

    lines += [
        "",
        "| 用例 | 问题 | 期望命中 | 实际返回 | 结果 |",
        "|---|---|---|---|---|",
    ]
    for r in new["rows"]:
        expect = r["expect"] or "（应检索不到）"
        got = "、".join(r["got"]) or "（未命中）"
        lines.append(f"| {r['id']} | {r['question']} | {expect} | {got} | "
                     f"{'✅' if r['hit'] else '❌'} |")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="检索评测")
    parser.add_argument("--compare-baseline", action="store_true",
                        help="同时跑改造前的算法，输出对比表")
    parser.add_argument("--out", default="", help="把结果写到指定 Markdown 文件")
    args = parser.parse_args()

    new = evaluate(use_baseline=False)
    base = evaluate(use_baseline=True) if args.compare_baseline else None

    for r in new["rows"]:
        print(f"  {r['id']} [{KIND_LABEL.get(r['kind'], r['kind']):6}] "
              f"{r['question'][:22]:24} -> {'、'.join(r['got']) or '（未命中）'}"
              f"  {'OK' if r['hit'] else 'MISS'}")

    md = render(new, base)
    print("\n" + md)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(md + "\n")
        print(f"\n已写入 {args.out}")


if __name__ == "__main__":
    main()
