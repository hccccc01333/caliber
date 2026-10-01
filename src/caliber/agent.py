"""Data Agent。

两种模式：
- `RuleAgent`（默认，offline）：规则驱动，不调 LLM，结果确定可复现。
  它的定位是**评测基线**：暴露"不靠模型能力、只靠工程规则"能挡住多少陷阱。
- `LLMAgent`（可选）：接 OpenAI 兼容接口，读环境变量，用于真实演示。

规则 agent 不是背答案：它按「查口径字典 → 查连接白名单 → 生成模板 SQL」的通用流程工作，
答不了的题（跨表不可连、字段不存在、口径未定义）会如实失败，评测分会显示它的上限。
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass

from .schema import JOIN_RULES, METRICS, TABLES
from .score import AgentOutput

CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7}

# 库里根本不存在的字段：问到就必须说没有，不能编
MISSING_FIELD_WORDS = {
    "客单价": "订单金额 / 单价",
    "金额": "订单金额",
    "价格": "商品价格",
    "收入": "收入金额",
    "gmv": "成交金额",
    "arpu": "人均收入",
    "毛利": "成本与毛利",
}

# 订单侧实体词 / 用户侧属性词：两者同时出现 = 要求跨表关联
ORDER_SIDE_WORDS = ["付费", "订单", "支付", "已购", "购买", "取消"]
USER_SIDE_WORDS = ["用户"]
USER_ATTR_WORDS = ["激活时间", "完成激活", "注册时间", "任务完成", "完成任务"]
BUY_AGAIN_WORDS = ["重新购买", "再次购买", "复购", "留存", "后续购买"]

VAGUE_TIME_WORDS = ["最近", "近", "上个月", "上月", "本周", "这周", "近期", "这段时间", "前几天"]
EXPLICIT_DAYS_RE = re.compile(r"(最近|近|过去)\s*(\d+)\s*天")

ACTIVATION_DENOM_WORDS = ["全部激活", "所有激活", "激活用户做分母", "按激活用户", "以激活用户",
                          "按全部激活用户"]
REAL_DENOM_WORDS = ["真实激活", "排除自动", "非系统自动", "不是系统自动", "去除自动", "剔除自动",
                    "非自动"]
CONV_ORDER_WORDS = ["订单口径", "按订单", "订单数"]
CONV_USER_WORDS = ["用户口径", "按用户", "用户数"]


def _task_col(question: str) -> str | None:
    m = re.search(r"任务\s*([1-7一二三四五六七])", question)
    if not m:
        return None
    tok = m.group(1)
    n = int(tok) if tok.isdigit() else CN_NUM[tok]
    return f"task{n}_time"


def _join_allowed(left: str, right: str) -> tuple[bool, str]:
    for r in JOIN_RULES:
        if {r["左"], r["右"]} == {left, right}:
            return r["可连接"], r.get("说明", "")
    return True, ""


@dataclass
class RuleAgent:
    """规则驱动的 baseline agent。

    strict=True  ：口径没定就反问，跨表不可连就拒答，字段没有就说没有。
    strict=False ：naive 对照基线 —— 不看口径硬答、不检查连接、忽略时间窗。
                   它代表「只有 SQL 能力、没有口径意识」的 data agent，
                   存在的意义是量化口径错误到底要付多少代价。
    """

    strict: bool = True

    def answer(self, question: str) -> AgentOutput:
        q = question.strip()

        # 1) 字段不存在：直接说没有
        for word, what in MISSING_FIELD_WORDS.items():
            if word in q.lower() or word in q:
                return AgentOutput(
                    kind="refuse",
                    text=f"算不了：库里没有{what}字段（orders_paid 只有渠道、时间、是否先激活后购买，"
                         f"没有任何金额列），我不会拿别的东西替你估一个数。",
                )

        # 2) 要求跨不可连接的表关联：明确拒绝，而不是 join 出空结果当 0 报
        if (any(w in q for w in ORDER_SIDE_WORDS) and any(w in q for w in USER_SIDE_WORDS)
                and (any(w in q for w in USER_ATTR_WORDS) or any(w in q for w in BUY_AGAIN_WORDS))):
            ok, why = _join_allowed("users", "orders_paid")
            if not ok and not self.strict:
                # 硬连：会静默返回 0 行，然后被当成「没有人」报出去
                return AgentOutput(
                    kind="answer",
                    sql="SELECT COUNT(*) FROM orders_paid p "
                        "JOIN users u ON p.pay_sn = u.device_sn",
                    text="已按订单表与用户表关联统计。",
                    caliber={"source": "orders_paid JOIN users"},
                )
            if not ok:
                return AgentOutput(
                    kind="refuse",
                    text=f"算不了：订单侧编号与用户表是两套独立体系，两者不可连接（{why}）。"
                         f"强行关联会静默返回 0 行，那不是「没有人」，是「连不上」，"
                         f"我不会把一个空结果当成结论报给你。",
                )

        # 3) 时间范围不明确
        if not self.strict and any(w in q for w in VAGUE_TIME_WORDS):
            return AgentOutput(
                kind="answer", sql="SELECT COUNT(*) FROM users WHERE activate_time IS NOT NULL",
                text="按全窗口统计（未限定时间范围）。", caliber={"time_window": "全窗口"},
            )
        if any(w in q for w in VAGUE_TIME_WORDS) and not EXPLICIT_DAYS_RE.search(q):
            return AgentOutput(
                kind="clarify",
                text="这个我没法直接算：「最近 / 上个月」没有定义。请给我明确时间窗口"
                     "（比如 2026-01-05 到 2026-02-05，或最近 30 天）。"
                     "另外本库数据本身有固定窗口，超出窗口的区间取不到数。",
            )

        # 4) 完成率：口径必须由用户指定
        if "完成率" in q:
            col = _task_col(q) or "task1_time"
            metric = METRICS["task_completion_rate"]
            if any(w in q for w in REAL_DENOM_WORDS):
                denom = "real_activation"
                where = "activate_time IS NOT NULL AND auto_activated = 0"
                decl = "真实激活用户（排除 auto_activated=1）"
            elif any(w in q for w in ACTIVATION_DENOM_WORDS) or not self.strict:
                denom = "activation"
                where = "activate_time IS NOT NULL"
                decl = "全部激活用户（含系统自动开启）"
            else:
                return AgentOutput(
                    kind="clarify",
                    text="完成率的分母先确认一下：是「全部激活用户」，还是「排除系统自动开启后的"
                         "真实激活用户」？两者差约 8 个百分点——分母里混进系统自动开启、"
                         "用户从未真实发起的样本，会把完成率系统性低估。你说一个，我再算。",
                )
            sql = (f"SELECT ROUND(100.0 * SUM(CASE WHEN {col} IS NOT NULL THEN 1 ELSE 0 END) "
                   f"/ COUNT(*), 2) FROM users WHERE {where}")
            return AgentOutput(
                kind="answer", sql=sql,
                text=f"按{decl}做分母计算。",
                caliber={"denominator": decl},
                caveat=metric["陷阱"],
            )

        # 5) 付费转化率：三种口径都合法
        if "转化率" in q or ("付费" in q and "比例" in q):
            metric = METRICS["paid_conversion_rate"]
            if any(w in q for w in CONV_ORDER_WORDS):
                sql = ("SELECT ROUND(100.0 * (SELECT COUNT(*) FROM orders_paid) "
                       "/ (SELECT COUNT(*) FROM users), 2)")
                decl = "订单口径：已付订单数 ÷ 注册用户数"
            elif any(w in q for w in CONV_USER_WORDS):
                sql = ("SELECT ROUND(100.0 * (SELECT COUNT(*) FROM users "
                       "WHERE purchase_time IS NOT NULL) / (SELECT COUNT(*) FROM users "
                       "WHERE activate_time IS NOT NULL), 2)")
                decl = "用户口径：purchase_time 非空用户数 ÷ 激活用户数"
            elif not self.strict:
                sql = ("SELECT ROUND(100.0 * (SELECT COUNT(*) FROM orders_paid) "
                       "/ (SELECT COUNT(*) FROM users), 2)")
                decl = "订单口径：已付订单数 ÷ 注册用户数"
            else:
                return AgentOutput(
                    kind="clarify",
                    text="转化率先定两个口径：分子按订单数还是付费用户数？分母按注册用户还是激活用户？"
                         "另外有一批订单没有激活记录（独立直购路径），是否计入也要你定。"
                         "这几种口径差别不小，我不替你挑。",
                )
            return AgentOutput(kind="answer", sql=sql, text=f"按{decl}计算。",
                               caliber={"denominator": decl}, caveat=metric["陷阱"])

        # 6) 支付失败占比
        if "支付失败" in q or ("失败" in q and "支付" in q):
            metric = METRICS["payment_failure_rate"]
            sql = ("SELECT ROUND(100.0 * (SELECT COUNT(*) FROM orders_canceled) "
                   "/ ((SELECT COUNT(*) FROM orders_canceled) "
                   "+ (SELECT COUNT(*) FROM orders_paid)), 2)")
            decl = "订单级：取消订单数 ÷ (取消订单数 + 已付订单数)"
            return AgentOutput(kind="answer", sql=sql, text=f"按{decl}计算。",
                               caliber={"denominator": decl}, caveat=metric["陷阱"])

        # 7) 设备温度 / 健康
        if "温度" in q or "设备" in q and ("健康" in q or "异常" in q):
            metric = METRICS["device_health_rate"]
            m = re.search(r"(\d+(?:\.\d+)?)\s*度", q)
            thr = float(m.group(1)) if m else 60.0
            sql = (f"SELECT ROUND(100.0 * SUM(CASE WHEN temperature > {thr} THEN 1 ELSE 0 END) "
                   f"/ COUNT(*), 2) FROM device_snapshot")
            decl = "快照口径：以 device_snapshot 覆盖到的上报为分母"
            return AgentOutput(kind="answer", sql=sql, text=f"按{decl}计算（阈值 {thr} 度）。",
                               caliber={"denominator": decl}, caveat=metric["陷阱"])

        # 8) 设备台数（必须去重）
        if "多少台" in q or ("设备" in q and "覆盖" in q):
            sql = "SELECT COUNT(DISTINCT device_sn) FROM device_snapshot"
            decl = "快照覆盖的去重设备数（count(distinct device_sn)，不是上报行数）"
            return AgentOutput(kind="answer", sql=sql, text=f"按{decl}计算。",
                               caliber={"denominator": decl},
                               caveat=TABLES["device_snapshot"]["警告"][0])

        # 9) 每日趋势 / 汇总覆盖天数
        if "每日" in q or "趋势" in q or "断口" in q:
            metric = METRICS["daily_activation"]
            sql = ("SELECT COUNT(*) AS covered_days, MIN(date) AS d_min, MAX(date) AS d_max "
                   "FROM ads_daily_activation")
            return AgentOutput(
                kind="answer", sql=sql, text="按本地汇总口径统计覆盖天数。",
                caliber={"source": "ads_daily_activation（本地口径）"},
                caveat=(f"{TABLES['ads_daily_activation']['警告'][0]}；"
                        f"{metric['陷阱']}"),
            )

        # 10) 各任务完成人数（走长表）
        if "每个" in q and "任务" in q and "完成" in q:
            sql = ("SELECT task_code, COUNT(DISTINCT user_id) FROM task_completion "
                   "GROUP BY task_code ORDER BY task_code")
            return AgentOutput(kind="answer", sql=sql, text="按 task_completion 长表分组统计。",
                               caliber={"source": "task_completion（宽表转长表）"})

        # 10b) 提到任务但没说哪一个、也没说怎么算完成
        if "任务" in q and any(w in q for w in ["整体", "总体", "情况", "怎么样", "如何"]) \
                and _task_col(q) is None:
            return AgentOutput(
                kind="clarify",
                text="「任务完成情况」我没法直接算：库里有七个引导任务，"
                     "你要的是哪个任务（引导任务一到七中的哪一个）？"
                     "「完成」是指这一个任务完成，还是七个任务全部完成？"
                     "另外分母用全部激活用户还是真实激活用户也要先定。",
            )

        # 11) 支付渠道分布
        if "渠道" in q:
            sql = ("SELECT pay_channel, COUNT(*) FROM orders_paid "
                   "GROUP BY pay_channel ORDER BY pay_channel")
            return AgentOutput(kind="answer", sql=sql, text="按 pay_channel 分组统计。",
                               caliber={"source": "orders_paid"})

        # 12) 取消订单数
        if "取消" in q and "订单" in q:
            sql = "SELECT COUNT(*) FROM orders_canceled"
            return AgentOutput(kind="answer", sql=sql, text="按订单号计数。",
                               caliber={"source": "orders_canceled"})

        # 13) 已付订单数
        if ("订单" in q or "支付" in q or "付费" in q) and ("数" in q or "多少" in q or "几笔" in q):
            sql = "SELECT COUNT(*) FROM orders_paid"
            return AgentOutput(kind="answer", sql=sql, text="按订单号计数（订单号全局唯一）。",
                               caliber={"source": "orders_paid"})

        # 14) 激活用户数（是否排除自动激活）
        if "激活" in q:
            if any(w in q for w in REAL_DENOM_WORDS):
                sql = ("SELECT COUNT(*) FROM users WHERE activate_time IS NOT NULL "
                       "AND auto_activated = 0")
                decl = "真实激活用户（排除 auto_activated=1）"
            else:
                sql = "SELECT COUNT(*) FROM users WHERE activate_time IS NOT NULL"
                decl = "activate_time 非空即激活（含系统自动开启）"
            return AgentOutput(kind="answer", sql=sql, text=f"按{decl}计数。",
                               caliber={"denominator": decl})

        # 15) 用户总数
        if "用户" in q and ("多少" in q or "总数" in q or "一共" in q):
            sql = "SELECT COUNT(*) FROM users"
            return AgentOutput(kind="answer", sql=sql, text="按 user_id 计数。",
                               caliber={"denominator": "全部注册用户"})

        return AgentOutput(
            kind="clarify",
            text="我没能把这个问法对上口径字典里的指标，不想硬猜。换一种问法，"
                 "或者告诉我你要的是哪个指标、分母怎么定。",
        )


class AlwaysClarifyAgent:
    """下界基线：对任何问题都反问。

    存在的意义是证明这套评测集**不是「反问就能拿分」**——
    如果该答的题也反问，会被行为项一票否决。
    """

    def answer(self, question: str) -> AgentOutput:
        return AgentOutput(kind="clarify", text="请问您具体想看哪个口径？")


def get_agent(name: str = "rule"):
    """agent 工厂：naive / rule / clarify。"""
    if name == "naive":
        return RuleAgent(strict=False)
    if name == "clarify":
        return AlwaysClarifyAgent()
    if name == "rule":
        return RuleAgent(strict=True)
    raise ValueError(f"未知 agent: {name}")


BASELINES = ["naive", "rule", "clarify"]


class LLMAgent:
    """OpenAI 兼容接口的 agent。缺 env 时不可用，评测会自动跳过。"""

    def __init__(self, model: str | None = None, base_url: str | None = None) -> None:
        self.model = model or os.getenv("CALIBER_MODEL", "gpt-4o-mini")
        self.base_url = base_url or os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
        self.api_key = os.getenv("OPENAI_API_KEY")

    def available(self) -> bool:
        return bool(self.api_key)

    def answer(self, question: str) -> AgentOutput:
        raise NotImplementedError("LLM 模式留到评测基线跑通之后再接")


def run_agent(question: str, con: sqlite3.Connection | None = None) -> AgentOutput:
    """统一入口：默认规则 agent。"""
    _ = con
    return RuleAgent().answer(question)
