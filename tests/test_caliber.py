"""Caliber 测试套件。

分四组：
1. 合成库：可复现 + 八类结构陷阱确实成立
2. 评测集：golden SQL 全部可执行、题目登记自洽
3. 评分器：一票否决、部分给分、结果比对这些规则真的生效
4. 基线：三档基线排序符合预期（口径意识有正收益，永远反问没有）
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from caliber import cases as C  # noqa: E402
from caliber.agent import get_agent  # noqa: E402
from caliber.score import AgentOutput, score_case  # noqa: E402
from caliber.synth import generate  # noqa: E402


# ---------------- 1. 合成库 ----------------

def test_synth_is_reproducible(tmp_path):
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    generate(a)
    generate(b)
    assert a.read_bytes() == b.read_bytes(), "合成库必须按固定种子完全可复现"


def test_trap_cross_table_join_is_empty(synth_db):
    """陷阱一：订单侧编号与用户表不可连接，强行 join 应静默返回 0 行。"""
    n = synth_db.execute(
        "SELECT COUNT(*) FROM orders_paid p JOIN users u ON p.pay_sn = u.device_sn"
    ).fetchone()[0]
    assert n == 0


def test_trap_device_snapshot_is_truncated(synth_db):
    """陷阱二：设备快照按行数截断，报告条数远大于设备数。"""
    rows = synth_db.execute("SELECT COUNT(*) FROM device_snapshot").fetchone()[0]
    units = synth_db.execute("SELECT COUNT(DISTINCT device_sn) FROM device_snapshot").fetchone()[0]
    assert rows > units * 10, "必须体现出抽样快照的特征：少量设备、大量上报"


def test_trap_daily_series_has_gap(synth_db):
    """陷阱三：每日汇总缺一整段日期。"""
    days = synth_db.execute("SELECT COUNT(*) FROM ads_daily_activation").fetchone()[0]
    total_days = synth_db.execute("SELECT COUNT(*) FROM dim_date").fetchone()[0]
    assert days < total_days, "汇总表必须存在日期缺口"


def test_trap_denominator_pollution(synth_db):
    """陷阱四：分母污染 —— 含系统自动激活会明显低估完成率。"""
    def rate(where: str) -> float:
        return synth_db.execute(
            f"SELECT 100.0 * SUM(CASE WHEN task1_time IS NOT NULL THEN 1 ELSE 0 END) "
            f"/ COUNT(*) FROM users WHERE {where}"
        ).fetchone()[0]

    polluted = rate("activate_time IS NOT NULL")
    clean = rate("activate_time IS NOT NULL AND auto_activated = 0")
    assert polluted < clean - 5, "含自动激活的口径必须显著低估完成率（真实差值约 8 个百分点）"


def test_trap_direct_purchase_exists(synth_db):
    """陷阱五：存在无激活记录的直购订单，转化率分母因此有两种。"""
    direct = synth_db.execute(
        "SELECT COUNT(*) FROM orders_paid WHERE activate_time IS NULL").fetchone()[0]
    total = synth_db.execute("SELECT COUNT(*) FROM orders_paid").fetchone()[0]
    assert 0.2 < direct / total < 0.6


def test_trap_cancel_reason_is_mechanism(synth_db):
    """陷阱六：取消绝大多数来自支付超时机制，不能归因成用户意愿。"""
    timeout = synth_db.execute(
        "SELECT COUNT(*) FROM orders_canceled WHERE cancel_reason = '支付超时自动取消'"
    ).fetchone()[0]
    total = synth_db.execute("SELECT COUNT(*) FROM orders_canceled").fetchone()[0]
    assert timeout / total > 0.9


def test_trap_two_sources_disagree(synth_db):
    """陷阱七：本地汇总与官方导出不一致，引用数字必须声明来源。"""
    local = synth_db.execute("SELECT SUM(activate_users) FROM ads_daily_activation").fetchone()[0]
    official = synth_db.execute("SELECT SUM(activate_users) FROM ads_official_daily").fetchone()[0]
    assert local != official


# ---------------- 2. 评测集 ----------------

def test_all_golden_sql_executable(synth_db):
    for c in C.CASES:
        if c.golden_sql:
            synth_db.execute(c.golden_sql).fetchall()


def test_case_ids_unique():
    ids = [c.id for c in C.CASES]
    assert len(ids) == len(set(ids))


def test_cases_are_well_formed():
    for c in C.CASES:
        assert c.category in C.CATEGORIES, c.id
        assert c.expect in (C.ANSWER, C.CLARIFY, C.REFUSE), c.id
        if c.expect == C.ANSWER:
            assert c.golden_sql, f"{c.id} 是 answer 题，必须有 golden SQL"
        else:
            assert c.must_mention, f"{c.id} 是非 answer 题，必须有 must_mention 才能判分"


def test_every_category_covered():
    covered = {c.category for c in C.CASES}
    assert covered == set(C.CATEGORIES)


# ---------------- 3. 评分器 ----------------

def _case(**kw) -> C.Case:
    base = dict(id="X", category=C.A_DIRECT, question="q", expect=C.ANSWER,
                golden_sql="SELECT COUNT(*) FROM t")
    base.update(kw)
    return C.Case(**base)


def test_veto_when_should_clarify_but_answers(mem_db):
    """该反问却硬答：即使数字算对，也记 0。"""
    case = _case(expect=C.CLARIFY, must_mention=["分母"])
    out = AgentOutput(kind=C.ANSWER, sql="SELECT COUNT(*) FROM t", text="3")
    r = score_case(mem_db, case, out)
    assert r.score == 0.0 and not r.behavior_ok
    assert "一票否决" in r.detail


def test_partial_credit_without_caliber(mem_db):
    """数字对但没声明口径：只拿部分分。"""
    case = _case(caliber_required=["denominator"])
    out = AgentOutput(kind=C.ANSWER, sql="SELECT COUNT(*) FROM t", text="3")
    r = score_case(mem_db, case, out)
    assert 0 < r.score < 1.0
    assert not r.caliber_ok


def test_full_credit_with_caliber(mem_db):
    case = _case(caliber_required=["denominator"])
    out = AgentOutput(kind=C.ANSWER, sql="SELECT COUNT(*) FROM t",
                      caliber={"denominator": "全部行"})
    assert score_case(mem_db, case, out).score == 1.0


def test_wrong_result_gets_no_answer_points(mem_db):
    case = _case(caliber_required=["denominator"], caveat_required=True)
    out = AgentOutput(kind=C.ANSWER, sql="SELECT COUNT(*) FROM t WHERE v > 100",
                      caliber={"denominator": "x"}, caveat="有污染")
    r = score_case(mem_db, case, out)
    assert r.answer_ok is False
    assert r.caliber_ok and r.caveat_ok


def test_missing_caveat_is_penalized(mem_db):
    case = _case(caliber_required=["denominator"], caveat_required=True)
    out = AgentOutput(kind=C.ANSWER, sql="SELECT COUNT(*) FROM t",
                      caliber={"denominator": "x"})
    r = score_case(mem_db, case, out)
    assert not r.caveat_ok and r.score < 1.0


def test_refuse_without_reason_is_partial(synth_db):
    case = _case(expect=C.REFUSE, golden_sql=None, must_mention=["不可连接"])
    out = AgentOutput(kind=C.REFUSE, text="算不了")
    assert 0 < score_case(synth_db, case, out).score < 1.0


def test_refuse_with_reason_is_full(synth_db):
    case = _case(expect=C.REFUSE, golden_sql=None, must_mention=["不可连接"])
    out = AgentOutput(kind=C.REFUSE, text="两套编号体系不可连接，强行 join 会静默返回 0 行")
    assert score_case(synth_db, case, out).score == 1.0


def test_table_result_comparison(mem_db):
    """表格结果按行比对，不看 SQL 字符串。"""
    case = _case(golden_sql="SELECT g, SUM(v) FROM t GROUP BY g ORDER BY g")
    same = AgentOutput(kind=C.ANSWER, sql="SELECT g, SUM(v) AS s FROM t GROUP BY g ORDER BY g ASC")
    assert score_case(mem_db, case, same).score == 1.0
    diff = AgentOutput(kind=C.ANSWER, sql="SELECT g, COUNT(v) FROM t GROUP BY g ORDER BY g")
    assert score_case(mem_db, case, diff).score == 0.0


def test_float_tolerance(mem_db):
    case = _case(golden_sql="SELECT AVG(v) FROM t")
    out = AgentOutput(kind=C.ANSWER, sql="SELECT AVG(v) + 0.001 FROM t")
    assert score_case(mem_db, case, out).score == 1.0


def test_agent_self_reported_number_is_not_trusted(mem_db):
    """agent 嘴上说 3，SQL 却是错的 —— 以执行结果为准。"""
    case = _case()
    out = AgentOutput(kind=C.ANSWER, sql="SELECT COUNT(*) FROM t WHERE v > 99", text="答案是 3")
    assert score_case(mem_db, case, out).answer_ok is False


# ---------------- 4. 基线 ----------------

def _overall(synth_db, name: str) -> float:
    from caliber.runner import evaluate
    return evaluate(synth_db, C.CASES, get_agent(name)).overall


def test_rule_beats_naive(synth_db):
    """口径意识必须带来正收益。"""
    assert _overall(synth_db, "rule") > _overall(synth_db, "naive") + 0.15


def test_always_clarify_is_the_floor(synth_db):
    """永远反问拿不到分，证明评测集不是送分题。"""
    assert _overall(synth_db, "clarify") < 0.35


def test_rule_baseline_is_not_saturated(synth_db):
    """规则 agent 不能满分，否则评测集失去区分度。"""
    assert _overall(synth_db, "rule") < 0.95


def test_naive_gets_vetoed(synth_db):
    """硬答一定会踩到一票否决。"""
    from caliber.runner import evaluate
    rep = evaluate(synth_db, C.CASES, get_agent("naive"))
    assert rep.veto_count() > 0
