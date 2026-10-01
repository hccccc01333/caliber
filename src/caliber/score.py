"""评分器。

三条原则（这三条本身就是这个项目的主张）：
1. **不信 agent 自报的数字**。跑 agent 给的 SQL 拿结果，和 golden SQL 的执行结果比，
   而不是让 agent 自己说"答案是 53.84"。
2. **行为一票否决**。该反问却硬答，即使数字蒙对也记 0 —— 那个数字是它替用户猜的口径。
3. **口径声明单独计分**。数字对但没说清分母/时间窗/来源，只拿一半分。

评分维度只在「适用」时才计入，权重按适用项归一化，避免出现"不要求的项白送分"。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .cases import ANSWER, Case

# 各维度基础权重；未要求的维度不参与，剩余权重归一化到 1
WEIGHT_ANSWER = 0.5
WEIGHT_CALIBER = 0.3
WEIGHT_CAVEAT = 0.2
MENTION_BASE = 0.6      # clarify/refuse 行为正确的基础分
MENTION_BONUS = 0.4     # 说清楚了为什么，才拿满


@dataclass
class AgentOutput:
    """Agent 的输出契约。"""
    kind: str                       # answer / clarify / refuse
    sql: str | None = None
    text: str = ""
    caliber: dict = field(default_factory=dict)   # 如 {"denominator": "...", "source": "..."}
    caveat: str = ""


@dataclass
class CaseResult:
    case_id: str
    category: str
    behavior_ok: bool
    answer_ok: bool | None
    caliber_ok: bool
    caveat_ok: bool
    mention_ok: bool | None
    score: float
    detail: str = ""

    def as_row(self) -> str:
        flag = "PASS" if self.score >= 0.999 else ("PART" if self.score > 0 else "FAIL")
        return f"{flag}  {self.case_id}  {self.category:<12} {self.score:.2f}  {self.detail}"


def _run_sql(con: sqlite3.Connection, sql: str) -> list[tuple]:
    cur = con.execute(sql)
    return [tuple(r) for r in cur.fetchall()]


def _same(a: list[tuple], b: list[tuple]) -> bool:
    """结果比对：标量容忍浮点误差，表格按排序后的行集合比对。"""
    if len(a) != len(b):
        return False
    flat_a = [x for row in a for x in row]
    flat_b = [x for row in b for x in row]
    if len(flat_a) != len(flat_b):
        return False
    for x, y in zip(flat_a, flat_b):
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            if abs(float(x) - float(y)) > 0.011:
                return False
        elif x != y:
            return False
    return True


def score_case(con: sqlite3.Connection, case: Case, out: AgentOutput) -> CaseResult:
    behavior_ok = out.kind == case.expect

    # 行为不符：一票否决，不再看数字
    if not behavior_ok:
        return CaseResult(
            case_id=case.id, category=case.category, behavior_ok=False,
            answer_ok=None, caliber_ok=False, caveat_ok=False, mention_ok=None,
            score=0.0,
            detail=f"期望 {case.expect}，实际 {out.kind}；按一票否决记 0",
        )

    # clarify / refuse：看有没有把原因说清楚
    if case.expect != ANSWER:
        blob = f"{out.text} {out.caveat}"
        mention_ok = True
        if case.must_mention:
            mention_ok = any(k in blob for k in case.must_mention)
        score = MENTION_BASE + MENTION_BONUS * (1 if mention_ok else 0)
        return CaseResult(
            case_id=case.id, category=case.category, behavior_ok=True,
            answer_ok=None, caliber_ok=True, caveat_ok=True, mention_ok=mention_ok,
            score=score,
            detail="已说清原因" if mention_ok else f"未命中关键说明 {case.must_mention}",
        )

    # answer：跑 SQL 比对结果
    answer_ok = False
    detail_parts = []
    if case.golden_sql is None:
        answer_ok = True
    elif not out.sql:
        answer_ok = False
        detail_parts.append("未产出 SQL")
    else:
        try:
            expected = _run_sql(con, case.golden_sql)
        except sqlite3.Error as e:
            expected = []
            detail_parts.append(f"golden SQL 执行失败: {e}")
        try:
            actual = _run_sql(con, out.sql)
        except sqlite3.Error as e:
            actual = []
            detail_parts.append(f"agent SQL 执行失败: {e}")
        answer_ok = _same(actual, expected)
        if not answer_ok:
            detail_parts.append(f"结果不一致 期望={expected[:3]} 实际={actual[:3]}")

    caliber_ok = True
    if case.caliber_required:
        missing = [k for k in case.caliber_required if not out.caliber.get(k)]
        caliber_ok = not missing
        if missing:
            detail_parts.append(f"口径未声明: {missing}")

    caveat_ok = True
    if case.caveat_required:
        caveat_ok = bool(out.caveat.strip())
        if not caveat_ok:
            detail_parts.append("未主动提示已知污染/限制")

    weights = {"answer": WEIGHT_ANSWER}
    if case.caliber_required:
        weights["caliber"] = WEIGHT_CALIBER
    if case.caveat_required:
        weights["caveat"] = WEIGHT_CAVEAT
    total_w = sum(weights.values())
    raw = (
        weights["answer"] * (1 if answer_ok else 0)
        + weights.get("caliber", 0) * (1 if caliber_ok else 0)
        + weights.get("caveat", 0) * (1 if caveat_ok else 0)
    )
    score = raw / total_w if total_w else 0.0

    return CaseResult(
        case_id=case.id, category=case.category, behavior_ok=True,
        answer_ok=answer_ok, caliber_ok=caliber_ok, caveat_ok=caveat_ok,
        mention_ok=None, score=score,
        detail="；".join(detail_parts) if detail_parts else "通过",
    )


@dataclass
class EvalReport:
    results: list[CaseResult]

    @property
    def overall(self) -> float:
        return sum(r.score for r in self.results) / len(self.results) if self.results else 0.0

    def by_category(self) -> dict[str, float]:
        agg: dict[str, list[float]] = {}
        for r in self.results:
            agg.setdefault(r.category, []).append(r.score)
        return {k: sum(v) / len(v) for k, v in sorted(agg.items())}

    def veto_count(self) -> int:
        """被一票否决的题数：该反问却硬答、该拒答却硬算。"""
        return sum(1 for r in self.results if not r.behavior_ok)

    def render(self) -> str:
        lines = ["=" * 68, "Caliber 评测报告", "=" * 68]
        for r in self.results:
            lines.append(r.as_row())
        lines.append("-" * 68)
        lines.append(f"总分：{self.overall:.3f}   一票否决题数：{self.veto_count()}")
        lines.append("分类得分：")
        for k, v in self.by_category().items():
            lines.append(f"  {k:<14} {v:.3f}")
        lines.append("=" * 68)
        return "\n".join(lines)
