"""合成业务库生成器。

生成一个与真实业务仓同构、数值全合成、但**保留真实数据结构陷阱**的 SQLite 库。

设计原则：
1. 固定随机种子，任何机器生成结果完全一致（可复现）。
2. 只复刻「结构性质」，不复刻任何真实数字 —— 见 docs/00-方案-内部不公开.md 第三节。
3. 每一张表都至少埋一个口径陷阱，供评测集出题。

用法：
    python -m caliber.synth --out data/synth.db
"""

from __future__ import annotations

import argparse
import datetime as dt
import random
import sqlite3
from pathlib import Path

SEED = 42
START_DATE = dt.date(2026, 1, 5)
DAYS = 78
END_DATE = START_DATE + dt.timedelta(days=DAYS - 1)

N_USERS = 3000
ACTIVATE_RATE = 0.85          # 注册后完成激活的比例
AUTO_ACTIVATE_SHARE = 0.15    # 激活中「系统自动开启、用户未真实发起」的比例
PURCHASE_RATE = 0.013         # 真实激活用户的购买率
N_ORDERS_PAID = 300
DIRECT_PURCHASE_SHARE = 0.39  # 已付订单中「无激活记录、直接购买」的比例
N_ORDERS_CANCELED = 240
TIMEOUT_CANCEL_SHARE = 0.997  # 取消原因中「支付超时自动取消」的比例
N_DEVICE_ROWS = 2000
N_DEVICE_UNITS = 20           # 2000 行只覆盖 20 台设备 —— 截断快照陷阱
GAP_START_OFFSET = 8          # 每日汇总缺失区间起点（相对 START_DATE）
GAP_DAYS = 11                 # 每日汇总缺失天数 —— 趋势断口陷阱

TASKS = [
    ("task1", "引导任务一", 0.62, 0.08),
    ("task2", "引导任务二", 0.45, 0.04),
    ("task3", "引导任务三", 0.36, 0.03),
    ("task4", "记忆任务", 0.30, 0.02),
    ("task5", "动态任务", 0.15, 0.01),
    ("task6", "日记任务", 0.12, 0.01),
    ("task7", "提醒任务", 0.09, 0.005),
]

DDL = """
DROP TABLE IF EXISTS users;
CREATE TABLE users (
    user_id          TEXT PRIMARY KEY,
    device_sn        TEXT,
    reg_time         TEXT,
    activate_time    TEXT,
    freeze_end_time  TEXT,
    purchase_time    TEXT,
    task1_time       TEXT,
    task2_time       TEXT,
    task3_time       TEXT,
    task4_time       TEXT,
    task5_time       TEXT,
    task6_time       TEXT,
    task7_time       TEXT,
    auto_activated   INTEGER
);

DROP TABLE IF EXISTS orders_paid;
CREATE TABLE orders_paid (
    order_id         TEXT PRIMARY KEY,
    pay_sn           TEXT,
    pay_channel      TEXT,
    reg_time         TEXT,
    activate_time    TEXT,
    pay_time         TEXT,
    activated_before_pay INTEGER,
    conv_cycle_days  REAL
);

DROP TABLE IF EXISTS orders_canceled;
CREATE TABLE orders_canceled (
    order_id         TEXT PRIMARY KEY,
    user_key         TEXT,
    trial_activate_time TEXT,
    create_time      TEXT,
    status           TEXT,
    cancel_time      TEXT,
    cancel_reason    TEXT
);

DROP TABLE IF EXISTS task_completion;
CREATE TABLE task_completion (
    user_id          TEXT,
    device_sn        TEXT,
    task_code        TEXT,
    finish_time      TEXT
);

DROP TABLE IF EXISTS device_snapshot;
CREATE TABLE device_snapshot (
    device_sn        TEXT,
    robot_id         TEXT,
    conn_status      TEXT,
    report_time      TEXT,
    device_status    TEXT,
    network_mode     TEXT,
    battery_pct      INTEGER,
    life_battery_pct INTEGER,
    temperature      REAL,
    mood             TEXT,
    firmware_version TEXT,
    body_version     TEXT
);

DROP TABLE IF EXISTS dim_date;
CREATE TABLE dim_date (
    date             TEXT PRIMARY KEY,
    year             INTEGER,
    month            INTEGER,
    week             INTEGER,
    weekday          INTEGER
);

DROP TABLE IF EXISTS ads_daily_activation;
CREATE TABLE ads_daily_activation (
    date             TEXT PRIMARY KEY,
    activate_users   INTEGER
);

DROP TABLE IF EXISTS ads_official_daily;
CREATE TABLE ads_official_daily (
    date             TEXT PRIMARY KEY,
    activate_users   INTEGER,
    source           TEXT
);
"""


def _ts(base: dt.date, day_offset: int, rng: random.Random) -> str:
    """把日期偏移 + 随机时刻合成 'YYYY-MM-DD HH:MM:SS'。"""
    d = base + dt.timedelta(days=day_offset)
    t = dt.time(rng.randint(0, 23), rng.randint(0, 59), rng.randint(0, 59))
    return f"{d.isoformat()} {t.strftime('%H:%M:%S')}"


def _day_offset(rng: random.Random, low: int, high: int) -> int:
    return rng.randint(low, min(high, DAYS - 1))


def build_users(rng: random.Random) -> list[tuple]:
    rows = []
    for i in range(1, N_USERS + 1):
        uid = f"U{i:05d}"
        sn = f"SN-{rng.randint(100000, 999999)}"
        reg_off = _day_offset(rng, 0, DAYS - 1)
        reg_time = _ts(START_DATE, reg_off, rng)

        activated = rng.random() < ACTIVATE_RATE
        auto = 0
        activate_time = None
        freeze_end = None
        if activated:
            act_off = min(reg_off + rng.randint(0, 2), DAYS - 1)
            activate_time = _ts(START_DATE, act_off, rng)
            freeze_end = _ts(START_DATE, min(act_off + 7, DAYS - 1), rng)
            if rng.random() < AUTO_ACTIVATE_SHARE:
                auto = 1

        purchase_time = None
        if activated and not auto and rng.random() < PURCHASE_RATE:
            purchase_time = _ts(START_DATE, _day_offset(rng, act_off, DAYS - 1), rng)

        task_times = []
        for _, _, p_real, p_auto in TASKS:
            p = p_auto if auto else p_real
            if activated and rng.random() < p:
                task_times.append(_ts(START_DATE, _day_offset(rng, act_off, DAYS - 1), rng))
            else:
                task_times.append(None)

        rows.append(
            (uid, sn, reg_time, activate_time, freeze_end, purchase_time, *task_times, auto)
        )
    return rows


def build_orders_paid(rng: random.Random) -> list[tuple]:
    """付费订单表。陷阱：pay_sn 与 users.device_sn 是两套编号体系，交集恒为 0。"""
    rows = []
    for i in range(1, N_ORDERS_PAID + 1):
        oid = f"O{i:05d}"
        pay_sn = f"PX-{rng.randint(100000, 999999)}"   # 独立编号体系，不可与 users 连接
        channel = rng.choice(["wechat", "alipay", "app_store"])
        reg_off = _day_offset(rng, 0, DAYS - 1)
        reg_time = _ts(START_DATE, reg_off, rng)

        direct = rng.random() < DIRECT_PURCHASE_SHARE
        activate_time = None
        activated_before_pay = 0
        cycle = None
        if not direct:
            act_off = min(reg_off + rng.randint(0, 1), DAYS - 1)
            activate_time = _ts(START_DATE, act_off, rng)
            activated_before_pay = 1
            gap = rng.uniform(0.2, 25.0)
            cycle = round(gap, 2)
            pay_off = min(act_off + int(gap) + 1, DAYS - 1)
        else:
            pay_off = min(reg_off + rng.randint(0, 3), DAYS - 1)
        pay_time = _ts(START_DATE, pay_off, rng)

        rows.append((oid, pay_sn, channel, reg_time, activate_time, pay_time,
                     activated_before_pay, cycle))
    return rows


def build_orders_canceled(rng: random.Random) -> list[tuple]:
    """取消订单表。陷阱：取消原因几乎全部是支付超时机制，不是用户主动放弃。"""
    rows = []
    for i in range(1, N_ORDERS_CANCELED + 1):
        oid = f"C{i:05d}"
        ukey = f"K{i:05d}"
        trial_off = _day_offset(rng, 0, DAYS - 2)
        trial = _ts(START_DATE, trial_off, rng)
        create_off = min(trial_off + rng.randint(0, 1), DAYS - 1)
        create = _ts(START_DATE, create_off, rng)
        cancel = _ts(START_DATE, min(create_off + 1, DAYS - 1), rng)
        reason = "支付超时自动取消" if rng.random() < TIMEOUT_CANCEL_SHARE else "用户主动取消"
        rows.append((oid, ukey, trial, create, "已取消", cancel, reason))
    return rows


def build_task_completion(user_rows: list[tuple]) -> list[tuple]:
    rows = []
    for r in user_rows:
        uid, sn = r[0], r[1]
        for idx, (code, _, _, _) in enumerate(TASKS):
            finish = r[6 + idx]
            if finish:
                rows.append((uid, sn, code, finish))
    return rows


def build_device_snapshot(rng: random.Random) -> list[tuple]:
    """设备快照。陷阱：行数上限截断，只覆盖少量设备，不能外推总体。"""
    rows = []
    units = [f"SN-D{i:04d}" for i in range(1, N_DEVICE_UNITS + 1)]
    per_unit = N_DEVICE_ROWS // N_DEVICE_UNITS
    for sn in units:
        robot_id = f"R-{rng.randint(10000, 99999)}"
        for _ in range(per_unit):
            report = _ts(START_DATE, _day_offset(rng, 0, DAYS - 1), rng)
            rows.append((
                sn,
                robot_id,
                rng.choice(["online", "offline", "weak"]),
                report,
                rng.choice(["normal", "charging", "sleep", "error"]),
                rng.choice(["wifi", "4g", "ble"]),
                rng.randint(1, 100),
                rng.randint(1, 100),
                round(rng.uniform(30.0, 72.0), 1),
                rng.choice(["calm", "happy", "tired", "sad"]),
                f"v{rng.randint(1, 3)}.{rng.randint(0, 9)}.{rng.randint(0, 9)}",
                f"b{rng.randint(1, 4)}.{rng.randint(0, 9)}",
            ))
    return rows


def build_dim_date() -> list[tuple]:
    rows = []
    for k in range(DAYS):
        d = START_DATE + dt.timedelta(days=k)
        iso = d.isocalendar()
        rows.append((d.isoformat(), iso[0], d.month, iso[1], d.weekday() + 1))
    return rows


def build_daily_activation(user_rows: list[tuple]) -> tuple[list[tuple], list[tuple]]:
    """每日激活汇总。陷阱：本地汇总缺一整段日期；另有一份官方来源数字与之不一致。"""
    local: dict[str, int] = {}
    for r in user_rows:
        at = r[3]
        if at:
            local[at[:10]] = local.get(at[:10], 0) + 1

    gap = {(START_DATE + dt.timedelta(days=GAP_START_OFFSET + k)).isoformat()
           for k in range(GAP_DAYS)}

    local_rows = [(d, c) for d, c in sorted(local.items()) if d not in gap]
    official_rows = []
    for d, c in sorted(local.items()):
        official_rows.append((d, int(round(c * 1.008)) + 1, "official_export"))
    return local_rows, official_rows


def generate(out_path: Path) -> Path:
    rng = random.Random(SEED)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()

    con = sqlite3.connect(out_path)
    con.executescript(DDL)

    users = build_users(rng)
    paid = build_orders_paid(rng)
    canceled = build_orders_canceled(rng)
    tasks = build_task_completion(users)
    devices = build_device_snapshot(rng)
    dates = build_dim_date()
    daily, official = build_daily_activation(users)

    con.executemany("INSERT INTO users VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", users)
    con.executemany("INSERT INTO orders_paid VALUES (?,?,?,?,?,?,?,?)", paid)
    con.executemany("INSERT INTO orders_canceled VALUES (?,?,?,?,?,?,?)", canceled)
    con.executemany("INSERT INTO task_completion VALUES (?,?,?,?)", tasks)
    con.executemany("INSERT INTO device_snapshot VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", devices)
    con.executemany("INSERT INTO dim_date VALUES (?,?,?,?,?)", dates)
    con.executemany("INSERT INTO ads_daily_activation VALUES (?,?)", daily)
    con.executemany("INSERT INTO ads_official_daily VALUES (?,?,?)", official)
    con.commit()
    con.close()
    return out_path


def main() -> None:
    ap = argparse.ArgumentParser(description="生成合成业务库")
    ap.add_argument("--out", default="data/synth.db", help="输出 SQLite 路径")
    args = ap.parse_args()
    p = generate(Path(args.out))
    print(f"已生成合成库: {p}")


if __name__ == "__main__":
    main()
