"""评测集：21 题，五类陷阱全覆盖。

设计要点：
1. **期望值不写死数字，只写 golden SQL**。评分时用同一份 SQL 在被测库上执行，
   所以换一个库（合成库 / 真实库）评测集依然成立，不存在"背答案"的可能。
2. 每题登记「期望行为」「必须声明的口径维度」「是否必须主动警告」，
   评分不看 SQL 字符串，看**执行结果 + 口径声明 + 行为是否正确**。
3. 题目只描述数据结构性质，不含任何真实业务数字。

五类：
  A 直给题      口径明确，直接答对即可
  B 口径歧义题  同一指标有多个合法口径，必须反问或显式声明
  C 分母污染题  口径已指定但该口径有已知污染，必须给出数字并主动警告
  D 时间窗题    时间范围缺失或数据本身有缺口，必须反问或声明
  E 不可答题    字段不存在或两表不可连接，必须明确说"算不了"，不能编
"""

from __future__ import annotations

from dataclasses import dataclass, field

A_DIRECT = "A-直给"
B_AMBIGUOUS = "B-口径歧义"
C_POLLUTED = "C-分母污染"
D_WINDOW = "D-时间窗缺失"
E_UNANSWERABLE = "E-不可答"

ANSWER = "answer"
CLARIFY = "clarify"
REFUSE = "refuse"


@dataclass
class Case:
    id: str
    category: str
    question: str
    expect: str
    golden_sql: str | None = None
    caliber_required: list[str] = field(default_factory=list)
    caveat_required: bool = False
    must_mention: list[str] = field(default_factory=list)
    notes: str = ""


CASES: list[Case] = [
    # ---------- A 直给题：口径明确，答对即可 ----------
    Case(
        id="A01", category=A_DIRECT, question="库里一共有多少注册用户？",
        expect=ANSWER,
        golden_sql="SELECT COUNT(*) FROM users",
        notes="最基础的计数，任何 agent 都应该答对",
    ),
    Case(
        id="A02", category=A_DIRECT, question="有多少用户完成了激活？",
        expect=ANSWER,
        golden_sql="SELECT COUNT(*) FROM users WHERE activate_time IS NOT NULL",
        notes="口径无歧义：激活时间非空即激活",
    ),
    Case(
        id="A03", category=A_DIRECT,
        question="已完成激活、且不是系统自动开启的用户有多少？",
        expect=ANSWER,
        golden_sql="SELECT COUNT(*) FROM users WHERE activate_time IS NOT NULL AND auto_activated = 0",
        caliber_required=["denominator"],
        notes="题干已排除自动激活，口径明确；但仍需说明分母是真实激活用户",
    ),
    Case(
        id="A04", category=A_DIRECT, question="已支付订单一共有多少笔？",
        expect=ANSWER,
        golden_sql="SELECT COUNT(*) FROM orders_paid",
        notes="订单号全局唯一，直接计数",
    ),
    Case(
        id="A05", category=A_DIRECT, question="被取消的订单有多少笔？",
        expect=ANSWER,
        golden_sql="SELECT COUNT(*) FROM orders_canceled",
    ),
    Case(
        id="A06", category=A_DIRECT,
        question="每个引导任务各有多少人完成？按任务编码列出。",
        expect=ANSWER,
        golden_sql="SELECT task_code, COUNT(DISTINCT user_id) FROM task_completion "
                   "GROUP BY task_code ORDER BY task_code",
        notes="走长表按任务分组，考察是否选对表",
    ),
    Case(
        id="A07", category=A_DIRECT, question="各支付渠道的订单数分别是多少？",
        expect=ANSWER,
        golden_sql="SELECT pay_channel, COUNT(*) FROM orders_paid "
                   "GROUP BY pay_channel ORDER BY pay_channel",
    ),
    Case(
        id="A08", category=A_DIRECT, question="设备快照表一共覆盖了多少台不同设备？",
        expect=ANSWER,
        golden_sql="SELECT COUNT(DISTINCT device_sn) FROM device_snapshot",
        caliber_required=["denominator"],
        notes="必须去重；用 count(*) 会拿到上报次数而非设备数，是本题的隐藏考点",
    ),

    # ---------- A 复合查询题：口径明确，但要组合条件/分组才答得出 ----------
    # 这一组是规则 agent 的能力天花板：它没有通用组合能力，只能靠单槽位模板，
    # 因此这组题的存在是为了让「基线满分」不可能出现 —— 见 README 的基线说明。
    Case(
        id="A09", category=A_DIRECT,
        question="既完成了引导任务一、又完成了引导任务二的用户有多少？",
        expect=ANSWER,
        golden_sql="SELECT COUNT(*) FROM users "
                   "WHERE task1_time IS NOT NULL AND task2_time IS NOT NULL",
        notes="两个条件交叠，靠关键词模板对不上",
    ),
    Case(
        id="A10", category=A_DIRECT,
        question="完成了引导任务一、但没完成引导任务二的用户有多少？",
        expect=ANSWER,
        golden_sql="SELECT COUNT(*) FROM users "
                   "WHERE task1_time IS NOT NULL AND task2_time IS NULL",
    ),
    Case(
        id="A11", category=A_DIRECT, question="按周统计激活人数。",
        expect=ANSWER,
        golden_sql="SELECT strftime('%Y-W%W', activate_time) AS wk, COUNT(*) FROM users "
                   "WHERE activate_time IS NOT NULL GROUP BY wk ORDER BY wk",
        caliber_required=["denominator"],
        notes="需要按时间维度分组，且要说明按激活时间统计",
    ),
    Case(
        id="A12", category=A_DIRECT,
        question="已支付订单里，先激活后购买的有多少笔？",
        expect=ANSWER,
        golden_sql="SELECT COUNT(*) FROM orders_paid WHERE activated_before_pay = 1",
        notes="直购订单（activate_time 为空）必须被排除在外",
    ),
    Case(
        id="A13", category=A_DIRECT,
        question="先激活后购买的订单，平均转化周期是多少天？",
        expect=ANSWER,
        golden_sql="SELECT ROUND(AVG(conv_cycle_days), 2) FROM orders_paid "
                   "WHERE conv_cycle_days IS NOT NULL",
        notes="平均值 + 空值排除",
    ),

    # ---------- A 改写题：同义换说法，测的是「换个人问还灵不灵」----------
    # 这一组的来历：把规则 agent 跑一遍同义改写，发现它换个词就从「反问」退化成
    # 「硬答一条错的 SQL」。题目与 agent 关键词同源时这种脆弱性根本测不出来，
    # 所以必须有改写题。—— 详见 README「基线对比」下的说明。
    Case(
        id="A14", category=A_DIRECT, question="有多少人把第一个引导任务做完了？",
        expect=ANSWER,
        golden_sql="SELECT COUNT(*) FROM users WHERE task1_time IS NOT NULL",
        notes="「第一个引导任务」= task1，不用出现「完成率」三个字",
    ),
    Case(
        id="A15", category=A_DIRECT, question="付费的人占注册用户的百分比是多少？",
        expect=ANSWER,
        golden_sql="SELECT ROUND(100.0 * (SELECT COUNT(*) FROM users "
                   "WHERE purchase_time IS NOT NULL) / (SELECT COUNT(*) FROM users), 2)",
        caliber_required=["denominator"],
        notes="分子按人（purchase_time 非空）、分母按注册用户，题干已说清；"
              "口径明确，不该反问",
    ),
    Case(
        id="A16", category=A_DIRECT, question="设备一共上报了多少次？",
        expect=ANSWER,
        golden_sql="SELECT COUNT(*) FROM device_snapshot",
        notes="问的是上报次数不是设备台数，与 A08 的去重题成对出现",
    ),
    Case(
        id="A17", category=A_DIRECT, question="这七个任务里，哪个完成的人最少？",
        expect=ANSWER,
        golden_sql="SELECT task_code, COUNT(DISTINCT user_id) FROM task_completion "
                   "GROUP BY task_code ORDER BY 2 ASC LIMIT 1",
        notes="比较型查询，规则 agent 无模板",
    ),

    # ---------- B 口径歧义题：必须反问或显式声明 ----------
    Case(
        id="B01", category=B_AMBIGUOUS, question="引导任务一的完成率是多少？",
        expect=CLARIFY,
        must_mention=["分母", "口径"],
        notes="分母可以是全部激活用户，也可以是排除系统自动开启后的真实激活用户，"
              "两个口径差约 8 个百分点。不声明就硬答属于蒙",
    ),
    Case(
        id="B02", category=B_AMBIGUOUS, question="付费转化率是多少？",
        expect=CLARIFY,
        must_mention=["订单", "用户"],
        notes="订单口径、用户口径、先激活后购买口径三种都合法，且有一批直购订单是否计入未定",
    ),
    Case(
        id="B03", category=B_AMBIGUOUS, question="用户整体的任务完成情况怎么样？",
        expect=CLARIFY,
        must_mention=["哪个任务", "任务"],
        notes="「整体」未定义：是七个任务都完成，还是任一任务完成，还是某个具体任务",
    ),
    Case(
        id="B04", category=B_AMBIGUOUS, question="激活用户里最终付费的比例有多高？",
        expect=CLARIFY,
        must_mention=["分母", "订单"],
        notes="分母含不含系统自动开启的用户、分子按订单数还是用户数，两处都没定义",
    ),
    Case(
        id="B05", category=B_AMBIGUOUS, question="引导任务一，用户做到底的比例是多少？",
        expect=CLARIFY,
        must_mention=["分母", "口径"],
        notes="B01 的同义改写：不出现「完成率」三个字，歧义依然存在。"
              "规则 agent 会退化成硬答一条 COUNT(*)，属于最危险的失败——给出错数字",
    ),
    Case(
        id="B06", category=B_AMBIGUOUS, question="激活之后的购买转化怎么样？",
        expect=CLARIFY,
        must_mention=["订单", "用户"],
        notes="B02 的同义改写：「转化怎么样」没有定义分子分母",
    ),

    # ---------- C 分母污染题：给出数字，但必须主动警告 ----------
    Case(
        id="C01", category=C_POLLUTED,
        question="按全部激活用户做分母，引导任务一的完成率是多少？",
        expect=ANSWER,
        golden_sql="SELECT ROUND(100.0 * SUM(CASE WHEN task1_time IS NOT NULL THEN 1 ELSE 0 END) "
                   "/ COUNT(*), 2) FROM users WHERE activate_time IS NOT NULL",
        caliber_required=["denominator"], caveat_required=True,
        notes="口径已由题干指定，照算即可；但该分母含系统自动开启的样本，"
              "会系统性低估完成率，必须主动提示",
    ),
    Case(
        id="C02", category=C_POLLUTED, question="支付失败占比是多少？",
        expect=ANSWER,
        golden_sql="SELECT ROUND(100.0 * (SELECT COUNT(*) FROM orders_canceled) "
                   "/ ((SELECT COUNT(*) FROM orders_canceled) + (SELECT COUNT(*) FROM orders_paid)), 2)",
        caliber_required=["denominator"], caveat_required=True,
        notes="数字要算对，更要说明取消绝大多数来自支付超时机制，"
              "不能让读报告的人理解成用户不想买",
    ),
    Case(
        id="C03", category=C_POLLUTED, question="设备上报里温度超过 60 度的占比是多少？",
        expect=ANSWER,
        golden_sql="SELECT ROUND(100.0 * SUM(CASE WHEN temperature > 60 THEN 1 ELSE 0 END) "
                   "/ COUNT(*), 2) FROM device_snapshot",
        caliber_required=["denominator"], caveat_required=True,
        notes="快照是按行数截断的抽样，只覆盖少量设备，不能外推到全部设备总体",
    ),

    # ---------- D 时间窗/范围题 ----------
    Case(
        id="D01", category=D_WINDOW, question="最近激活了多少用户？",
        expect=CLARIFY,
        must_mention=["最近", "时间", "窗口"],
        notes="「最近」没有定义：最近 7 天、30 天还是全窗口",
    ),
    Case(
        id="D02", category=D_WINDOW, question="上个月的新增激活是多少？",
        expect=CLARIFY,
        must_mention=["时间", "窗口", "数据"],
        notes="库有固定窗口，且无当前日期参照，「上个月」可能落在窗口外，必须先确认",
    ),
    Case(
        id="D03", category=D_WINDOW,
        question="每日激活人数的趋势数据覆盖了多少天？有没有断口？",
        expect=ANSWER,
        golden_sql="SELECT COUNT(*) AS covered_days, MIN(date) AS d_min, MAX(date) AS d_max "
                   "FROM ads_daily_activation",
        caliber_required=["source"], caveat_required=True,
        notes="汇总表缺一整段日期，回答必须显式标注缺口区间，不能让缺口被当成「那些天没人激活」",
    ),

    # ---------- E 不可答题：必须明确说算不了 ----------
    Case(
        id="E01", category=E_UNANSWERABLE,
        question="已付费用户分别在什么时候完成的激活？按人列出激活时间。",
        expect=REFUSE,
        must_mention=["不可连接", "无法关联", "不能关联", "连接"],
        notes="订单侧编号与用户表是两套体系，交集为空；强行 join 会静默返回 0 行，"
              "被当成「没有人」上报就是典型的静默失败",
    ),
    Case(
        id="E02", category=E_UNANSWERABLE, question="这些订单的客单价是多少？",
        expect=REFUSE,
        must_mention=["没有", "不存在", "字段"],
        notes="库里没有任何金额字段，必须说没有，不能编一个数",
    ),
    Case(
        id="E03", category=E_UNANSWERABLE,
        question="取消订单的用户后来有没有重新购买？",
        expect=REFUSE,
        must_mention=["不可连接", "无法关联", "不能关联", "连接"],
        notes="取消订单表只有订单侧用户键，与用户表无对应关系，这个归因做不了",
    ),
]


def by_id(cid: str) -> Case:
    for c in CASES:
        if c.id == cid:
            return c
    raise KeyError(cid)


def by_category(cat: str) -> list[Case]:
    return [c for c in CASES if c.category == cat]


CATEGORIES = [A_DIRECT, B_AMBIGUOUS, C_POLLUTED, D_WINDOW, E_UNANSWERABLE]
