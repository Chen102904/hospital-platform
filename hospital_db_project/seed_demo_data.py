# -*- coding: utf-8 -*-
"""
生成并导入医院信息管理系统的演示数据
运行：python seed_demo_data.py
作用：1) 生成 04_seed_demo_data.sql（可提交到仓库）
      2) 直接连 MySQL 把数据导进去（会先清空原有数据）
"""

import os
import sys
import random
from collections import defaultdict, namedtuple
from datetime import datetime, timedelta

from werkzeug.security import generate_password_hash

# 复用 hospital_web/config.py 里的数据库配置，避免密码写两份
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "hospital_web"))
from config import DB_CONFIG  # noqa: E402
import pymysql  # noqa: E402

random.seed(20261014)
NOW = datetime.now()
TODAY = NOW.date()
DAY_MINUTES = 24 * 60      # pick_time 抽签用的固定范围（见其说明）


def esc(v):
    """转义成 SQL 字符串字面量"""
    return "'" + str(v).replace("\\", "\\\\").replace("'", "''") + "'"


def pick_time(day, lo_hour=8, hi_hour=16, margin_min=3):
    """在指定日期上取一个工作时间内的时间点，并保证早于"此刻"。

    今天是特殊的一天：只能取 08:00 ~ (此刻 - 3 分钟) 之间的时间，
    否则页面上就会出现"今天 16:15 挂号"而此刻才 13:00 这种未来数据。

    注意：抽签用的"范围"必须是固定的常量。范围一旦随执行时刻变化，
    底层 getrandbits 消耗的随机位数也跟着变，后面整批数据都会错位——
    这就是"换个时间跑结果全变样"的根因。所以这里固定抽 [0, DAY_MINUTES)，
    再按比例映射到真正可用的 [0, span]。
    """
    if day.date() == TODAY:
        start = day.replace(hour=lo_hour, minute=0, second=0, microsecond=0)
        span = int((NOW - start).total_seconds() // 60) - margin_min
        pick = random.randrange(DAY_MINUTES)     # 先抽签：任何分支都消耗同样多的随机数
        if span <= 0:                            # 此刻还没到 lo_hour，直接往前推一小段
            return (NOW - timedelta(minutes=30 + margin_min)).replace(
                second=0, microsecond=0)
        return (start + timedelta(minutes=pick * (span + 1) // DAY_MINUTES)).replace(
            second=0, microsecond=0)
    return day.replace(hour=random.randint(lo_hour, hi_hour),
                       minute=random.choice([0, 15, 30, 45]),
                       second=0, microsecond=0)


# ---------------- 1. 科室（12 个） ----------------
DEPTS = [
    ("内科", "门诊楼2层东区", "0572-6011001"),
    ("外科", "门诊楼2层西区", "0572-6011002"),
    ("儿科", "门诊楼3层东区", "0572-6011003"),
    ("妇产科", "门诊楼3层西区", "0572-6011004"),
    ("骨科", "门诊楼4层东区", "0572-6011005"),
    ("眼科", "门诊楼4层西区", "0572-6011006"),
    ("口腔科", "门诊楼5层东区", "0572-6011007"),
    ("皮肤科", "门诊楼5层西区", "0572-6011008"),
    ("神经内科", "住院部1层", "0572-6011009"),
    ("心血管内科", "住院部2层", "0572-6011010"),
    ("耳鼻喉科", "门诊楼6层", "0572-6011011"),
    ("急诊科", "急诊楼1层", "0572-6011012"),
]

# 演示"停用"样本：基础数据一律改状态停用、不做物理删除（历史挂号/病历还引用着它们）。
# 留一个停用的科室，页面上就能直接看出"停用项不再出现在新增下拉里"。
OFF_DEPT_IDX = {5}          # 眼科（索引 5 → dept_id 6）

# ---------------- 2. 医生（30 名） ----------------
SURNAMES = list("张王李赵刘陈杨黄周吴徐孙马朱胡林郭何高罗郑梁谢宋唐许韩冯邓曹彭曾")
GIVEN_M = ["建国", "志强", "明", "伟", "磊", "勇", "杰", "涛", "超", "刚",
           "建华", "文", "俊", "峰", "海", "军", "鹏", "斌", "浩", "东"]
GIVEN_F = ["芳", "敏", "静", "丽", "艳", "娟", "秀英", "霞", "平", "桂英",
           "玉兰", "雪", "梅", "红", "燕", "婷", "颖", "洁", "萍", "玲"]
SPECIALTY = {
    "内科": "高血压、糖尿病等慢性病诊治",
    "外科": "普外科常见手术与术后管理",
    "儿科": "小儿呼吸道感染、发热",
    "妇产科": "孕期保健、妇科炎症",
    "骨科": "骨折复位、关节损伤",
    "眼科": "白内障、近视矫正",
    "口腔科": "龋齿治疗、牙周病",
    "皮肤科": "湿疹、皮炎、真菌感染",
    "神经内科": "头痛、眩晕、脑血管病",
    "心血管内科": "冠心病、心律失常",
    "耳鼻喉科": "鼻炎、扁桃体炎",
    "急诊科": "急危重症抢救",
}
TITLE_ORDER = ["主任医师"] * 4 + ["副主任医师"] * 8 + ["主治医师"] * 12 + ["住院医师"] * 6
TITLE_FEE = {"主任医师": 30.00, "副主任医师": 20.00, "主治医师": 15.00, "住院医师": 10.00}

used_names = set()


def make_name(gender):
    pool = GIVEN_M if gender == "男" else GIVEN_F
    for _ in range(500):
        n = random.choice(SURNAMES) + random.choice(pool)
        if n not in used_names:
            used_names.add(n)
            return n
    raise RuntimeError("取名失败")


DEPTS_NEED = [d[0] for d in DEPTS]
doctors = []          # (name, gender, title, dept_index, specialty, phone)
dept_slots = []
for i, d in enumerate(DEPTS):
    dept_slots += [i, i]          # 每个科室至少 2 名
random.shuffle(dept_slots)
dept_slots = dept_slots[:30]
while len(dept_slots) < 30:
    dept_slots.append(random.randrange(12))

random.shuffle(TITLE_ORDER)
for idx, dept_idx in enumerate(dept_slots):
    gender = "男" if random.random() < 0.6 else "女"
    name = make_name(gender)
    title = TITLE_ORDER[idx]
    dept_name = DEPTS[dept_idx][0]
    doctors.append((name, gender, title, dept_idx, SPECIALTY[dept_name],
                    "139" + str(random.randint(10000000, 99999999))))

# 留两位离职医生：他们的历史挂号/病历/排班都保留（医生离职不该抹掉既往记录），
# 只是不再出现在"新增挂号 / 新增排班"的医生下拉里，也不再有新的排班。
LEAVING_DOCTOR_IDX = {len(doctors) - 1, len(doctors) - 2}

# ---------------- 3. 患者（60 名） ----------------
patients = []
for i in range(60):
    gender = "男" if random.random() < 0.5 else "女"
    name = make_name(gender)
    year = random.randint(1950, 2020)
    month = random.randint(1, 12)
    day = random.randint(1, 28)
    birth = "%04d-%02d-%02d" % (year, month, day)
    id_card = "330521%04d%02d%02d%04d" % (year, month, day, random.randint(1, 9999))
    phone = "138" + str(random.randint(10000000, 99999999))
    addr = "浙江省湖州市长兴县%s街道%d号" % (
        random.choice(["雉城", "太湖", "画溪", "龙山", "洪桥"]), random.randint(1, 300))
    patients.append((name, gender, birth, id_card, phone, addr))

# ---------------- 4. 药品（60 种，11 个分类；其中 6 种库存低于 100，会触发预警） ----------------
# 用 namedtuple 存，字段名一眼能看懂；用元组下标取值的话
# 加了"分类"这一列之后到处都要跟着改数字，很容易改漏。
Med = namedtuple("Med", "category name spec unit price stock mfr")

MEDS = [
    # —— 抗感染用药 ——
    Med("抗感染用药",   "阿莫西林胶囊",         "0.25g*24粒",   "盒", 18.50, 320, "华北制药"),
    Med("抗感染用药",   "头孢克肟分散片",       "0.1g*12片",    "盒", 36.80, 210, "扬子江药业"),
    Med("抗感染用药",   "注射用头孢曲松钠",     "1.0g",         "支", 12.30,  95, "罗氏制药"),
    Med("抗感染用药",   "注射用青霉素钠",       "80万单位",     "支",  3.60,  62, "华北制药"),
    Med("抗感染用药",   "左氧氟沙星片",         "0.5g*6片",     "盒", 31.20, 205, "第一三共"),
    Med("抗感染用药",   "阿奇霉素分散片",       "0.25g*6片",    "盒", 27.60, 175, "辉瑞制药"),
    Med("抗感染用药",   "甲硝唑片",             "0.2g*100片",   "瓶",  7.90, 340, "华中药业"),
    Med("抗感染用药",   "阿昔洛韦片",           "0.2g*24片",    "盒", 15.40, 150, "丽珠集团"),

    # —— 心血管系统用药 ——
    Med("心血管系统用药", "阿司匹林肠溶片",     "100mg*30片",   "盒", 15.80, 390, "拜耳医药"),
    Med("心血管系统用药", "硝苯地平控释片",     "30mg*7片",     "盒", 34.20, 180, "拜耳医药"),
    Med("心血管系统用药", "苯磺酸氨氯地平片",   "5mg*7片",      "盒", 28.50, 260, "国药集团"),
    Med("心血管系统用药", "氯沙坦钾片",         "50mg*7片",     "盒", 28.90, 150, "默沙东"),
    Med("心血管系统用药", "辛伐他汀片",         "20mg*10片",    "盒", 26.40, 220, "默沙东"),
    Med("心血管系统用药", "瑞舒伐他汀钙片",     "10mg*7片",     "盒", 38.90, 170, "阿斯利康"),
    Med("心血管系统用药", "硫酸氢氯吡格雷片",   "75mg*7片",     "盒", 45.60, 110, "赛诺菲"),
    Med("心血管系统用药", "华法林钠片",         "2.5mg*60片",   "盒", 32.50, 130, "齐鲁制药"),
    Med("心血管系统用药", "美托洛尔缓释片",     "47.5mg*7片",   "盒", 24.80, 195, "阿斯利康"),

    # —— 消化系统用药 ——
    Med("消化系统用药", "奥美拉唑肠溶胶囊",     "20mg*14粒",    "盒", 29.50, 260, "阿斯利康"),
    Med("消化系统用药", "铝碳酸镁咀嚼片",       "0.5g*20片",    "盒", 23.60, 185, "拜耳医药"),
    Med("消化系统用药", "多潘立酮片",           "10mg*30片",    "盒", 12.80, 240, "西安杨森"),
    Med("消化系统用药", "蒙脱石散",             "3g*10袋",      "盒", 19.80, 310, "博福益普生"),
    Med("消化系统用药", "复方甘草片",           "100片",        "瓶",  8.50,  88, "太极集团"),
    Med("消化系统用药", "双歧杆菌三联活菌胶囊", "0.21g*36粒",   "盒", 36.00, 145, "上海信谊"),

    # —— 呼吸系统用药 ——
    Med("呼吸系统用药", "氨溴索口服溶液",       "100ml",        "瓶", 21.50, 230, "勃林格殷格翰"),
    Med("呼吸系统用药", "孟鲁司特钠片",         "10mg*5片",     "盒", 42.30, 120, "默沙东"),
    Med("呼吸系统用药", "沙丁胺醇气雾剂",       "100μg*200揿",  "支", 32.40, 140, "葛兰素史克"),
    Med("呼吸系统用药", "复方甲氧那明胶囊",     "12粒*2板",     "盒", 26.90, 175, "第一三共"),
    Med("呼吸系统用药", "布地奈德福莫特罗吸入剂", "160μg*60揿", "支", 168.00, 65, "阿斯利康"),

    # —— 内分泌及代谢用药 ——
    Med("内分泌及代谢用药", "盐酸二甲双胍片",   "0.5g*20片",    "盒", 12.60, 430, "中美华东"),
    Med("内分泌及代谢用药", "阿卡波糖片",       "50mg*30片",    "盒", 46.20, 130, "拜耳医药"),
    Med("内分泌及代谢用药", "格列美脲片",       "2mg*15片",     "盒", 26.80, 165, "赛诺菲"),
    Med("内分泌及代谢用药", "左甲状腺素钠片",   "50μg*100片",   "盒", 34.50, 190, "默克雪兰诺"),
    Med("内分泌及代谢用药", "碳酸钙D3片",       "600mg*60片",   "瓶", 38.60, 280, "惠氏制药"),

    # —— 解热镇痛抗炎药 ——
    Med("解热镇痛抗炎药", "布洛芬缓释胶囊",     "0.3g*20粒",    "盒", 22.00, 480, "中美史克"),
    Med("解热镇痛抗炎药", "对乙酰氨基酚片",     "0.5g*20片",    "盒",  9.60, 520, "中美天津史克"),
    Med("解热镇痛抗炎药", "双氯芬酸钠缓释片",   "0.1g*12片",    "盒", 22.80, 300, "扬子江药业"),
    Med("解热镇痛抗炎药", "塞来昔布胶囊",       "0.2g*6粒",     "盒", 41.50, 110, "辉瑞制药"),

    # —— 神经系统用药 ——
    Med("神经系统用药", "甲钴胺片",             "0.5mg*20片",   "盒", 33.20, 155, "卫材药业"),
    Med("神经系统用药", "氟桂利嗪胶囊",         "5mg*20粒",     "盒", 19.40, 180, "西安杨森"),
    Med("神经系统用药", "多奈哌齐片",           "5mg*7片",      "盒", 52.80,  70, "卫材药业"),
    Med("神经系统用药", "阿普唑仑片",           "0.4mg*20片",   "盒", 11.50,  95, "江苏恩华"),

    # —— 维生素与矿物质 ——
    Med("维生素与矿物质", "维生素C片",          "100mg*100片",  "瓶",  6.80, 640, "东北制药"),
    Med("维生素与矿物质", "复合维生素B片",      "100片",        "瓶",  8.90, 420, "华中药业"),
    Med("维生素与矿物质", "叶酸片",             "0.4mg*31片",   "盒", 12.30, 350, "常州制药"),
    Med("维生素与矿物质", "琥珀酸亚铁片",       "0.1g*20片",    "盒", 24.60, 210, "金陵药业"),

    # —— 注射与输液用药 ——
    Med("注射与输液用药", "葡萄糖注射液",       "5%*500ml",     "瓶",  5.20, 720, "科伦药业"),
    Med("注射与输液用药", "氯化钠注射液",       "0.9%*500ml",   "瓶",  4.80, 690, "科伦药业"),
    Med("注射与输液用药", "乳酸钠林格注射液",   "500ml",        "瓶",  6.40, 320, "石四药"),
    Med("注射与输液用药", "地塞米松磷酸钠注射液", "5mg*1ml",    "支",  2.80,  45, "天津药业"),
    Med("注射与输液用药", "呋塞米片",           "20mg*100片",   "瓶",  9.60, 240, "上海信谊"),

    # —— 中成药 ——
    Med("中成药",       "连花清瘟胶囊",         "0.35g*24粒",   "盒", 25.00, 460, "以岭药业"),
    Med("中成药",       "感冒灵颗粒",           "10g*9袋",      "盒", 16.50, 520, "华润三九"),
    Med("中成药",       "藿香正气水",           "10ml*10支",    "盒", 13.60, 380, "太极集团"),
    Med("中成药",       "六味地黄丸",           "360丸",        "瓶", 22.40, 290, "北京同仁堂"),
    Med("中成药",       "复方丹参滴丸",         "27mg*180丸",   "瓶", 38.90, 240, "天士力"),
    Med("中成药",       "血塞通软胶囊",         "33mg*30粒",    "盒", 42.60, 135, "昆明制药"),

    # —— 皮肤及五官科用药 ——
    Med("皮肤及五官科用药", "莫匹罗星软膏",     "10g",          "支", 18.70, 220, "中美天津史克"),
    Med("皮肤及五官科用药", "丁酸氢化可的松乳膏", "10g",        "支", 15.90, 195, "天津药业"),
    Med("皮肤及五官科用药", "氯雷他定片",       "10mg*6片",     "盒", 17.20, 310, "上海先灵葆雅"),
    Med("皮肤及五官科用药", "氧氟沙星滴眼液",   "5ml",          "支",  8.60, 260, "沈阳兴齐"),
]

# 留两种停用（淘汰）药品：历史处方与收费快照照样查得到它们，只是开新处方时不再出现。
OFF_MED_IDX = {len(MEDS) - 2, len(MEDS) - 1}

MED_CATEGORIES = []
for _m in MEDS:                       # 保持"首次出现"的先后顺序去重
    if _m.category not in MED_CATEGORIES:
        MED_CATEGORIES.append(_m.category)

# ---------------- 4b. 员工与登录账号（5 个角色各 1 个） ----------------
# 密码只在数据库里存哈希，源码里保留明文是为了演示时能告诉你登录密码
STAFF = [
    # (登录账号, 明文密码, 姓名, 角色, 电话)
    ("admin", "admin123", "系统管理员", "管理员", "13900000001"),
    ("gh01",  "123456",   "王丽",       "挂号员", "13900000002"),
    ("ys01",  "123456",   "张建国",     "医生",   "13900000003"),
    ("yf01",  "123456",   "赵敏",       "药房",   "13900000004"),
    ("sf01",  "123456",   "陈晓",       "收费员", "13900000005"),
]
PAY_METHODS = ["现金", "微信", "支付宝", "银行卡"]
REFUND_REASONS = ["患者要求退费", "医生撤销处方", "重复收费冲正"]

# ---------------- 5. 挂号（初诊 150 条 + 复诊若干） ----------------
DIAGNOSIS = [
    ("上呼吸道感染", "多饮水、注意休息，一周后复诊"),
    ("急性支气管炎", "避免受凉，如有发热及时就诊"),
    ("高血压2级", "低盐低脂饮食，每日监测血压"),
    ("2型糖尿病", "控制饮食，定期复查糖化血红蛋白"),
    ("急性胃炎", "清淡饮食，忌辛辣刺激"),
    ("腰椎间盘突出", "避免久坐与负重，建议理疗"),
    ("过敏性皮炎", "避免接触过敏原，按时用药"),
    ("急性扁桃体炎", "多饮水，必要时输液治疗"),
    ("冠心病稳定型心绞痛", "随身携带硝酸甘油，避免剧烈运动"),
    ("偏头痛", "规律作息，减少熬夜"),
]
PRESC_TEXT = [
    "阿莫西林胶囊 0.5g 口服 每日3次；连花清瘟胶囊 4粒 口服 每日3次",
    "布洛芬缓释胶囊 0.3g 口服 每日2次；维生素C片 100mg 口服 每日3次",
    "硝苯地平控释片 30mg 口服 每日1次；阿司匹林肠溶片 100mg 口服 每日1次",
    "盐酸二甲双胍片 0.5g 口服 每日3次；瑞舒伐他汀钙片 10mg 口服 每晚1次",
    "奥美拉唑肠溶胶囊 20mg 口服 每日2次；复方甘草片 3片 口服 每日3次",
    "氯沙坦钾片 50mg 口服 每日1次；呋塞米片 20mg 口服 每日1次",
]
STATUS_POOL = ["已就诊"] * 70 + ["已挂号"] * 24 + ["已取消"] * 6

registrations = []   # (patient_idx, doctor_idx, dept_idx, reg_date, fee, status, visit_type, parent_idx)
for _ in range(150):
    d_idx = random.randrange(len(doctors))
    dept_idx = doctors[d_idx][3]
    title = doctors[d_idx][2]
    days_ago = random.choices(range(0, 30), weights=[6] + [4] * 6 + [3] * 23)[0]
    reg_date = pick_time(NOW - timedelta(days=days_ago))
    registrations.append((random.randrange(60), d_idx, dept_idx, reg_date,
                          TITLE_FEE[title], random.choice(STATUS_POOL), "初诊", None))

# 复诊：从已就诊的初诊里挑一批，时间往后推 3~20 天（且不能超过此刻）
revisit_pool = [i for i, r in enumerate(registrations) if r[5] == "已就诊"]
random.shuffle(revisit_pool)
revisit_count = 0
for i in revisit_pool:
    if revisit_count >= 30:
        break
    origin = registrations[i]
    new_day = origin[3] + timedelta(days=random.randint(3, 20))
    # 先按"天"粗筛，再用 pick_time 生成具体时刻（它会保证今天的时间早于此刻）
    if new_day.date() > TODAY:
        continue
    new_date = pick_time(new_day)
    d_idx = origin[1]
    registrations.append((origin[0], d_idx, origin[2], new_date,
                          TITLE_FEE[doctors[d_idx][2]],
                          random.choice(["已挂号", "已就诊", "已就诊"]),
                          "复诊", i))
    revisit_count += 1

# ---------------- 5b. 排班与号源（医生 × 日期 × 时段） ----------------
# 挂号要占号源，所以得先有排班：历史班次按实际挂号数反推，
# 今天起 3 天给全部医生按上下午放号，方便演示"提前预约"。
TIME_PERIODS = ["上午", "下午"]


def period_of(dt):
    return "上午" if dt.hour < 12 else "下午"


schedules = []      # dict：医生 / 日期 / 时段 / 号源 / 已挂 / 状态
sched_pos = {}      # (医生idx, 日期, 时段) -> 在 schedules 中的下标


def ensure_schedule(d_idx, day, period, quota=0):
    key = (d_idx, day, period)
    if key not in sched_pos:
        sched_pos[key] = len(schedules)
        schedules.append({"doctor_idx": d_idx, "day": day, "period": period,
                          "total_quota": quota, "booked": 0, "status": "正常"})
    return sched_pos[key]


# 1) 已有挂号落到对应班次上并累计已挂数（已取消的号已释放，不计入）
for r in registrations:
    si = ensure_schedule(r[1], r[3].date(), period_of(r[3]))
    if r[5] != "已取消":
        schedules[si]["booked"] += 1

# 2) 今天起 3 天，在职医生按上下午放号（已有班次跳过；离职医生不再放新号）
for _delta in range(0, 3):
    _day = TODAY + timedelta(days=_delta)
    for _d_idx in range(len(doctors)):
        for _period in TIME_PERIODS:
            # 先把号源数抽出来、再判断是否离职：抽签次数必须与"离职名单"无关，
            # 否则改一下名单，整个随机流错位、整批演示数据都会跟着变样。
            _quota = random.randint(20, 40)
            if _d_idx in LEAVING_DOCTOR_IDX:
                continue
            if (_d_idx, _day, _period) not in sched_pos:
                ensure_schedule(_d_idx, _day, _period, _quota)

# 3) 历史班次号源 = 已挂数 + 少量余量，保证"已挂 ≤ 号源"永远成立
for s in schedules:
    if s["total_quota"] == 0:
        s["total_quota"] = s["booked"] + random.randint(2, 12)
    assert 0 <= s["booked"] <= s["total_quota"], "号源不自洽：%s" % s

# 离职医生不能有"明天及以后"的排班——那些班次只可能由"提前放号"产生，
# 说明离职医生还在接新号。（"今天"允许保留：那是他最后一次出诊，由历史挂号反推出来的）
assert not [s for s in schedules
            if s["doctor_idx"] in LEAVING_DOCTOR_IDX and s["day"] > TODAY], \
    "离职医生不应有明天及以后的排班"

# 4) 候诊序号：同一位医生同一天，按挂号时间先后从 1 开始编号
_qseq = defaultdict(int)
queue_no = {}
for _idx in sorted(range(len(registrations)), key=lambda i: registrations[i][3]):
    _r = registrations[_idx]
    _key = (_r[1], _r[3].date())
    _qseq[_key] += 1
    queue_no[_idx] = _qseq[_key]

# 5) 把 时段 / 候诊序号 / 排班编号 挂到每条挂号上
for _idx, _r in enumerate(registrations):
    _day, _period = _r[3].date(), period_of(_r[3])
    registrations[_idx] = _r + (_period, queue_no[_idx],
                                sched_pos[(_r[1], _day, _period)] + 1)

# ---------------- 6. 病历 + 处方明细 ----------------
records = []   # (reg_idx, patient_idx, doctor_idx, visit_date, diagnosis, prescription, remark)
for reg_idx, r in enumerate(registrations):
    if r[5] != "已就诊":
        continue
    diag, remark = random.choice(DIAGNOSIS)
    records.append((reg_idx, r[0], r[1], r[3], diag, random.choice(PRESC_TEXT), remark))

details = []   # (record_idx, medicine_id, quantity, dosage)
for rec_idx in range(len(records)):
    for med_id in random.sample(range(1, len(MEDS) + 1), random.randint(1, 3)):
        details.append((rec_idx, med_id, random.randint(1, 3),
                        random.choice(["口服，每日3次", "口服，每日2次", "静脉滴注，每日1次",
                                       "口服，每晚1次", "外用，每日2次"])))

# 按病历把处方明细分组，后面收费和扣库存都要用
det_by_rec = defaultdict(list)
for d in details:
    det_by_rec[d[0]].append(d)

# ---------------- 7. 收费单 + 收费明细 ----------------
# 只有"已就诊"的挂号才可能去交钱：约 70% 交了费（状态变 已收费），
# 剩下 30% 留在"已就诊"当作"看完病还没去收费窗口"的真实场景。
# 再抽 4% 的收费单做退费（状态 已退费），用来演示反向流程。
MED_BY_ID = {i + 1: MEDS[i] for i in range(len(MEDS))}

charges = []          # dict 列表，字段见下
charge_details = []   # (charge_idx, item_type, medicine_id, item_name, unit_price, qty, amount)
refunded_med = defaultdict(int)   # 退药累计量（倒推期初库存要用）
dispenses = []        # dict 列表：药房发药记录

seq_by_day = defaultdict(int)
for rec_idx, rec in enumerate(records):
    reg_idx = rec[0]
    if random.random() > 0.70:        # 30% 留在"已就诊（待收费）"
        continue
    reg = registrations[reg_idx]
    # 所有"后面才用得上"的随机数都先抽好：一旦因为"时间还没到"而 continue，
    # 也不会少抽随机数。否则随机流错位，换个时刻再跑整批数据就全变样（不可复现）。
    _off_min = random.randint(8, 40)                  # 收费时刻相对就诊后的偏移
    _pay = random.choice(PAY_METHODS)                 # 支付方式
    _r_refund = random.random()                       # 是否退费
    _refund_off = random.randint(10, 150)             # 退费时刻偏移
    _refund_rsn = random.choice(REFUND_REASONS)       # 退费原因
    _disp_off = random.randint(5, 30)                 # 发药时刻偏移（已收费）
    _r_disp = random.random()                         # 是否发药（已收费）
    _r_disp_ref = random.random()                     # 是否发药（已退费）
    _disp_off_ref = random.randint(5, 20)             # 发药时刻偏移（已退费）

    charge_time = rec[3] + timedelta(minutes=_off_min)
    if charge_time > NOW:             # 刚看完还没走到收费窗口，今天的不补
        continue

    lines = []                        # (item_type, medicine_id, name, unit_price, qty, amount)
    lines.append(("挂号费", None, "门诊挂号费（%s）" % doctors[reg[1]][2], float(reg[4]), 1,
                  round(float(reg[4]) * 1, 2)))
    drug_fee = 0.0
    for _, med_id, qty, _ in det_by_rec.get(rec_idx, []):
        m = MED_BY_ID[med_id]
        amount = round(float(m.price) * qty, 2)
        drug_fee += amount
        lines.append(("药品费", med_id, m.name, float(m.price), qty, amount))
    drug_fee = round(drug_fee, 2)
    total = round(float(reg[4]) + drug_fee, 2)

    status = "已收费"
    refund_time = refund_reason = refund_operator = None
    if _r_refund < 0.04:
        rt = charge_time + timedelta(minutes=_refund_off)
        if rt <= NOW:                 # 退费时间也不能是未来
            status = "已退费"
            refund_time = rt
            refund_reason = _refund_rsn
            refund_operator = "系统管理员"

    key = charge_time.strftime("%Y%m%d")
    seq_by_day[key] += 1
    charge_no = "SF%s%04d" % (key, seq_by_day[key])

    ci = len(charges)
    charges.append({
        "charge_no": charge_no, "reg_idx": reg_idx, "patient_idx": reg[0],
        "reg_fee": round(float(reg[4]), 2), "drug_fee": drug_fee, "total_fee": total,
        "pay_method": _pay, "status": status,
        "charge_time": charge_time, "operator": "陈晓",
        "refund_time": refund_time, "refund_reason": refund_reason,
        "refund_operator": refund_operator,
    })
    for it in lines:
        charge_details.append((ci, it[0], it[1], it[2], it[3], it[4], it[5]))
        if status == "已退费" and it[0] == "药品费":
            refunded_med[it[1]] += it[4]

    # ---- 药房发药：收完费后药师核对处方发药，挂号状态推进到「已发药」----
    # 少数交完钱没来取药的留在「已收费」；已退费的若当初已发出，
    # 记一条「已退回」并填上退回时间，形成反向流程。
    dispensed = False
    dispense_time = None
    if status == "已收费":
        dt = charge_time + timedelta(minutes=_disp_off)
        if dt <= NOW and _r_disp < 0.85:
            dispensed, dispense_time = True, dt
    elif status == "已退费" and _r_disp_ref < 0.5:
        dt = charge_time + timedelta(minutes=_disp_off_ref)
        if dt <= refund_time:
            dispensed, dispense_time = True, dt
    if dispensed:
        dispenses.append({
            "reg_idx": reg_idx, "record_idx": rec_idx,
            "status": "已退回" if status == "已退费" else "已发药",
            "dispense_time": dispense_time,
            "return_time": refund_time if status == "已退费" else None,
        })

    # 挂号状态：退费 > 已发药 > 已收费
    reg_status = status if status == "已退费" else ("已发药" if dispensed else "已收费")
    registrations[reg_idx] = reg[:5] + (reg_status,) + reg[6:]

# ---------------- 8. 药品出入库流水（事件驱动的统一账本） ----------------
# 把所有会改变库存的动作（期初建账 / 处方扣减 / 退费退药）按时间排成一条事件流，
# 再一条条滚动算出"变动前 / 变动后"，这样流水表连续可验算，不会出现凭空对不上账的假数据。
deduct_total = {}
for _, med_id, qty, _ in details:
    deduct_total[med_id] = deduct_total.get(med_id, 0) + qty

# MEDS 里写的 stock 表示"当前库存"。
# 期末 = 期初 - 累计出库 + 累计退药，所以：期初 = 当前库存 + 累计出库 - 累计退药
init_stock = {i + 1: MEDS[i].stock + deduct_total.get(i + 1, 0) - refunded_med.get(i + 1, 0)
              for i in range(len(MEDS))}
_neg = {k: v for k, v in init_stock.items() if v < 0}
assert not _neg, "期初库存算成了负数，说明退药量超过了扣减量：%s" % _neg

OPEN_TIME = NOW - timedelta(days=45)
events = []   # (时间, 药品编号, 类型, 数量, 原因)
for med_id in range(1, len(MEDS) + 1):
    events.append((OPEN_TIME, med_id, "入库", init_stock[med_id], "期初建账入库"))

for rec_idx in sorted(range(len(records)), key=lambda i: records[i][3]):
    visit_date = records[rec_idx][3]
    for _, med_id, qty, _ in det_by_rec[rec_idx]:
        events.append((visit_date, med_id, "出库", qty,
                       "处方扣减（病历 #%d）" % (rec_idx + 1)))

for ci, c in enumerate(charges):
    if c["status"] != "已退费":
        continue
    for cd in charge_details:
        if cd[0] != ci or cd[1] != "药品费":
            continue
        events.append((c["refund_time"], cd[2], "入库", cd[5],
                       "退药入库（退费 %s）" % c["charge_no"]))

cur_stock = {}
med_logs = []   # (medicine_id, change_type, qty, before, after, reason, time)
for tm, med_id, ctype, qty, reason in sorted(
        events, key=lambda e: (e[0], 0 if e[2] == "入库" else 1)):
    before = cur_stock.get(med_id, 0)
    after = before + qty if ctype == "入库" else before - qty
    assert after >= 0, "库存被扣成负数：药品 %d 于 %s" % (med_id, tm)
    cur_stock[med_id] = after
    med_logs.append((med_id, ctype, qty, before, after, reason, tm))

# 自检：逐条滚完，库存必须和 MEDS 里声明的当前库存一致
for _mid in range(1, len(MEDS) + 1):
    assert cur_stock[_mid] == MEDS[_mid - 1].stock, "库存不自洽：药品 %d" % _mid

# ---------------- 8b. 住院登记（在院 / 已出院，事件留痕） ----------------
# 住院是一次"事件"而不是患者身上的开关：同一个患者可以住很多次，
# 每住一次留一条记录（入院 → 出院）。所以这里给每位患者排一条时间线，
# 依次生成若干次住院；当前在院的那些人，最后一次的出院时间是空的。
ADMIT_DIAGNOSIS = [
    "社区获得性肺炎", "急性阑尾炎", "2型糖尿病伴血糖控制不佳",
    "冠心病、不稳定型心绞痛", "急性脑梗死", "腰椎间盘突出症",
    "胆囊结石伴慢性胆囊炎", "慢性阻塞性肺疾病急性加重",
    "急性胃肠炎伴脱水", "高血压3级（很高危）", "股骨颈骨折", "过敏性紫癜",
]
DISCHARGE_SUMMARY = [
    "症状缓解，各项指标平稳，予出院，门诊随访",
    "复查血常规、生化基本正常，准予出院",
    "病情好转，伤口愈合良好，拆线后出院",
    "症状明显改善，嘱按时服药、定期复查",
]
ADMIT_OPERATORS = ["王丽", "系统管理员"]

admissions = []                    # dict 列表，字段见 build_admission
adm_seq_by_day = defaultdict(int)  # 住院号按入院日期编流水


def build_admission(p_idx, d_idx, admit_time, discharge_time, status):
    day_key = admit_time.strftime("%Y%m%d")
    adm_seq_by_day[day_key] += 1
    dept_idx = doctors[d_idx][3]
    return {
        "admission_no": "ZY%s%04d" % (day_key, adm_seq_by_day[day_key]),
        "patient_idx": p_idx, "dept_idx": dept_idx, "doctor_idx": d_idx,
        "bed_no": "%s-%02d" % (DEPTS[dept_idx][0], random.randint(1, 40)),
        "admit_diagnosis": random.choice(ADMIT_DIAGNOSIS),
        "admit_time": admit_time,
        "discharge_time": discharge_time,
        "status": status,
        "discharge_summary": random.choice(DISCHARGE_SUMMARY) if discharge_time else None,
        "admit_operator": random.choice(ADMIT_OPERATORS),
        "discharge_operator": (random.choice(["王丽", "张建国", "系统管理员"])
                               if discharge_time else None),
    }


ADMIT_POOL = random.sample(range(len(patients)), 20)   # 住过院的患者
IN_WARD = set(random.sample(ADMIT_POOL, 7))            # 其中这几位当前在院
for _p in ADMIT_POOL:
    _d = random.randrange(len(doctors))                # 这位患者的主治医生
    _in_ward = _p in IN_WARD
    _cursor = NOW - timedelta(days=random.randint(55, 85))   # 时间线起点
    # 在院的患者要留出最后一段，所以历史住院最多 2 次
    _times = random.randint(1, 3) if not _in_ward else random.randint(0, 2)
    # 出院必须早于"此刻"；在院的还要给最后一次留空档，避免两次时间重叠
    _hard_end = NOW - timedelta(days=10 if _in_ward else 1)
    # 1) 历史住院（已出院），一位患者最多 3 次
    for _k in range(_times):
        _los = random.randint(3, 15)                   # 这次住了几天
        _window = (_hard_end - _cursor).days - _los - 1
        if _window <= 0:                               # 时间线上排不下了
            break
        _admit = pick_time(_cursor + timedelta(days=random.randint(1, _window)))
        _discharge = _admit + timedelta(days=_los, hours=random.randint(2, 9))
        if _discharge > _hard_end:
            continue
        admissions.append(build_admission(_p, _d, _admit, _discharge, "已出院"))
        _cursor = min(_discharge + timedelta(days=random.randint(15, 45)),
                      NOW - timedelta(days=9))         # 不能顶到"在院"那一次前面
    # 2) 当前在院的：入院时间落在最近 1~8 天，出院时间留空
    if _in_ward:
        _admit = pick_time(NOW - timedelta(days=random.randint(1, 8)), 8, 17)
        assert _admit > _cursor, "住院时间线重叠：患者 %d" % (_p + 1)
        admissions.append(build_admission(_p, _d, _admit, None, "在院"))

# 自检 1：同一患者最多一条"在院"记录（和数据库里的生成列唯一键是同一条规则）
_inward_pids = [a["patient_idx"] for a in admissions if a["status"] == "在院"]
assert len(_inward_pids) == len(set(_inward_pids)), "同一患者出现了多条在院记录"
# 自检 2：出院必须晚于入院；在院的出院时间必须是空的
for _a in admissions:
    if _a["status"] == "在院":
        assert _a["discharge_time"] is None, "在院记录不该有出院时间"
    else:
        assert _a["discharge_time"] >= _a["admit_time"], \
            "出院早于入院：%s" % _a["admission_no"]

# ---------------- 9. 操作日志（演示用，最近一周） ----------------
OP_ACTIONS = [
    ("登录", "系统", "账号密码登录成功"),
    ("新增挂号", "挂号", "为现场患者建档并挂号"),
    ("接诊开方", "病历", "开具处方并扣减库存"),
    ("收费", "收费单", "现金/微信收款"),
    ("退费", "收费单", "冲正并退回药品"),
    ("入库", "药品", "采购入库"),
    ("出库", "药品", "报损出库"),
    ("办理入院", "住院单", "开具住院单并登记床位"),
    ("办理出院", "住院单", "填写出院小结并办理出院"),
]
oper_logs = []   # (staff_idx, action, target, detail, time)
for _ in range(14):
    si = random.randrange(len(STAFF))
    act, tgt, det = random.choice(OP_ACTIONS)
    tm = pick_time(NOW - timedelta(days=random.randint(0, 6)), 8, 17)
    oper_logs.append((si, act, tgt, det, tm))
oper_logs.append((0, "登录", "系统", "账号密码登录成功", NOW - timedelta(minutes=8)))

# 自检：所有演示时间都不得晚于"此刻"（挂号 / 病历 / 库存流水 / 收费 / 日志一起查）
_all_times = ([r[3] for r in registrations] +
              [r[3] for r in records] +
              [m[6] for m in med_logs] +
              [c["charge_time"] for c in charges] +
              [c["refund_time"] for c in charges if c["refund_time"]] +
              [d["dispense_time"] for d in dispenses] +
              [d["return_time"] for d in dispenses if d["return_time"]] +
              [a["admit_time"] for a in admissions] +
              [a["discharge_time"] for a in admissions if a["discharge_time"]] +
              [o[4] for o in oper_logs])
_future = sorted(t for t in _all_times if t > NOW)
assert not _future, "存在晚于此刻的时间：%s" % _future[:5]

# ---------------- 组装 SQL ----------------
S = []
S.append("-- 医院信息管理系统 演示数据（自动生成，可重复执行）")
S.append("-- 生成时间：%s" % NOW.strftime("%Y-%m-%d %H:%M:%S"))
S.append("-- 说明：会先清空原有数据再重新写入")
S.append("")
S.append("USE hospital_db;")
S.append("")
S.append("SET FOREIGN_KEY_CHECKS = 0;")
for t in ["admission", "dispense", "charge_detail", "charge", "operation_log", "staff",
          "medicine_stock_log", "prescription_detail", "medical_record", "registration",
          "doctor_schedule", "medicine", "patient", "doctor", "department"]:
    S.append("TRUNCATE TABLE %s;" % t)
S.append("SET FOREIGN_KEY_CHECKS = 1;")
S.append("")

S.append("-- 科室：%d 条（已停用 %d 条）" % (len(DEPTS), len(OFF_DEPT_IDX)))
for _i, (name, loc, phone) in enumerate(DEPTS):
    _st = "停用" if _i in OFF_DEPT_IDX else "启用"
    S.append("INSERT INTO department (dept_name, dept_location, dept_phone, status) "
             "VALUES (%s, %s, %s, %s);"
             % (esc(name), esc(loc), esc(phone), esc(_st)))
S.append("")

S.append("-- 医生：%d 名（已离职 %d 名，历史记录保留、不再出现在新增下拉里）"
         % (len(doctors), len(LEAVING_DOCTOR_IDX)))
for _i, (name, gender, title, dept_idx, spec, phone) in enumerate(doctors):
    _st = "离职" if _i in LEAVING_DOCTOR_IDX else "在职"
    S.append("INSERT INTO doctor (doctor_name, gender, title, dept_id, specialty, phone, status) "
             "VALUES (%s, %s, %s, %d, %s, %s, %s);"
             % (esc(name), esc(gender), esc(title), dept_idx + 1, esc(spec), esc(phone), esc(_st)))
S.append("")

S.append("-- 患者：%d 名" % len(patients))
for name, gender, birth, idc, phone, addr in patients:
    S.append("INSERT INTO patient (patient_name, gender, birth_date, id_card, phone, address) "
             "VALUES (%s, %s, %s, %s, %s, %s);"
             % (esc(name), esc(gender), esc(birth), esc(idc), esc(phone), esc(addr)))
S.append("")

S.append("-- 药品：%d 种 / %d 个分类（库存低于 100 的会出现在预警里；已停用 %d 种）"
         % (len(MEDS), len(MED_CATEGORIES), len(OFF_MED_IDX)))
for _i, m in enumerate(MEDS):
    _st = "停用" if _i in OFF_MED_IDX else "在用"
    S.append("INSERT INTO medicine "
             "(medicine_name, category, spec, unit, price, stock, manufacturer, status) "
             "VALUES (%s, %s, %s, %s, %.2f, %d, %s, %s);"
             % (esc(m.name), esc(m.category), esc(m.spec), esc(m.unit), m.price, m.stock,
                esc(m.mfr), esc(_st)))
S.append("")

S.append("-- 医生排班与号源：%d 条（含今天起 3 天全医生放号）" % len(schedules))
for s in schedules:
    S.append("INSERT INTO doctor_schedule "
             "(doctor_id, work_date, time_period, total_quota, booked_count, status) "
             "VALUES (%d, %s, %s, %d, %d, %s);"
             % (s["doctor_idx"] + 1, esc(s["day"].strftime("%Y-%m-%d")),
                esc(s["period"]), s["total_quota"], s["booked"], esc(s["status"])))
S.append("")

S.append("-- 挂号：%d 条（初诊 %d + 复诊 %d）"
         % (len(registrations), len(registrations) - revisit_count, revisit_count))
for (p_idx, d_idx, dept_idx, reg_date, fee, status, vtype, parent_idx,
     period, qno, sched_id) in registrations:
    parent_sql = "NULL" if parent_idx is None else str(parent_idx + 1)
    S.append("INSERT INTO registration "
             "(patient_id, doctor_id, dept_id, schedule_id, reg_date, time_period, queue_no, "
             " reg_fee, reg_status, visit_type, parent_reg_id) "
             "VALUES (%d, %d, %d, %d, %s, %s, %d, %.2f, %s, %s, %s);"
             % (p_idx + 1, d_idx + 1, dept_idx + 1, sched_id,
                esc(reg_date.strftime("%Y-%m-%d %H:%M:%S")), esc(period), qno,
                fee, esc(status), esc(vtype), parent_sql))
S.append("")

S.append("-- 病历：%d 条" % len(records))
for reg_idx, p_idx, d_idx, visit_date, diag, presc, remark in records:
    S.append("INSERT INTO medical_record (patient_id, doctor_id, reg_id, visit_date, diagnosis, prescription, remark) "
             "VALUES (%d, %d, %d, %s, %s, %s, %s);"
             % (p_idx + 1, d_idx + 1, reg_idx + 1,
                esc(visit_date.strftime("%Y-%m-%d %H:%M:%S")),
                esc(diag), esc(presc), esc(remark)))
S.append("")

S.append("-- 处方明细：%d 条" % len(details))
for rec_idx, med_id, qty, dosage in details:
    S.append("INSERT INTO prescription_detail (record_id, medicine_id, quantity, dosage) "
             "VALUES (%d, %d, %d, %s);" % (rec_idx + 1, med_id, qty, esc(dosage)))
S.append("")

S.append("-- 药品出入库流水：%d 条（期初入库 %d + 处方扣减 %d）"
         % (len(med_logs), len(MEDS), len(med_logs) - len(MEDS)))
for med_id, ctype, qty, before, after, reason, tm in sorted(med_logs, key=lambda x: (x[6], x[0], x[1])):
    S.append("INSERT INTO medicine_stock_log "
             "(medicine_id, change_type, quantity, before_stock, after_stock, reason, create_time) "
             "VALUES (%d, %s, %d, %d, %d, %s, %s);"
             % (med_id, esc(ctype), qty, before, after, esc(reason),
                esc(tm.strftime("%Y-%m-%d %H:%M:%S"))))
S.append("")

# 收费单按 charge_time 排序后插入，charge_id 由自增列按"插入顺序"分配；
# 收费明细里的 ci 是 charges 的"追加顺序"。两者顺序不一致时必须换算，
# 否则明细会挂到编号相邻但内容无关的别的收费单上（曾出过这个 bug）。
charge_order = sorted(range(len(charges)), key=lambda i: charges[i]["charge_time"])
charge_id_of = {old_i: pos + 1 for pos, old_i in enumerate(charge_order)}

# 写库前自检：每张收费单的"药费"必须等于它名下明细里药品项的合计
_sum_by_charge = defaultdict(float)
for _ci, _itype, _mid, _name, _p, _q, _amt in charge_details:
    if _itype == "药品费":
        _sum_by_charge[_ci] += _amt
for _ci, _c in enumerate(charges):
    assert abs(_c["drug_fee"] - round(_sum_by_charge.get(_ci, 0.0), 2)) < 0.01, \
        "收费单 #%d 的 drug_fee 与收费明细对不上" % (_ci + 1)

S.append("-- 收费单：%d 张（已收费 %d / 已退费 %d）"
         % (len(charges),
            sum(1 for c in charges if c["status"] == "已收费"),
            sum(1 for c in charges if c["status"] == "已退费")))
for old_i in charge_order:
    c = charges[old_i]
    S.append("INSERT INTO charge "
             "(charge_no, reg_id, patient_id, reg_fee, drug_fee, total_fee, pay_method, "
             " charge_status, charge_time, operator, refund_time, refund_reason, refund_operator) "
             "VALUES (%s, %d, %d, %.2f, %.2f, %.2f, %s, %s, %s, %s, %s, %s, %s);"
             % (esc(c["charge_no"]), c["reg_idx"] + 1, c["patient_idx"] + 1,
                c["reg_fee"], c["drug_fee"], c["total_fee"], esc(c["pay_method"]),
                esc(c["status"]), esc(c["charge_time"].strftime("%Y-%m-%d %H:%M:%S")),
                esc(c["operator"]),
                "NULL" if not c["refund_time"] else esc(c["refund_time"].strftime("%Y-%m-%d %H:%M:%S")),
                "NULL" if not c["refund_reason"] else esc(c["refund_reason"]),
                "NULL" if not c["refund_operator"] else esc(c["refund_operator"])))
S.append("")

S.append("-- 收费明细（费用快照）：%d 条" % len(charge_details))
for ci, itype, med_id, name, price, qty, amount in charge_details:
    S.append("INSERT INTO charge_detail "
             "(charge_id, item_type, medicine_id, item_name, unit_price, quantity, amount) "
             "VALUES (%d, %s, %s, %s, %.2f, %d, %.2f);"
             % (charge_id_of[ci], esc(itype), "NULL" if med_id is None else str(med_id),
                esc(name), price, qty, amount))
S.append("")

S.append("-- 药房发药记录：%d 条（已发药 %d / 已退回 %d）"
         % (len(dispenses),
            sum(1 for d in dispenses if d["status"] == "已发药"),
            sum(1 for d in dispenses if d["status"] == "已退回")))
for d in sorted(dispenses, key=lambda x: x["dispense_time"]):
    S.append("INSERT INTO dispense "
             "(reg_id, record_id, dispense_status, pharmacist, dispense_time, return_time, remark) "
             "VALUES (%d, %d, %s, %s, %s, %s, %s);"
             % (d["reg_idx"] + 1, d["record_idx"] + 1, esc(d["status"]), esc("赵敏"),
                esc(d["dispense_time"].strftime("%Y-%m-%d %H:%M:%S")),
                "NULL" if not d["return_time"] else esc(d["return_time"].strftime("%Y-%m-%d %H:%M:%S")),
                esc("退费退药" if d["status"] == "已退回" else "按处方核对发药")))
S.append("")

S.append("-- 住院登记：%d 条（在院 %d / 已出院 %d）"
         % (len(admissions),
            sum(1 for a in admissions if a["status"] == "在院"),
            sum(1 for a in admissions if a["status"] == "已出院")))
S.append("-- 注意：current_patient_id 是生成列，不能手动赋值，所以不在插入列里")
for a in sorted(admissions, key=lambda x: (x["admit_time"], x["patient_idx"])):
    S.append("INSERT INTO admission "
             "(admission_no, patient_id, dept_id, doctor_id, bed_no, admit_diagnosis, "
             " admit_time, discharge_time, status, discharge_summary, "
             " admit_operator, discharge_operator) "
             "VALUES (%s, %d, %d, %d, %s, %s, %s, %s, %s, %s, %s, %s);"
             % (esc(a["admission_no"]), a["patient_idx"] + 1, a["dept_idx"] + 1,
                a["doctor_idx"] + 1, esc(a["bed_no"]), esc(a["admit_diagnosis"]),
                esc(a["admit_time"].strftime("%Y-%m-%d %H:%M:%S")),
                "NULL" if not a["discharge_time"]
                else esc(a["discharge_time"].strftime("%Y-%m-%d %H:%M:%S")),
                esc(a["status"]),
                "NULL" if not a["discharge_summary"] else esc(a["discharge_summary"]),
                esc(a["admit_operator"]),
                "NULL" if not a["discharge_operator"] else esc(a["discharge_operator"])))
S.append("")

S.append("-- 员工账号：%d 个（密码存的是哈希，明文见 seed_demo_data.py 注释）" % len(STAFF))
for username, plain, real_name, role, phone in STAFF:
    S.append("INSERT INTO staff (username, password_hash, real_name, role, phone) "
             "VALUES (%s, %s, %s, %s, %s);"
             % (esc(username), esc(generate_password_hash(plain)), esc(real_name),
                esc(role), esc(phone)))
S.append("")

S.append("-- 操作日志：%d 条" % len(oper_logs))
for si, action, target, detail, tm in sorted(oper_logs, key=lambda x: x[4]):
    username, _, real_name, role, _ = STAFF[si]
    S.append("INSERT INTO operation_log "
             "(staff_id, staff_name, role, action, target, detail, ip, create_time) "
             "VALUES (%d, %s, %s, %s, %s, %s, '127.0.0.1', %s);"
             % (si + 1, esc(real_name), esc(role), esc(action), esc(target),
                esc(detail), esc(tm.strftime("%Y-%m-%d %H:%M:%S"))))
S.append("")

sql_text = "\n".join(S)
out_path = os.path.join(HERE, "04_seed_demo_data.sql")
with open(out_path, "w", encoding="utf-8") as f:
    f.write(sql_text)
print("已生成 SQL 文件：", out_path)


# ---------------- 老库自动补字段（保证脚本可重复执行） ----------------
# 新增的四张表 DDL，内容与 01_create_tables.sql 保持一致
NEW_TABLES = {
    "charge": """
        CREATE TABLE charge (
            charge_id     INT PRIMARY KEY AUTO_INCREMENT COMMENT '收费单编号',
            charge_no     VARCHAR(24)  NOT NULL UNIQUE COMMENT '收费单号，形如 SF202610140001',
            reg_id        INT          NOT NULL UNIQUE COMMENT '关联挂号编号（一次挂号只收一次费）',
            patient_id    INT          NOT NULL COMMENT '患者编号',
            reg_fee       DECIMAL(8,2) NOT NULL DEFAULT 0.00 COMMENT '挂号费',
            drug_fee      DECIMAL(8,2) NOT NULL DEFAULT 0.00 COMMENT '药费合计',
            total_fee     DECIMAL(10,2) NOT NULL DEFAULT 0.00 COMMENT '应收合计 = 挂号费 + 药费',
            pay_method    VARCHAR(10)  NOT NULL DEFAULT '现金' COMMENT '支付方式：现金/微信/支付宝/银行卡',
            charge_status VARCHAR(10)  NOT NULL DEFAULT '已收费' COMMENT '状态：已收费/已退费',
            charge_time   DATETIME     NOT NULL COMMENT '收费时间',
            operator      VARCHAR(30)  COMMENT '收费员',
            refund_time   DATETIME     NULL COMMENT '退费时间',
            refund_reason VARCHAR(100) NULL COMMENT '退费原因',
            refund_operator VARCHAR(30) NULL COMMENT '退费操作员',
            CONSTRAINT fk_charge_reg     FOREIGN KEY (reg_id)     REFERENCES registration(reg_id),
            CONSTRAINT fk_charge_patient FOREIGN KEY (patient_id) REFERENCES patient(patient_id)
        ) COMMENT '门诊收费单表'
    """,
    "charge_detail": """
        CREATE TABLE charge_detail (
            cd_id       INT PRIMARY KEY AUTO_INCREMENT COMMENT '明细编号',
            charge_id   INT           NOT NULL COMMENT '收费单编号',
            item_type   VARCHAR(10)   NOT NULL COMMENT '费用类型：挂号费/药品费',
            medicine_id INT           NULL COMMENT '药品编号（挂号费为 NULL）',
            item_name   VARCHAR(100)  NOT NULL COMMENT '项目名称快照',
            unit_price  DECIMAL(8,2)  NOT NULL COMMENT '单价快照',
            quantity    INT           NOT NULL DEFAULT 1 COMMENT '数量',
            amount      DECIMAL(10,2) NOT NULL COMMENT '金额 = 单价 × 数量',
            CONSTRAINT fk_cd_charge FOREIGN KEY (charge_id) REFERENCES charge(charge_id)
        ) COMMENT '收费明细表（费用快照）'
    """,
    "staff": """
        CREATE TABLE staff (
            staff_id      INT PRIMARY KEY AUTO_INCREMENT COMMENT '员工编号',
            username      VARCHAR(30)  NOT NULL UNIQUE COMMENT '登录账号',
            password_hash VARCHAR(255) NOT NULL COMMENT '密码哈希（scrypt，不存明文）',
            real_name     VARCHAR(30)  NOT NULL COMMENT '姓名',
            role          VARCHAR(20)  NOT NULL COMMENT '角色：管理员/挂号员/医生/药房/收费员',
            phone         VARCHAR(20)  COMMENT '联系电话',
            status        TINYINT      NOT NULL DEFAULT 1 COMMENT '1 启用 / 0 停用',
            last_login    DATETIME     NULL COMMENT '最近登录时间',
            create_time   DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '建档时间'
        ) COMMENT '员工与登录账号表'
    """,
    "operation_log": """
        CREATE TABLE operation_log (
            log_id      INT PRIMARY KEY AUTO_INCREMENT COMMENT '日志编号',
            staff_id    INT          NULL COMMENT '操作人编号（未登录操作为 NULL）',
            staff_name  VARCHAR(30)  COMMENT '操作人姓名',
            role        VARCHAR(20)  COMMENT '操作人角色',
            action      VARCHAR(30)  NOT NULL COMMENT '动作：登录/收费/退费/入库/出库/接诊开方/新增挂号/状态变更',
            target      VARCHAR(50)  COMMENT '操作对象，如 挂号 #12、收费单 SF202610140001',
            detail      VARCHAR(200) COMMENT '补充说明',
            ip          VARCHAR(45)  COMMENT '来源 IP',
            create_time DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '操作时间'
        ) COMMENT '操作日志表'
    """,
    "doctor_schedule": """
        CREATE TABLE doctor_schedule (
            schedule_id  INT PRIMARY KEY AUTO_INCREMENT COMMENT '排班编号',
            doctor_id    INT          NOT NULL COMMENT '医生编号',
            work_date    DATE         NOT NULL COMMENT '出诊日期',
            time_period  VARCHAR(4)   NOT NULL COMMENT '时段：上午/下午',
            total_quota  INT          NOT NULL DEFAULT 30 COMMENT '放号总数（号源）',
            booked_count INT          NOT NULL DEFAULT 0  COMMENT '已挂出号数',
            status       VARCHAR(4)   NOT NULL DEFAULT '正常' COMMENT '状态：正常/停诊',
            create_time  DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
            CONSTRAINT uk_sched_unique UNIQUE (doctor_id, work_date, time_period),
            CONSTRAINT fk_sched_doctor FOREIGN KEY (doctor_id) REFERENCES doctor(doctor_id)
        ) COMMENT '医生排班与号源表'
    """,
    "dispense": """
        CREATE TABLE dispense (
            dispense_id     INT PRIMARY KEY AUTO_INCREMENT COMMENT '发药编号',
            reg_id          INT          NOT NULL UNIQUE COMMENT '关联挂号编号（一次挂号只发一次）',
            record_id       INT          NULL COMMENT '关联病历编号',
            dispense_status VARCHAR(8)   NOT NULL DEFAULT '已发药' COMMENT '状态：已发药/已退回',
            pharmacist      VARCHAR(30)  COMMENT '发药药师',
            dispense_time   DATETIME     NOT NULL COMMENT '发药时间',
            return_time     DATETIME     NULL COMMENT '退回时间（退费退药时填写）',
            remark          VARCHAR(100) COMMENT '备注',
            CONSTRAINT fk_disp_reg    FOREIGN KEY (reg_id)    REFERENCES registration(reg_id),
            CONSTRAINT fk_disp_record FOREIGN KEY (record_id) REFERENCES medical_record(record_id)
        ) COMMENT '药房发药记录表'
    """,
    "admission": """
        CREATE TABLE admission (
            admission_id      INT PRIMARY KEY AUTO_INCREMENT COMMENT '住院登记编号',
            admission_no      VARCHAR(24)  NOT NULL UNIQUE COMMENT '住院号，形如 ZY202610140001',
            patient_id        INT          NOT NULL COMMENT '患者编号',
            dept_id           INT          NOT NULL COMMENT '住院科室',
            doctor_id         INT          NOT NULL COMMENT '主治医生',
            bed_no            VARCHAR(20)  COMMENT '床位号，如 心血管内科-08',
            admit_diagnosis   VARCHAR(200) COMMENT '入院诊断',
            admit_time        DATETIME     NOT NULL COMMENT '入院时间',
            discharge_time    DATETIME     NULL COMMENT '出院时间（在院为 NULL）',
            status            VARCHAR(6)   NOT NULL DEFAULT '在院' COMMENT '状态：在院/已出院',
            discharge_summary VARCHAR(200) NULL COMMENT '出院小结',
            admit_operator    VARCHAR(30)  COMMENT '办理入院的员工',
            discharge_operator VARCHAR(30) NULL COMMENT '办理出院的员工',
            remark            VARCHAR(200) COMMENT '备注',
            create_time       DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '记录创建时间',
            current_patient_id INT GENERATED ALWAYS AS (IF(status = '在院', patient_id, NULL)) STORED
                               COMMENT '在院去重键：在院=患者编号，出院=NULL',
            CONSTRAINT uk_adm_in_ward   UNIQUE (current_patient_id),
            CONSTRAINT ck_adm_time      CHECK (discharge_time IS NULL OR discharge_time >= admit_time),
            CONSTRAINT ck_adm_discharge CHECK ((status = '在院'   AND discharge_time IS NULL)
                                            OR (status = '已出院' AND discharge_time IS NOT NULL)),
            CONSTRAINT fk_adm_patient   FOREIGN KEY (patient_id) REFERENCES patient(patient_id),
            CONSTRAINT fk_adm_dept      FOREIGN KEY (dept_id)    REFERENCES department(dept_id),
            CONSTRAINT fk_adm_doctor    FOREIGN KEY (doctor_id)  REFERENCES doctor(doctor_id)
        ) COMMENT '住院登记表'
    """,
}


def ensure_schema(cur):
    """early 版本的库没有 visit_type / parent_reg_id，这里自动补上"""
    # 药品分类（旧库的 medicine 表没有这一列，药品页要按分类筛选，所以补上）
    cur.execute("SHOW COLUMNS FROM medicine LIKE 'category'")
    if not cur.fetchone():
        cur.execute("ALTER TABLE medicine ADD COLUMN category VARCHAR(30) NULL "
                    "COMMENT '药品分类' AFTER medicine_name")
        print("  已补充字段：medicine.category")

    # 基础数据的"启用状态"（科室/医生/药品）。
    # 停用只改状态、不删记录：老数据（历史挂号、病历、处方、收费快照）都还引用着它们，
    # 物理删除会连带毁掉历史；停用后只是不再出现在"新增"的候选下拉里。
    for tbl, col, ddl in [
        ("department", "status",
         "ALTER TABLE department ADD COLUMN status VARCHAR(4) NOT NULL DEFAULT '启用' "
         "COMMENT '状态：启用/停用' AFTER dept_phone"),
        ("doctor", "status",
         "ALTER TABLE doctor ADD COLUMN status VARCHAR(4) NOT NULL DEFAULT '在职' "
         "COMMENT '状态：在职/离职' AFTER phone"),
        ("medicine", "status",
         "ALTER TABLE medicine ADD COLUMN status VARCHAR(4) NOT NULL DEFAULT '在用' "
         "COMMENT '状态：在用/停用' AFTER manufacturer"),
    ]:
        cur.execute("SHOW COLUMNS FROM %s LIKE %%s" % tbl, (col,))
        if not cur.fetchone():
            cur.execute(ddl)
            print("  已补充字段：%s.%s" % (tbl, col))

    cur.execute("SHOW COLUMNS FROM registration LIKE 'visit_type'")
    if not cur.fetchone():
        cur.execute("ALTER TABLE registration ADD COLUMN visit_type VARCHAR(10) "
                    "NOT NULL DEFAULT '初诊' COMMENT '就诊类型：初诊/复诊' AFTER reg_status")
        print("  已补充字段：registration.visit_type")
    cur.execute("SHOW COLUMNS FROM registration LIKE 'parent_reg_id'")
    if not cur.fetchone():
        cur.execute("ALTER TABLE registration ADD COLUMN parent_reg_id INT NULL "
                    "COMMENT '复诊时关联的原挂号编号（初诊为 NULL）' AFTER visit_type")
        cur.execute("ALTER TABLE registration ADD CONSTRAINT fk_reg_parent "
                    "FOREIGN KEY (parent_reg_id) REFERENCES registration(reg_id)")
        print("  已补充字段：registration.parent_reg_id")
    cur.execute("SHOW INDEX FROM registration WHERE Key_name = 'idx_reg_status'")
    if not cur.fetchone():
        cur.execute("CREATE INDEX idx_reg_status ON registration(reg_status)")
        print("  已补充索引：idx_reg_status")

    # 药品出入库流水表（早期版本还没有这张表）
    cur.execute("SHOW TABLES LIKE 'medicine_stock_log'")
    if not cur.fetchone():
        cur.execute(
            """
            CREATE TABLE medicine_stock_log (
                log_id        INT PRIMARY KEY AUTO_INCREMENT COMMENT '流水编号',
                medicine_id   INT          NOT NULL COMMENT '药品编号',
                change_type   VARCHAR(10)  NOT NULL COMMENT '变动类型：入库/出库',
                quantity      INT          NOT NULL COMMENT '变动数量（正数）',
                before_stock  INT          NOT NULL COMMENT '变动前库存',
                after_stock   INT          NOT NULL COMMENT '变动后库存',
                reason        VARCHAR(100) COMMENT '原因：采购入库/处方扣减/报损出库等',
                create_time   DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '变动时间',
                CONSTRAINT fk_log_medicine FOREIGN KEY (medicine_id) REFERENCES medicine(medicine_id)
            ) COMMENT '药品出入库流水表'
            """
        )
        cur.execute("CREATE INDEX idx_log_medicine ON medicine_stock_log(medicine_id)")
        cur.execute("CREATE INDEX idx_log_time ON medicine_stock_log(create_time)")
        print("  已补充表：medicine_stock_log")

    # 收费 / 员工 / 操作日志四张表（早期版本还没有），表结构同 01_create_tables.sql
    for tname, ddl in NEW_TABLES.items():
        cur.execute("SHOW TABLES LIKE %s", (tname,))
        if not cur.fetchone():
            cur.execute(ddl)
            print("  已补充表：%s" % tname)

    for iname, tname, cols in [
        ("idx_charge_time", "charge", "charge_time"),
        ("idx_charge_status", "charge", "charge_status"),
        ("idx_cd_charge", "charge_detail", "charge_id"),
        ("idx_oplog_time", "operation_log", "create_time"),
        ("idx_oplog_staff", "operation_log", "staff_id"),
    ]:
        cur.execute("SHOW INDEX FROM %s WHERE Key_name = %%s" % tname, (iname,))
        if not cur.fetchone():
            cur.execute("CREATE INDEX %s ON %s(%s)" % (iname, tname, cols))
            print("  已补充索引：%s" % iname)

    # registration 的排班 / 时段 / 候诊序号字段（旧库没有，自动补）
    for col, ddl in [
        ("schedule_id", "ALTER TABLE registration ADD COLUMN schedule_id INT NULL "
                        "COMMENT '关联排班编号（占用的号源）' AFTER dept_id"),
        ("time_period", "ALTER TABLE registration ADD COLUMN time_period VARCHAR(4) NULL "
                        "COMMENT '就诊时段：上午/下午' AFTER reg_date"),
        ("queue_no", "ALTER TABLE registration ADD COLUMN queue_no INT NULL "
                     "COMMENT '当日该医生的候诊序号' AFTER time_period"),
        ("called_time", "ALTER TABLE registration ADD COLUMN called_time DATETIME NULL "
                        "COMMENT '最近一次叫号时间' AFTER queue_no"),
    ]:
        cur.execute("SHOW COLUMNS FROM registration LIKE %s", (col,))
        if not cur.fetchone():
            cur.execute(ddl)
            print("  已补充字段：registration.%s" % col)

    cur.execute("SHOW INDEX FROM registration WHERE Key_name = 'idx_reg_schedule'")
    if not cur.fetchone():
        cur.execute("CREATE INDEX idx_reg_schedule ON registration(schedule_id)")
        print("  已补充索引：idx_reg_schedule")

    for iname, tname, cols in [
        ("idx_sched_date", "doctor_schedule", "work_date"),
        ("idx_sched_doctor", "doctor_schedule", "doctor_id"),
        ("idx_disp_time", "dispense", "dispense_time"),
        ("idx_adm_patient", "admission", "patient_id"),
        ("idx_adm_status", "admission", "status"),
        ("idx_adm_time", "admission", "admit_time"),
        ("idx_adm_dept", "admission", "dept_id"),
    ]:
        cur.execute("SHOW INDEX FROM %s WHERE Key_name = %%s" % tname, (iname,))
        if not cur.fetchone():
            cur.execute("CREATE INDEX %s ON %s(%s)" % (iname, tname, cols))
            print("  已补充索引：%s" % iname)


# ---------------- 执行导入 ----------------
conn = pymysql.connect(host=DB_CONFIG["host"], port=DB_CONFIG["port"],
                       user=DB_CONFIG["user"], password=DB_CONFIG["password"],
                       database=DB_CONFIG["database"], charset="utf8mb4")
try:
    with conn.cursor() as cur:
        ensure_schema(cur)
        count = 0
        for stmt in sql_text.split("\n"):
            stmt = stmt.strip()
            if not stmt or stmt.startswith("--"):
                continue
            if not stmt.endswith(";"):
                continue
            cur.execute(stmt)
            count += 1
    conn.commit()
    print("导入完成，共执行 %d 条 SQL。" % count)
    print("  科室 %d / 医生 %d / 患者 %d / 药品 %d / 挂号 %d（含复诊 %d）/ 病历 %d / 处方明细 %d / 库存流水 %d"
          % (len(DEPTS), len(doctors), len(patients), len(MEDS),
             len(registrations), revisit_count, len(records), len(details), len(med_logs)))
    print("  收费单 %d（已收费 %d / 已退费 %d）/ 收费明细 %d / 员工 %d / 操作日志 %d"
          % (len(charges), sum(1 for c in charges if c["status"] == "已收费"),
             sum(1 for c in charges if c["status"] == "已退费"),
             len(charge_details), len(STAFF), len(oper_logs)))
    print("  医生排班 %d 条 / 发药记录 %d 条（已发药 %d / 已退回 %d）"
          % (len(schedules), len(dispenses),
             sum(1 for d in dispenses if d["status"] == "已发药"),
             sum(1 for d in dispenses if d["status"] == "已退回")))
    print("  住院登记 %d 条（在院 %d / 已出院 %d）"
          % (len(admissions),
             sum(1 for a in admissions if a["status"] == "在院"),
             sum(1 for a in admissions if a["status"] == "已出院")))
    st = {}
    for r in registrations:
        st[r[5]] = st.get(r[5], 0) + 1
    print("  挂号状态分布：%s" % st)
finally:
    conn.close()
