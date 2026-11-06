# -*- coding: utf-8 -*-
"""
造一份「12 个月医院运营数据」，并故意注入 4 个已知异常 + 1 个假异常。

为什么要造数据：原 HIS 只有 180 条挂号、1 个月跨度，分到「科室×周」后平均每格 3 条，
统计噪声会盖过真实信号，归因出来的「下降」全是随机波动。

造数据的价值在于「已知答案」：我们明确知道哪个月哪个科室因为什么原因掉量，
才能反过来检验 Agent 是否真的归因到了正确原因，而不是编一个听起来合理的理由。
"""
import random
import sys
from datetime import date, timedelta

import pymysql

SEED = 20261106
random.seed(SEED)          # 固定随机流，保证任何人重跑得到同一份数据

DB = dict(host="127.0.0.1", user="root", password="his_dev_only",
          charset="utf8mb4", database="hospital_ops")

START = date(2025, 11, 1)
END = date(2026, 10, 31)

# 科室：(名称, 医生数, 基础挂号率权重)
DEPTS = [
    ("内科", 4, 1.30),
    ("外科", 3, 1.00),
    ("儿科", 3, 1.10),
    ("妇产科", 2, 0.85),
    ("骨科", 3, 0.95),
    ("眼科", 2, 0.70),
    ("耳鼻喉科", 2, 0.65),
    ("皮肤科", 2, 0.75),
    ("口腔科", 2, 0.80),
    ("中医科", 2, 0.90),
    ("神经内科", 2, 0.70),
    ("心内科", 3, 1.05),
]

SURNAMES = "王李张刘陈杨黄赵吴周徐孙马朱胡郭何高林罗"
GIVEN = "伟芳娜敏静秀丽强磊洋勇艳杰娟涛明超霞平刚桂英"

# ---------------------------------------------------------------- 注入的异常
# 每一项都是一个「已知真相」，后面用来给 Agent 的对错打分
INJECTED = [
    {
        "id": "A",
        "month": "2026-07",
        "dept": "内科",
        "kind": "医生离职",
        "detail": "内科 2 名医生于 7 月离职（状态改为离职、7 月起不再排班），"
                  "在岗医生从 4 人降到 2 人，号源减少约一半",
        "is_real_problem": True,
    },
    {
        "id": "B",
        "month": "2026-08",
        "dept": "儿科",
        "kind": "退号率异常",
        "detail": "儿科 8 月退号率从常态 3% 升到 25%，挂号总量正常但实际就诊量大跌，"
                  "根因是挂号系统重复放号后批量取消",
        "is_real_problem": True,
    },
    {
        "id": "C",
        "month": "2026-02",
        "dept": None,          # 全院
        "kind": "春节假期",
        "detail": "2026 年 2 月春节，全院门诊量下降约 40%，属于季节性正常波动，不是问题",
        "is_real_problem": False,   # 假异常：用来测 Agent 会不会误报
    },
    {
        "id": "D",
        "month": "2026-09",
        "dept": "骨科",
        "kind": "停诊未撤号",
        "detail": "骨科 1 名医生 9 月停诊 10 天，但排班仍按正常放号，"
                  "导致号源放出去无人接诊（实际挂号数远低于放号数）",
        "is_real_problem": True,
    },
]


def month_of(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def build_schema(cur):
    cur.execute("DROP TABLE IF EXISTS registration")
    cur.execute("DROP TABLE IF EXISTS doctor_schedule")
    cur.execute("DROP TABLE IF EXISTS doctor")
    cur.execute("DROP TABLE IF EXISTS department")

    # 字段名与 HIS 保持一致，便于说明「接的是同一套业务模型」
    cur.execute("""
    CREATE TABLE department (
        dept_id   INT PRIMARY KEY,
        dept_name VARCHAR(50) NOT NULL,
        status    VARCHAR(4)  NOT NULL DEFAULT '启用'
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    cur.execute("""
    CREATE TABLE doctor (
        doctor_id   INT PRIMARY KEY,
        doctor_name VARCHAR(30) NOT NULL,
        dept_id     INT NOT NULL,
        title       VARCHAR(20),
        status      VARCHAR(4) NOT NULL DEFAULT '在职',
        leave_month VARCHAR(7) NULL COMMENT '离职生效月份，用于归因',
        KEY idx_dept (dept_id),
        CONSTRAINT fk_d_dept FOREIGN KEY (dept_id) REFERENCES department(dept_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    cur.execute("""
    CREATE TABLE doctor_schedule (
        schedule_id  INT PRIMARY KEY AUTO_INCREMENT,
        doctor_id    INT NOT NULL,
        work_date    DATE NOT NULL,
        time_period  VARCHAR(4) NOT NULL,
        total_quota  INT NOT NULL DEFAULT 30,
        status       VARCHAR(4) NOT NULL DEFAULT '正常',
        KEY idx_doc_date (doctor_id, work_date),
        KEY idx_date (work_date),
        CONSTRAINT fk_s_doc FOREIGN KEY (doctor_id) REFERENCES doctor(doctor_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    cur.execute("""
    CREATE TABLE registration (
        reg_id     INT PRIMARY KEY AUTO_INCREMENT,
        doctor_id  INT NOT NULL,
        dept_id    INT NOT NULL,
        reg_date   DATE NOT NULL,
        time_period VARCHAR(4) NOT NULL,
        reg_fee    DECIMAL(8,2) NOT NULL DEFAULT 0.00,
        reg_status VARCHAR(10) NOT NULL DEFAULT '已就诊',
        KEY idx_date (reg_date),
        KEY idx_dept_date (dept_id, reg_date),
        CONSTRAINT fk_r_doc  FOREIGN KEY (doctor_id) REFERENCES doctor(doctor_id),
        CONSTRAINT fk_r_dept FOREIGN KEY (dept_id)   REFERENCES department(dept_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)


def build_base(cur):
    dept_rows, doctor_rows = [], []
    did = 0
    dept_doctors = {}          # dept_id -> [doctor_id]
    for i, (name, n_doc, _w) in enumerate(DEPTS, start=1):
        dept_rows.append((i, name, "启用"))
        ids = []
        for _ in range(n_doc):
            did += 1
            dname = random.choice(SURNAMES) + random.choice(GIVEN)
            title = random.choice(["主任医师", "副主任医师", "主治医师", "住院医师"])
            doctor_rows.append((did, dname, i, title, "在职", None))
            ids.append(did)
        dept_doctors[i] = ids

    cur.executemany("INSERT INTO department VALUES (%s,%s,%s)", dept_rows)
    cur.executemany(
        "INSERT INTO doctor (doctor_id,doctor_name,dept_id,title,status,leave_month) "
        "VALUES (%s,%s,%s,%s,%s,%s)", doctor_rows)
    return {name: i for i, (name, _n, _w) in enumerate(DEPTS, start=1)}, dept_doctors


def main():
    conn = pymysql.connect(**DB)
    conn.autocommit = False
    cur = conn.cursor()
    build_schema(cur)
    dept_id, dept_doctors = build_base(cur)

    # ---- 注入异常 A：内科 2 名医生 2026-07 起离职
    internal = sorted(dept_doctors[dept_id["内科"]])
    leave_idx = internal[-2:]
    cur.executemany(
        "UPDATE doctor SET status='离职', leave_month='2026-07' WHERE doctor_id=%s",
        [(d,) for d in leave_idx])
    retired = set(leave_idx)

    # ---- 注入异常 D：骨科 1 名医生 2026-09 停诊 10 天
    ortho = sorted(dept_doctors[dept_id["骨科"]])
    stopped_doc = ortho[0]
    stop_start = date(2026, 9, 7)
    stop_days = {stop_start + timedelta(days=k) for k in range(10)}

    weight = {dept_id[n]: w for n, _x, w in DEPTS}
    sched_rows, reg_rows = [], []
    d = START
    while d <= END:
        m = month_of(d)
        is_weekend = d.weekday() >= 5
        # 春节：2026-02-15 ~ 2026-02-21 门诊大幅收缩
        spring = date(2026, 2, 15) <= d <= date(2026, 2, 21)

        for dname, n_doc, w in DEPTS:
            did_ = dept_id[dname]
            in_spring = spring
            for doctor in dept_doctors[did_]:
                if doctor in retired and m >= "2026-07":
                    continue                      # 离职后不再排班
                if is_weekend:
                    continue
                # 「停诊未撤号」的关键：停诊期间排班照样挂着，号照样放，只是没人接诊。
                # 所以这几天必须保证有排班，不能被下面的随机跳过。
                is_stopped = (doctor == stopped_doc and d in stop_days)
                if not is_stopped and random.random() < 0.25:
                    continue
                if in_spring and not is_stopped and random.random() < 0.75:
                    continue                      # 春节大幅减少出诊

                period = "上午" if random.random() < 0.6 else "下午"
                quota = 30
                sched_rows.append((doctor, d, period, quota, "正常"))

                # ---- 该时段实际挂号数
                base_rate = w * (0.62 if period == "上午" else 0.45)
                booked = int(quota * base_rate * random.uniform(0.85, 1.15))
                if in_spring:
                    booked = int(booked * 0.35)
                booked = max(2, min(quota, booked))

                # ---- 异常 D：停诊未撤号，实际只有零星挂号
                if doctor == stopped_doc and d in stop_days:
                    booked = random.randint(0, 2)

                # ---- 异常 B：儿科 8 月退号率 25%，其余月约 3%
                cancel_rate = 0.03
                if dname == "儿科" and m == "2026-08":
                    cancel_rate = 0.25

                fee = 20.00
                for _ in range(booked):
                    r = random.random()
                    if r < cancel_rate:
                        status = "已取消"
                    elif r < cancel_rate + 0.05:
                        status = "已退费"
                    elif r < 0.55:
                        status = "已发药"
                    elif r < 0.80:
                        status = "已收费"
                    else:
                        status = "已就诊"
                    reg_rows.append((doctor, did_, d, period, fee, status))
        d += timedelta(days=1)

    # 批量插入，避免单条往返
    for i in range(0, len(sched_rows), 2000):
        cur.executemany(
            "INSERT INTO doctor_schedule (doctor_id,work_date,time_period,total_quota,status) "
            "VALUES (%s,%s,%s,%s,%s)", sched_rows[i:i + 2000])
    for i in range(0, len(reg_rows), 3000):
        cur.executemany(
            "INSERT INTO registration (doctor_id,dept_id,reg_date,time_period,reg_fee,reg_status) "
            "VALUES (%s,%s,%s,%s,%s,%s)", reg_rows[i:i + 3000])

    conn.commit()

    cur.execute("SELECT COUNT(*) FROM registration")
    n_reg = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM doctor_schedule")
    n_sched = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM doctor")
    n_doc = cur.fetchone()[0]
    print(f"科室 {len(DEPTS)} · 医生 {n_doc} · 排班 {n_sched:,} · 挂号 {n_reg:,}")
    print(f"区间 {START} ~ {END}（固定 seed={SEED}，重跑结果一致）")
    conn.close()

    print("\n注入的异常（Agent 不知道这些，用来打分）：")
    for x in INJECTED:
        flag = "真问题" if x["is_real_problem"] else "假异常(不该报)"
        print(f"  [{x['id']}] {x['month']} {x['dept'] or '全院'} · {x['kind']} · {flag}")


if __name__ == "__main__":
    main()
