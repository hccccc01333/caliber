"""表结构描述 + 口径字典。

口径字典是整个项目的核心资产：它把「一个指标有哪些合法口径」显式登记下来，
Data Agent 必须从中挑一个并声明，而不是默默挑一个。

对外（LLM / 文档）用 render_schema_text() 与 render_metric_text()。
"""

from __future__ import annotations

TABLES: dict[str, dict] = {
    "users": {
        "用途": "用户主宽表：注册、激活、冷冻期、购买与 7 个引导任务的完成时间",
        "粒度": "一行一个用户",
        "列": {
            "user_id": "用户唯一标识",
            "device_sn": "设备序列号，与设备快照表可连接",
            "reg_time": "注册时间",
            "activate_time": "激活成功时间，未激活为空",
            "freeze_end_time": "冷冻期结束时间",
            "purchase_time": "购买时间，未购买为空",
            "task1_time": "引导任务一完成时间",
            "task2_time": "引导任务二完成时间",
            "task3_time": "引导任务三完成时间",
            "task4_time": "记忆任务完成时间",
            "task5_time": "动态任务完成时间",
            "task6_time": "日记任务完成时间",
            "task7_time": "提醒任务完成时间",
            "auto_activated": "1=系统自动开启激活，用户未真实发起；0=用户真实发起",
        },
        "警告": [
            "activate_time 非空 ≠ 用户真实发起：auto_activated=1 的样本由系统自动开启，"
            "做完成率分母时会虚增分母、系统性低估完成率",
            "任务完成时间只在 activate_time 非空的行上才有意义",
        ],
    },
    "orders_paid": {
        "用途": "已支付订单",
        "粒度": "一行一笔订单",
        "列": {
            "order_id": "订单号，全局唯一",
            "pay_sn": "订单侧的支付主体编号",
            "pay_channel": "支付渠道",
            "reg_time": "注册时间",
            "activate_time": "激活时间，直购路径为空",
            "pay_time": "支付成功时间",
            "activated_before_pay": "1=支付前已完成激活",
            "conv_cycle_days": "激活到支付的转化周期（天），直购为空",
        },
        "警告": [
            "pay_sn 与 users.device_sn 是两套独立编号体系，两表不可直接连接，"
            "强行 join 会得到空结果而不是报错 —— 空结果不能被当成「0 个用户」上报",
            "activate_time 为空的订单属于独立直购路径，是否计入转化率分母必须声明",
        ],
    },
    "orders_canceled": {
        "用途": "未支付/已取消订单",
        "粒度": "一行一笔订单",
        "列": {
            "order_id": "订单号",
            "user_key": "订单侧用户键，与 users 不可连接",
            "trial_activate_time": "试用激活时间",
            "create_time": "订单创建时间",
            "status": "订单状态",
            "cancel_time": "取消时间",
            "cancel_reason": "取消原因",
        },
        "警告": [
            "cancel_reason 绝大多数是「支付超时自动取消」，属于机制触发而非用户主动放弃，"
            "归因时不能解释为「用户意愿不足」",
        ],
    },
    "task_completion": {
        "用途": "任务完成事实表（宽表转长表）",
        "粒度": "一行一次任务完成",
        "列": {
            "user_id": "用户标识，与 users 可连接",
            "device_sn": "设备序列号",
            "task_code": "任务编码 task1..task7",
            "finish_time": "完成时间",
        },
        "警告": [],
    },
    "device_snapshot": {
        "用途": "设备上报快照",
        "粒度": "一行一次上报（同一设备多次上报）",
        "列": {
            "device_sn": "设备序列号，与 users 可连接",
            "robot_id": "机器人标识",
            "conn_status": "连接状态",
            "report_time": "上报时间",
            "device_status": "设备状态",
            "network_mode": "网络模式",
            "battery_pct": "机身电量",
            "life_battery_pct": "生命电量",
            "temperature": "温度",
            "mood": "情绪",
            "firmware_version": "固件版本",
            "body_version": "身体版本",
        },
        "警告": [
            "本表是按行数上限截断的导出，只覆盖少量设备的多次上报，属于抽样快照，"
            "不能外推到全部设备总体",
            "统计设备数必须用 count(distinct device_sn)，不能用 count(*)",
        ],
    },
    "dim_date": {
        "用途": "日期维度",
        "粒度": "一行一天",
        "列": {
            "date": "日期",
            "year": "年",
            "month": "月",
            "week": "ISO 周序",
            "weekday": "星期几（1=周一）",
        },
        "警告": [],
    },
    "ads_daily_activation": {
        "用途": "每日激活人数汇总（本地口径）",
        "粒度": "一行一天",
        "列": {"date": "日期", "activate_users": "当日激活人数"},
        "警告": [
            "本表缺一整段日期（导出缺口），画趋势时必须显式标注缺口区间，不能当成「那些天没有激活」",
        ],
    },
    "ads_official_daily": {
        "用途": "每日激活人数汇总（官方导出口径）",
        "粒度": "一行一天",
        "列": {"date": "日期", "activate_users": "当日激活人数", "source": "来源标记"},
        "警告": [
            "与 ads_daily_activation 存在系统性差异（导出时窗与字段完整性不同），"
            "引用数字时必须声明用的是哪一份来源",
        ],
    },
}

# 可连接规则：不在白名单里的连接一律禁止，避免静默空结果
JOIN_RULES: list[dict] = [
    {"左": "users", "右": "task_completion", "键": "user_id", "可连接": True},
    {"左": "users", "右": "device_snapshot", "键": "device_sn", "可连接": True},
    {"左": "users", "右": "orders_paid", "键": "device_sn = pay_sn", "可连接": False,
     "说明": "两套独立编号体系，交集为空，强行 join 静默返回 0 行"},
    {"左": "users", "右": "orders_canceled", "键": "user_id = user_key", "可连接": False,
     "说明": "订单侧用户键与用户表无对应关系"},
]

METRICS: dict[str, dict] = {
    "task_completion_rate": {
        "名称": "引导任务完成率",
        "口径": {
            "activation": "分母 = activate_time 非空的用户（含系统自动激活）",
            "real_activation": "分母 = activate_time 非空且 auto_activated=0 的用户",
        },
        "默认口径": None,
        "必须反问": True,
        "陷阱": "按 activation 口径算会把完成率低估约 8 个百分点："
                "分母里混进了系统自动开启、用户从未真实发起的样本",
    },
    "paid_conversion_rate": {
        "名称": "付费转化率",
        "口径": {
            "order": "订单口径：已付订单数 ÷ 注册用户数",
            "user": "用户口径：purchase_time 非空用户数 ÷ 激活用户数",
            "activation_only": "仅计先激活后购买：orders_paid 中 activated_before_pay=1 的订单 ÷ 激活用户数",
        },
        "默认口径": None,
        "必须反问": True,
        "陷阱": "订单表里有一批无激活记录的直购订单，是否计入分子/分母会显著改变结论；"
                "订单数、付费用户数、付费设备数三者不能互换",
    },
    "payment_failure_rate": {
        "名称": "支付失败占比",
        "口径": {
            "order": "订单级：取消订单数 ÷ (取消订单数 + 已付订单数)",
        },
        "默认口径": "order",
        "必须反问": False,
        "陷阱": "取消原因绝大多数是支付超时机制触发，结论应指向支付流程摩擦，"
                "不能归因成用户购买意愿不足",
    },
    "device_health_rate": {
        "名称": "设备健康率",
        "口径": {
            "snapshot": "快照口径：以 device_snapshot 覆盖到的设备为分母",
            "total": "总体口径：以全部已激活设备为分母",
        },
        "默认口径": "snapshot",
        "必须反问": False,
        "陷阱": "快照表是按行数截断的抽样，只覆盖少量设备，用 total 口径外推总体不成立",
    },
    "daily_activation": {
        "名称": "每日激活人数",
        "口径": {
            "local": "本地宽表聚合（ads_daily_activation）",
            "official": "官方导出（ads_official_daily）",
        },
        "默认口径": "local",
        "必须反问": False,
        "陷阱": "本地汇总缺一整段日期；两份来源数字不一致，引用时必须声明来源",
    },
}


def render_schema_text() -> str:
    """渲染给 LLM 的表结构说明（含警告）。"""
    out = []
    for name, meta in TABLES.items():
        cols = "，".join(f"{k}（{v}）" for k, v in meta["列"].items())
        out.append(f"表 {name}｜{meta['用途']}｜粒度：{meta['粒度']}｜列：{cols}")
        for w in meta["警告"]:
            out.append(f"  ! {w}")
    out.append("连接规则：")
    for r in JOIN_RULES:
        flag = "可连接" if r["可连接"] else "禁止连接"
        extra = f"（{r['说明']}）" if not r["可连接"] else ""
        out.append(f"  - {r['左']} × {r['右']} on {r['键']}：{flag}{extra}")
    return "\n".join(out)


def render_metric_text() -> str:
    """渲染口径字典。"""
    out = ["口径字典（同一指标有多个合法口径时必须声明选用了哪个）："]
    for key, m in METRICS.items():
        out.append(f"- {m['名称']}（{key}）")
        for vk, vv in m["口径"].items():
            out.append(f"    · {vk}：{vv}")
        if m["默认口径"] is None:
            out.append("    ! 无默认口径，被问到必须先反问分母")
        else:
            out.append(f"    · 默认口径：{m['默认口径']}")
        out.append(f"    ! 陷阱：{m['陷阱']}")
    return "\n".join(out)


def known_columns(table: str) -> set[str]:
    return set(TABLES.get(table, {}).get("列", {}))


def all_tables() -> list[str]:
    return list(TABLES)
