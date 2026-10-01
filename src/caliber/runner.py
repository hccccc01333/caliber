"""评测 runner：跑评测集并出报告。

用法：
    python -m caliber.runner --db data/synth.db
    python -m caliber.runner --db data/synth.db --only C
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from . import cases as cases_mod
from .agent import BASELINES, RuleAgent, get_agent
from .score import AgentOutput, EvalReport, score_case


def evaluate(con: sqlite3.Connection, case_list: list[cases_mod.Case],
             agent: RuleAgent | None = None) -> EvalReport:
    agent = agent or RuleAgent()
    results = [score_case(con, c, agent.answer(c.question)) for c in case_list]
    return EvalReport(results=results)


def render_baseline_table(con: sqlite3.Connection,
                          case_list: list[cases_mod.Case]) -> str:
    """三档基线横向对比：naive（硬答）/ rule（查口径）/ clarify（永远反问）。"""
    cats = sorted({c.category for c in case_list})
    lines = ["", "基线对比（分类型得分，括号内为一票否决题数）", "-" * 68]
    lines.append(f"{'agent':<10}" + "".join(f"{c:<14}" for c in cats) + "总分")
    for name in BASELINES:
        rep = evaluate(con, case_list, get_agent(name))
        per = rep.by_category()
        row = f"{name:<10}" + "".join(f"{per.get(c, 0):<14.3f}" for c in cats)
        lines.append(row + f"{rep.overall:.3f} ({rep.veto_count()})")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="跑 Caliber 口径评测集")
    ap.add_argument("--db", default="data/synth.db", help="SQLite 库路径")
    ap.add_argument("--agent", default="rule", choices=BASELINES + ["all"],
                    help="跑哪个 agent；all = 三档基线对比")
    ap.add_argument("--only", default="", help="只跑某一类，如 A / B / C / D / E")
    ap.add_argument("--verbose", action="store_true", help="打印每题的 agent 输出")
    args = ap.parse_args()

    db = Path(args.db)
    if not db.exists():
        raise SystemExit(f"找不到库：{db}，先跑 python -m caliber.synth --out {db}")

    case_list = cases_mod.CASES
    if args.only:
        case_list = [c for c in case_list if c.category.startswith(args.only.upper())]

    con = sqlite3.connect(db)
    agent = get_agent(args.agent if args.agent != "all" else "rule")

    if args.verbose:
        for c in case_list:
            out: AgentOutput = agent.answer(c.question)
            print(f"\n[{c.id}] {c.question}")
            print(f"  kind={out.kind}  caliber={out.caliber}")
            if out.sql:
                print(f"  sql={out.sql}")
            if out.caveat:
                print(f"  caveat={out.caveat}")
            print(f"  text={out.text[:120]}")
        print()

    if args.agent == "all":
        print(evaluate(con, case_list, agent).render())
        print(render_baseline_table(con, case_list))
    else:
        print(evaluate(con, case_list, agent).render())


if __name__ == "__main__":
    main()
