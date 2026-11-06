# 医院信息管理系统 - Web 前端
# 运行方式：命令行执行 python app.py，然后浏览器打开 http://localhost:5000

import json
import os
import re
from datetime import date, datetime, timedelta
from functools import wraps
from io import BytesIO

import pymysql

from flask import (
    Flask, render_template, request, redirect, url_for, flash, abort, send_file,
    session, g,
)
from werkzeug.security import check_password_hash, generate_password_hash
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from db import query_all, query_one, execute, insert, transaction
import snapshot

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "hospital-demo-2026")   # 签名登录会话；本地演示用默认值即可

PAGE_SIZE = 15          # 列表页每页显示条数
LOW_STOCK = 100         # 库存低于这个数算"预警"
DANGER_STOCK = 50       # 低于这个数算"紧缺"

# 挂号状态流转规则：只有在"已挂号"状态下才允许改状态
STATUS_TARGETS = ("已就诊", "已取消")

# 门诊挂号状态机（见 README）：
#   已挂号 → 已就诊 → 已收费 → 已发药（取消 / 退费是两条支路）
REG_STATUSES = ("已挂号", "已就诊", "已收费", "已发药", "已退费", "已取消")
PAY_METHODS = ("现金", "微信", "支付宝", "银行卡")
CHARGE_SEQ_PREFIX = "SF"      # 收费单号前缀，如 SF202610140001
TIME_PERIODS = ("上午", "下午")     # 排班时段
SCHEDULE_STATUSES = ("正常", "停诊")
BOOK_AHEAD_DAYS = 2               # 允许提前几天挂号（与 seed 里放号的天数对应）
QUEUE_DEFAULT_PERIOD = "上午"      # 候诊叫号页默认看的时段

# 住院登记（admission）：住院是一次"事件"不是患者身上的开关，
# 同一个患者可以有多条住院记录，但同一时间只能有一条"在院"
# （这条规则由表上的生成列 current_patient_id + 唯一键在数据库层保证）
ADMISSION_STATUSES = ("在院", "已出院")
ADMISSION_SEQ_PREFIX = "ZY"       # 住院号前缀，如 ZY202610140001

# 基础数据（主数据）的状态值域。
# 这些档案都被历史业务数据引用着（科室牵着医生、医生牵着病历、药品牵着处方），
# 所以只做"停用"不做物理删除：停用后不再出现在新增的下拉候选里，历史记录照常可查。
DEPT_STATUSES = ("启用", "停用")
DOCTOR_STATUSES = ("在职", "离职")
DOCTOR_TITLES = ("主任医师", "副主任医师", "主治医师", "住院医师")
GENDERS = ("男", "女")
MEDICINE_STATUSES = ("在用", "停用")


class BizError(Exception):
    """业务规则不满足时抛出（比如库存不够），交给页面提示用户"""
    pass


# ---------------- 登录与角色权限 ----------------
# 每个角色能做哪些事。'*' 表示全部权限。
# 权限点与控制的路由一一对应，见下面的 ENDPOINT_PERMS。
ROLE_PERMS = {
    "管理员": {"*"},
    "挂号员": {"reg.view", "reg.add", "reg.cancel", "patient.view", "patient.edit",
               "charge.view", "charge.do", "print", "dashboard.view",
               "schedule.view", "schedule.manage", "queue.view",
               "admission.view", "admission.add", "admission.discharge"},
    "医生":   {"reg.view", "reg.visit", "patient.view", "print", "dashboard.view",
               "queue.view", "queue.call",
               "admission.view", "admission.add", "admission.discharge"},
    "药房":   {"medicine.view", "medicine.stock", "medicine.manage", "stocklog.view",
               "patient.view", "charge.view", "dashboard.view",
               "dispense.view", "dispense.do",
               "admission.view"},
    "收费员": {"charge.view", "charge.do", "charge.refund", "print",
               "reg.view", "patient.view", "dashboard.view",
               "admission.view"},
}

# 路由 → 需要的权限。没登记的路由只要求"已登录"（白名单外的默认是收紧而不是放开）
ENDPOINT_PERMS = {
    "home": None,
    "registrations": "reg.view",
    "add": "reg.add",
    "change_reg_status": "reg.cancel",
    "visit": "reg.visit",
    "print_registration": "print",
    "export_registrations": "reg.view",
    "patients": "patient.view",
    "patient_detail": "patient.view",
    "patient_edit": "patient.edit",
    "export_patients": "patient.view",
    "schedules": "schedule.view",
    "schedule_add": "schedule.manage",
    "schedule_toggle": "schedule.manage",
    "schedule_quota": "schedule.manage",
    "queue": "queue.view",
    "queue_call": "queue.call",
    "dispenses": "dispense.view",
    "dispense": "dispense.do",
    "admissions": "admission.view",
    "admission_new": "admission.add",
    "admission_discharge": "admission.discharge",
    "print_admission": "admission.view",
    "export_admissions": "admission.view",
    "medicines": "medicine.view",
    "medicine_stock": "medicine.stock",
    "stock_logs": "stocklog.view",
    "charges": "charge.view",
    "charge_page": "charge.view",
    "charge_pay": "charge.do",
    "charge_refund": "charge.refund",
    "print_charge": "charge.view",     # 收据属于收费业务，跟着收费查看权限走
    "export_charges": "charge.view",
    "dashboard": "dashboard.view",
    "logs": "log.view",
    "staff_list": "staff.view",
    # 数据存档：把整库导出成快照 / 恢复，属于系统级操作，只有管理员能做
    "backups": "backup.view",
    "backup_create": "backup.manage",
    "backup_restore": "backup.manage",
    "backup_delete": "backup.manage",
    # 基础数据维护（主数据）：科室 / 医生 / 药品目录 / 员工账号。
    # 这一层决定"业务数据从哪来"，与日常业务（挂号、发药）分开授权：
    # 科室与医生档案属于行政信息，只给管理员；药品目录归药剂科（药房）。
    "depts": "dept.manage",
    "dept_save": "dept.manage",
    "dept_toggle": "dept.manage",
    "doctors": "doctor.manage",
    "doctor_save": "doctor.manage",
    "doctor_toggle": "doctor.manage",
    "medicine_save": "medicine.manage",
    "medicine_toggle": "medicine.manage",
    "staff_save": "staff.manage",
    "staff_toggle": "staff.manage",
    "staff_password": "staff.manage",
}
OPEN_ENDPOINTS = ("static", "login", "logout")


def current_staff():
    """当前登录的人（没登录返回 None）"""
    if not session.get("staff_id"):
        return None
    return {
        "staff_id": session.get("staff_id"),
        "username": session.get("username"),
        "real_name": session.get("real_name"),
        "role": session.get("role"),
    }


def has_perm(perm):
    """当前登录的人有没有某个权限"""
    if not perm:
        return True
    role = session.get("role")
    if not role:
        return False
    allowed = ROLE_PERMS.get(role, set())
    return "*" in allowed or perm in allowed


# 模板里可以直接用 can('charge.do') 决定要不要显示某个按钮
app.jinja_env.globals["can"] = has_perm
app.jinja_env.globals["current_staff"] = current_staff


def require_perm(perm):
    """给单个视图加权限校验（列表类页面用；全局兜底见 guard()）"""
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not session.get("staff_id"):
                return redirect(url_for("login", next=request.full_path))
            if not has_perm(perm):
                abort(403)
            return fn(*args, **kwargs)
        return wrapper
    return deco


@app.before_request
def guard():
    """统一入口守卫：未登录 → 跳登录页；登录了但没权限 → 403

    放在 here 而不是每个视图上加装饰器，好处是新增路由如果忘了登记权限，
    默认也是"要求登录"（收紧），不会一不小心对外开放。
    """
    ep = request.endpoint
    if ep is None or ep in OPEN_ENDPOINTS:
        return None
    if not session.get("staff_id"):
        nxt = request.full_path if request.method == "GET" else None
        return redirect(url_for("login", next=nxt))
    perm = ENDPOINT_PERMS.get(ep)
    if perm and not has_perm(perm):
        abort(403)
    return None


@app.errorhandler(403)
def on_403(e):
    return render_template("error.html", code=403, title="没有权限",
                           msg="当前账号（%s · %s）不能访问这个功能，请用有权限的账号登录。"
                               % (session.get("real_name") or "未登录", session.get("role") or "-")), 403


@app.errorhandler(404)
def on_404(e):
    return render_template("error.html", code=404, title="页面不存在",
                           msg="地址可能写错了，或者这条数据已经被删除。"), 404


def log_action(action, target="", detail=""):
    """记一条操作日志。

    日志属于"顺带做的事"，写失败也不能影响主业务（比如库存已经扣了，
    不能因为日志表出问题就把整单回滚），所以这里吞掉异常。
    """
    try:
        execute(
            """
            INSERT INTO operation_log
                (staff_id, staff_name, role, action, target, detail, ip)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (session.get("staff_id"), session.get("real_name"), session.get("role"),
             action, (target or "")[:50], (detail or "")[:200], request.remote_addr),
        )
    except Exception:
        pass


# ---------------- 登录 / 登出 ----------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        nxt = request.form.get("next") or ""

        row = query_one(
            "SELECT staff_id, username, password_hash, real_name, role, status "
            "FROM staff WHERE username = %s",
            (username,),
        )
        # 账号不存在、密码不对、账号被停用，都给同一句提示（不告诉对方是哪一种）
        if row is None or not check_password_hash(row["password_hash"], password):
            flash("账号或密码不正确", "error")
            return render_template("login.html", username=username, next=nxt)
        if not row["status"]:
            flash("该账号已被停用，请联系管理员", "error")
            return render_template("login.html", username=username, next=nxt)

        session.clear()
        session["staff_id"] = row["staff_id"]
        session["username"] = row["username"]
        session["real_name"] = row["real_name"]
        session["role"] = row["role"]

        execute("UPDATE staff SET last_login = NOW() WHERE staff_id = %s", (row["staff_id"],))
        log_action("登录", "系统", "账号密码登录成功")

        # 只接受站内地址，防止被拼成外部跳转
        if nxt.startswith("/") and not nxt.startswith("//"):
            return redirect(nxt)
        return redirect(url_for("home"))

    return render_template("login.html", username="", next=request.args.get("next") or "")


@app.route("/logout")
def logout():
    log_action("登出", "系统", "退出登录")
    session.clear()
    flash("已退出登录", "ok")
    return redirect(url_for("login"))


# ---------------- 通用小工具 ----------------
def calc_age(birth):
    """按出生日期算周岁（出生日期可能为空，要防一下）"""
    if not birth:
        return None
    today = date.today()
    return today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))


def mask_phone(p):
    """手机号脱敏：13812345678 -> 138****5678"""
    p = p or ""
    return p[:3] + "****" + p[-4:] if len(p) >= 7 else p


def mask_id_card(s):
    """身份证脱敏：330521199001011234 -> 330521********1234"""
    s = s or ""
    return s[:6] + "********" + s[-4:] if len(s) >= 10 else s


def validate_new_patient(form):
    """校验"现场建档"表单，返回 (整理好的数据, 错误信息列表)

    数据库里 patient_name 和 gender 是 NOT NULL，所以这两项必须校验；
    身份证、手机号做格式校验，避免把脏数据写进库。
    """
    errs = []
    name = (form.get("new_name") or "").strip()
    gender = (form.get("new_gender") or "").strip()
    birth_str = (form.get("new_birth_date") or "").strip()
    id_card = (form.get("new_id_card") or "").strip().upper()
    phone = (form.get("new_phone") or "").strip()
    address = (form.get("new_address") or "").strip()

    if not name:
        errs.append("请填写患者姓名")
    elif len(name) > 30:
        errs.append("患者姓名不能超过 30 个字")

    if gender not in ("男", "女"):
        errs.append("请选择患者性别")

    birth = None
    if birth_str:
        try:
            birth = datetime.strptime(birth_str, "%Y-%m-%d").date()
        except ValueError:
            errs.append("出生日期格式不正确（应形如 2000-01-01）")
        else:
            if birth > date.today():
                errs.append("出生日期不能晚于今天")
            elif birth.year < 1900:
                errs.append("出生日期不能早于 1900 年")

    if id_card and not re.match(r"^\d{17}[\dX]$", id_card):
        errs.append("身份证号应为 18 位，末位可以是 X")

    if phone and not re.match(r"^1[3-9]\d{9}$", phone):
        errs.append("手机号格式不正确（应为 11 位数字）")

    data = {
        "patient_name": name,
        "gender": gender,
        "birth_date": birth,
        "id_card": id_card or None,
        "phone": phone or None,
        "address": address or None,
    }
    return data, errs


def clean_args(d):
    """把空参数去掉，避免生成 ?kw=&dept_id= 这种脏链接"""
    return {k: v for k, v in d.items() if v not in (None, "")}


KW_MAX = 50   # 搜索关键词最多 50 个字


def clean_kw(raw):
    """清洗搜索关键词：去首尾空白 + 限长。

    限长的必要性：关键词会拼进 URL。不设上限时，超长关键词（一两千字）
    会让请求行超过 WSGI 服务器的上限，服务器直接回 HTTP 431 并断开连接，
    用户看到的是"页面打不开"而不是"没搜到结果"。截断到合理长度最省事。
    """
    return (raw or "").strip()[:KW_MAX]


def json_for_script(obj):
    """把 Python 对象转成"能安全塞进 <script> 标签"的 JSON。

    json.dumps 不会转义 < > &，字符串里只要出现 </script> 就能提前闭合脚本标签，
    后面的内容随即被当成 HTML 解析（XSS）。这里把几个危险字符换成 \\uXXXX，
    在 JSON 里是等价写法，但 HTML 解析器不会再把它当标签。
    """
    return (json.dumps(obj, ensure_ascii=False)
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("&", "\\u0026")
            .replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029"))


def safe_back(default_endpoint="registrations"):
    """把"返回地址"限制在站内，防止被拼成外部跳转链接；顺手做一次"落点权限自检"。

    自检的必要性：POST 完通常会 redirect 回列表页。如果这个落点当前角色
    本来就没权限，用户就会看到"提交成功了，但页面变成 403 无权限"。
    真实的例子：药师点"发药"时表单里没带 back 参数，兜底落点原来是挂号列表，
    而药房角色没有挂号管理的权限，于是发药成功后直接跳到 403。
    所以：落点没权限时，退回首页，而不是把用户丢到错误页。

    ⚠ match 一定要显式指定 method="GET"。
    不指定时它会自动沿用"当前请求的方法"，而这个函数只在 POST 处理里被调用，
    于是变成"用 POST 去匹配只允许 GET 的 /queue"——必然抛 MethodNotAllowed，
    被下面的 except 吞掉，结果是每一次提交后都悄悄跳回首页，
    而且伪装成正常的 302，很难发现。
    这里要判断的是"这个页面能不能打开"，所以就该按 GET 去匹配。
    """
    back = request.form.get("back") or ""
    if not back.startswith("/") or back.startswith("//"):
        back = url_for(default_endpoint)
    try:
        path = back.split("?", 1)[0]
        endpoint = app.url_map.bind_to_environ(request.environ).match(path, method="GET")[0]
        need = ENDPOINT_PERMS.get(endpoint)
    except Exception:
        return url_for("home")
    return back if has_perm(need) else url_for("home")


def paginate(total, page):
    """算出：当前页、总页数、SQL 的 OFFSET"""
    total_pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    page = min(max(page, 1), total_pages)
    return page, total_pages, (page - 1) * PAGE_SIZE


def page_urls(endpoint, base_args, total_pages, current=1):
    """给模板用的分页链接：[(页码, 链接), ...]，页数多时只显示首尾和当前页附近"""
    if total_pages <= 12:
        nums = list(range(1, total_pages + 1))
    else:
        near = set(range(current - 1, current + 2))
        nums = sorted({1, 2, total_pages - 1, total_pages} | {p for p in near if 1 <= p <= total_pages})
    return [(p, url_for(endpoint, **clean_args({**base_args, "page": p})))
            for p in nums]


def stock_level(stock):
    """库存分级：给页面配色用"""
    if stock < DANGER_STOCK:
        return "danger"
    if stock < LOW_STOCK:
        return "warn"
    return "ok"


# ---------------- 收费相关的小工具 ----------------
CN_DIGITS = "零壹贰叁肆伍陆柒捌玖"
CN_UNITS = ["", "拾", "佰", "仟"]
CN_GROUPS = ["", "万", "亿"]


def rmb_upper(amount):
    """金额转中文大写，收据上要用（128.50 -> 壹佰贰拾捌元伍角）"""
    n = int(round(float(amount) * 100))
    if n == 0:
        return "零元整"
    yuan, cents = divmod(n, 100)
    jiao, fen = divmod(cents, 10)

    head = ""
    if yuan:
        groups, y = [], yuan
        while y > 0:
            groups.append(y % 10000)
            y //= 10000
        parts = []
        for gi in range(len(groups) - 1, -1, -1):
            g = groups[gi]
            if g == 0:
                # 整组为零：只在它后面还有非零组时补一个"零"
                if parts and any(groups[:gi]):
                    parts.append("零")
                continue
            seg, pending_zero = "", False
            for pos in range(3, -1, -1):
                d = (g // 10 ** pos) % 10
                if d == 0:
                    pending_zero = True
                else:
                    if pending_zero and seg:
                        seg += "零"
                    pending_zero = False
                    seg += CN_DIGITS[d] + CN_UNITS[pos]
            # 低位组不足 4 位时，前面要补一个"零"（1000005 -> 壹佰万零伍）
            if gi < len(groups) - 1 and g < 1000:
                seg = "零" + seg
            parts.append(seg + CN_GROUPS[gi])
        head = "".join(parts) + "元"

    if jiao == 0 and fen == 0:
        return head + "整"
    tail = (CN_DIGITS[jiao] + "角" if jiao else "零") + (CN_DIGITS[fen] + "分" if fen else "")
    if jiao == 0 and fen:
        tail = "零" + CN_DIGITS[fen] + "分"
    return head + tail


def reg_fee_items(reg_id, cur=None):
    """算这次挂号该收的钱：挂号费 + 处方里的药费，返回 (明细列表, 药费合计)

    查病历和处方明细。如果没有处方（医生没开药），就只有挂号费。
    """
    sql_reg = "SELECT reg_fee, doctor_id FROM registration WHERE reg_id = %s"
    sql_items = """
        SELECT pd.medicine_id, pd.quantity, m.medicine_name, m.spec, m.unit, m.price
        FROM medical_record mr
        JOIN prescription_detail pd ON pd.medicine_id IS NOT NULL
             AND pd.record_id = mr.record_id
        JOIN medicine m ON pd.medicine_id = m.medicine_id
        WHERE mr.reg_id = %s
        ORDER BY pd.detail_id
    """
    if cur is not None:
        cur.execute(sql_reg, (reg_id,))
        reg = cur.fetchone()
        cur.execute(sql_items, (reg_id,))
        drugs = cur.fetchall()
    else:
        reg = query_one(sql_reg, (reg_id,))
        drugs = query_all(sql_items, (reg_id,))

    items = []
    if reg:
        items.append({
            "item_type": "挂号费", "medicine_id": None,
            "item_name": "门诊挂号费", "unit_price": float(reg["reg_fee"]),
            "quantity": 1, "amount": round(float(reg["reg_fee"]), 2),
        })
    drug_fee = 0.0
    for d in drugs:
        amount = round(float(d["price"]) * d["quantity"], 2)
        drug_fee += amount
        items.append({
            "item_type": "药品费", "medicine_id": d["medicine_id"],
            "item_name": d["medicine_name"] + ("（%s）" % d["spec"] if d["spec"] else ""),
            "unit_price": float(d["price"]), "quantity": d["quantity"], "amount": amount,
        })
    return items, round(drug_fee, 2)


def next_charge_no(cur):
    """生成收费单号：SF + 日期 + 当天第几单，如 SF202610140001"""
    prefix = CHARGE_SEQ_PREFIX + date.today().strftime("%Y%m%d")
    cur.execute("SELECT COUNT(*) AS n FROM charge WHERE charge_no LIKE %s", (prefix + "%",))
    return "%s%04d" % (prefix, cur.fetchone()["n"] + 1)


def next_admission_no(cur):
    """生成住院号：ZY + 日期 + 当天第几条，如 ZY202610140001"""
    prefix = ADMISSION_SEQ_PREFIX + date.today().strftime("%Y%m%d")
    cur.execute("SELECT COUNT(*) AS n FROM admission WHERE admission_no LIKE %s",
                (prefix + "%",))
    return "%s%04d" % (prefix, cur.fetchone()["n"] + 1)


def in_ward_of(pid):
    """这位患者当前的在院记录（没有则返回 None）"""
    return query_one(
        "SELECT admission_id, admission_no, bed_no, admit_time "
        "FROM admission WHERE patient_id = %s AND status = '在院'",
        (pid,),
    )


def global_counts():
    """门户页和概览页共用的几个关键数字"""
    row = query_one(
        """
        SELECT
            (SELECT COUNT(*) FROM patient) AS patient_count,
            (SELECT COUNT(*) FROM doctor)  AS doctor_count,
            (SELECT COUNT(*) FROM registration) AS reg_count,
            (SELECT COUNT(*) FROM registration WHERE DATE(reg_date) = CURDATE()) AS today_reg,
            (SELECT COUNT(*) FROM registration WHERE reg_status = '已挂号') AS waiting,
            (SELECT COUNT(*) FROM registration WHERE reg_status = '已就诊') AS to_charge,
            (SELECT COUNT(*) FROM registration WHERE reg_status = '已收费') AS to_dispense,
            (SELECT COUNT(*) FROM doctor_schedule
              WHERE work_date = CURDATE() AND status = '正常') AS today_sched,
            (SELECT COUNT(*) FROM admission WHERE status = '在院') AS in_ward,
            (SELECT COUNT(*) FROM admission WHERE DATE(admit_time) = CURDATE()) AS today_admit,
            (SELECT COUNT(*) FROM admission
              WHERE discharge_time IS NOT NULL AND DATE(discharge_time) = CURDATE()) AS today_discharge,
            (SELECT COUNT(*) FROM admission) AS adm_count,
            (SELECT COUNT(*) FROM medicine WHERE stock < %s) AS low_stock,
            (SELECT COUNT(*) FROM medicine) AS medicine_count,
            (SELECT COALESCE(SUM(total_fee), 0) FROM charge
              WHERE charge_status = '已收费' AND DATE(charge_time) = CURDATE()) AS today_income,
            (SELECT COALESCE(SUM(total_fee), 0) FROM charge
              WHERE charge_status = '已收费') AS total_income
        """,
        (LOW_STOCK,),
    )
    row["today_income"] = float(row["today_income"])
    row["total_income"] = float(row["total_income"])
    return row


def build_xlsx(sheet_title, headers, rows, widths=None):
    """把二维数据写成 Excel（返回内存中的文件对象）"""
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_title

    ws.append(headers)
    head_fill = PatternFill("solid", fgColor="2D6A4F")
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = head_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for row in rows:
        ws.append(list(row))

    for i, w in enumerate(widths or [], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    ws.freeze_panes = "A2"   # 冻结表头
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# ---------------- 首页：系统门户 ----------------
@app.route("/")
def home():
    counts = global_counts()
    return render_template("home.html", counts=counts)


# ---------------- 数据概览 ----------------
@app.route("/dashboard")
def dashboard():
    counts = global_counts()

    # 各科室挂号量（多表联查 + 分组统计）
    dept_stats = query_all(
        """
        SELECT d.dept_name, COUNT(r.reg_id) AS cnt
        FROM department d
        LEFT JOIN registration r ON d.dept_id = r.dept_id
        GROUP BY d.dept_id, d.dept_name
        ORDER BY cnt DESC
        """
    )
    max_cnt = max([d["cnt"] for d in dept_stats], default=0)
    for d in dept_stats:
        d["percent"] = round(d["cnt"] / max_cnt * 100) if max_cnt else 0

    # 挂号状态分布
    status_stats = query_all(
        """
        SELECT reg_status, COUNT(*) AS cnt
        FROM registration
        GROUP BY reg_status
        ORDER BY cnt DESC
        """
    )
    total_reg = sum(s["cnt"] for s in status_stats) or 1
    for s in status_stats:
        s["percent"] = round(s["cnt"] / total_reg * 100, 1)

    # 初诊 / 复诊占比
    visit_stats = query_all(
        """
        SELECT visit_type, COUNT(*) AS cnt
        FROM registration
        GROUP BY visit_type
        ORDER BY cnt DESC
        """
    )

    # 医生接诊量排行（多表联查 + 分组 + 排序 + 取前 N）
    doctor_rank = query_all(
        """
        SELECT doc.doctor_name, doc.title, d.dept_name, COUNT(r.reg_id) AS cnt
        FROM doctor doc
        LEFT JOIN department d   ON doc.dept_id = d.dept_id
        LEFT JOIN registration r ON doc.doctor_id = r.doctor_id
        GROUP BY doc.doctor_id, doc.doctor_name, doc.title, d.dept_name
        ORDER BY cnt DESC, doc.doctor_id
        LIMIT 8
        """
    )

    # 药品库存预警（库存低于阈值）
    low_stock = query_all(
        """
        SELECT medicine_id, medicine_name, spec, stock, unit
        FROM medicine
        WHERE stock < %s
        ORDER BY stock ASC
        """,
        (LOW_STOCK,),
    )
    for m in low_stock:
        m["level"] = stock_level(m["stock"])

    # 最近 5 条挂号
    latest_regs = query_all(
        """
        SELECT r.reg_id, r.reg_date, r.reg_status, r.visit_type,
               p.patient_name, doc.doctor_name, d.dept_name
        FROM registration r
        JOIN patient    p   ON r.patient_id = p.patient_id
        JOIN doctor     doc ON r.doctor_id  = doc.doctor_id
        JOIN department d   ON r.dept_id    = d.dept_id
        ORDER BY r.reg_date DESC, r.reg_id DESC
        LIMIT 5
        """
    )

    # ---- 收费与收入 ----
    income_stat = query_one(
        """
        SELECT
            COALESCE(SUM(CASE WHEN charge_status = '已收费' THEN total_fee END), 0) AS total_income,
            COALESCE(SUM(CASE WHEN charge_status = '已收费' THEN reg_fee END), 0)   AS reg_income,
            COALESCE(SUM(CASE WHEN charge_status = '已收费' THEN drug_fee END), 0)  AS drug_income,
            COALESCE(SUM(CASE WHEN charge_status = '已收费' AND DATE(charge_time) = CURDATE()
                              THEN total_fee END), 0) AS today_income,
            COALESCE(SUM(CASE WHEN charge_status = '已退费' THEN total_fee END), 0) AS refund_income,
            SUM(charge_status = '已退费') AS refund_cnt,
            COUNT(*) AS charge_cnt
        FROM charge
        """
    )
    for k in ("total_income", "reg_income", "drug_income", "today_income", "refund_income"):
        income_stat[k] = float(income_stat[k] or 0)

    pay_stats = query_all(
        """
        SELECT pay_method, COUNT(*) AS cnt, COALESCE(SUM(total_fee), 0) AS amount
        FROM charge WHERE charge_status = '已收费'
        GROUP BY pay_method ORDER BY amount DESC
        """
    )
    for p in pay_stats:
        p["amount"] = float(p["amount"])

    # ---- 图表数据（交给前端 ECharts 画图）----
    # 近 30 天挂号趋势：SQL 只返回有记录的日期，缺口在 Python 里补 0
    trend_rows = query_all(
        """
        SELECT DATE(reg_date) AS d, COUNT(*) AS n
        FROM registration
        WHERE reg_date >= CURDATE() - INTERVAL 29 DAY
        GROUP BY DATE(reg_date)
        """
    )
    trend_map = {str(r["d"]): r["n"] for r in trend_rows}
    trend_labels, trend_values = [], []
    for i in range(29, -1, -1):
        day = date.today() - timedelta(days=i)
        trend_labels.append(day.strftime("%m-%d"))
        trend_values.append(int(trend_map.get(str(day), 0)))

    # 近 30 天收入趋势（同一天可能有好几张收费单，先按天 SUM）
    income_rows = query_all(
        """
        SELECT DATE(charge_time) AS d, COALESCE(SUM(total_fee), 0) AS n
        FROM charge
        WHERE charge_status = '已收费' AND charge_time >= CURDATE() - INTERVAL 29 DAY
        GROUP BY DATE(charge_time)
        """
    )
    income_map = {str(r["d"]): float(r["n"]) for r in income_rows}
    income_values = [round(income_map.get(str(date.today() - timedelta(days=i)), 0.0), 2)
                     for i in range(29, -1, -1)]

    # 各科室今日 / 累计对比（取挂号量前 8 的科室）
    dept_top = dept_stats[:8]
    today_by_dept = query_all(
        """
        SELECT dept_id, COUNT(*) AS n
        FROM registration
        WHERE DATE(reg_date) = CURDATE()
        GROUP BY dept_id
        """
    )
    today_map = {r["dept_id"]: r["n"] for r in today_by_dept}
    dept_today = query_all("SELECT dept_id, dept_name FROM department")
    dept_id_map = {d["dept_name"]: d["dept_id"] for d in dept_today}
    dept_chart = {
        "labels": [d["dept_name"] for d in dept_top],
        "total": [int(d["cnt"]) for d in dept_top],
        "today": [int(today_map.get(dept_id_map.get(d["dept_name"]), 0)) for d in dept_top],
    }

    chart_data = {
        "trend": {"labels": trend_labels, "values": trend_values},
        "income": {"labels": trend_labels, "values": income_values},
        "pay": {
            "labels": [p["pay_method"] for p in pay_stats],
            "values": [float(p["amount"]) for p in pay_stats],
        },
        "dept": dept_chart,
        "status": {
            "labels": [s["reg_status"] for s in status_stats],
            "values": [int(s["cnt"]) for s in status_stats],
        },
        "visit": {
            "labels": [v["visit_type"] for v in visit_stats],
            "values": [int(v["cnt"]) for v in visit_stats],
        },
        "doctor": {
            "labels": [d["doctor_name"] for d in doctor_rank],
            "values": [int(d["cnt"]) for d in doctor_rank],
        },
    }

    return render_template(
        "index.html",
        counts=counts, dept_stats=dept_stats, status_stats=status_stats,
        visit_stats=visit_stats, doctor_rank=doctor_rank,
        low_stock=low_stock, latest_regs=latest_regs,
        income_stat=income_stat, pay_stats=pay_stats,
        chart_json=json_for_script(chart_data),
        low_stock_limit=LOW_STOCK,
    )


# ---------------- 挂号列表：科室 / 状态 / 类型筛选 + 搜索 + 分页 ----------------
def build_reg_filter():
    """挂号列表的筛选条件（列表页和导出 Excel 共用，避免两处写法不一致）"""
    dept_id = request.args.get("dept_id", type=int)
    status = (request.args.get("status") or "").strip()
    vtype = (request.args.get("vtype") or "").strip()
    kw = clean_kw(request.args.get("kw"))

    where, args = [], []
    if dept_id:
        where.append("r.dept_id = %s")
        args.append(dept_id)
    if status in REG_STATUSES:
        where.append("r.reg_status = %s")
        args.append(status)
    if vtype in ("初诊", "复诊"):
        where.append("r.visit_type = %s")
        args.append(vtype)
    if kw:
        where.append("(p.patient_name LIKE %s OR doc.doctor_name LIKE %s)")
        args += ["%" + kw + "%", "%" + kw + "%"]

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    return {
        "dept_id": dept_id, "status": status, "vtype": vtype, "kw": kw,
        "where_sql": where_sql, "args": tuple(args),
    }


REG_SELECT = """
    SELECT r.reg_id, r.reg_date, r.reg_fee, r.reg_status, r.visit_type,
           r.parent_reg_id, r.time_period, r.queue_no, r.called_time,
           s.work_date, s.status AS sched_status,
           p.patient_id, p.patient_name, p.gender, p.birth_date, p.phone,
           doc.doctor_name, doc.title,
           d.dept_id, d.dept_name,
           (SELECT COUNT(*) FROM prescription_detail pd
              JOIN medical_record mr ON pd.record_id = mr.record_id
             WHERE mr.reg_id = r.reg_id) AS med_cnt
    FROM registration r
    JOIN patient    p   ON r.patient_id = p.patient_id
    JOIN doctor     doc ON r.doctor_id  = doc.doctor_id
    JOIN department d   ON r.dept_id    = d.dept_id
    LEFT JOIN doctor_schedule s ON r.schedule_id = s.schedule_id
"""


@app.route("/registrations")
def registrations():
    f = build_reg_filter()
    highlight = request.args.get("highlight", type=int)   # 刚新增/刚改过的那条
    page = request.args.get("page", type=int) or 1

    total = query_one(
        f"""
        SELECT COUNT(*) AS n
        FROM registration r
        JOIN patient p   ON r.patient_id = p.patient_id
        JOIN doctor  doc ON r.doctor_id  = doc.doctor_id
        {f['where_sql']}
        """,
        f["args"],
    )["n"]

    page, total_pages, offset = paginate(total, page)

    rows = query_all(
        f"""
        {REG_SELECT}
        {f['where_sql']}
        ORDER BY r.reg_date DESC, r.reg_id DESC
        LIMIT %s OFFSET %s
        """,
        f["args"] + (PAGE_SIZE, offset),
    )
    for r in rows:
        r["age"] = calc_age(r["birth_date"])
        r["phone_mask"] = mask_phone(r["phone"])

    depts = query_all("SELECT dept_id, dept_name FROM department ORDER BY dept_id")

    # 各状态的条数，显示在筛选按钮上
    status_counts = {row["reg_status"]: row["n"] for row in query_all(
        "SELECT reg_status, COUNT(*) AS n FROM registration GROUP BY reg_status"
    )}
    type_counts = {row["visit_type"]: row["n"] for row in query_all(
        "SELECT visit_type, COUNT(*) AS n FROM registration GROUP BY visit_type"
    )}

    base_args_url = {"dept_id": f["dept_id"], "status": f["status"],
                     "vtype": f["vtype"], "kw": f["kw"]}
    pager = page_urls("registrations", {**base_args_url, "highlight": highlight},
                      total_pages, page)
    # 导出 Excel 时带上当前筛选条件（导出的是"筛选后的结果"而不是全部）
    export_url = url_for("export_registrations", **clean_args(base_args_url))
    # Jinja2 不支持 **kwargs 解包，所以上一页/下一页链接在这里拼好
    prev_url = url_for("registrations",
                       **clean_args({**base_args_url, "page": page - 1})) if page > 1 else None
    next_url = url_for("registrations",
                       **clean_args({**base_args_url, "page": page + 1})) if page < total_pages else None

    return render_template(
        "registrations.html",
        rows=rows, depts=depts, current_dept=f["dept_id"], current_status=f["status"],
        current_vtype=f["vtype"], kw=f["kw"], highlight=highlight,
        total=total, page=page, total_pages=total_pages, pager=pager,
        prev_url=prev_url, next_url=next_url, export_url=export_url,
        status_counts=status_counts, type_counts=type_counts,
    )


# ---------------- 挂号状态流转：已挂号 → 已取消 / 已就诊（不开处方时的快捷通道）----------------
@app.route("/registration/<int:rid>/status", methods=["POST"])
def change_reg_status(rid):
    target = request.form.get("status", "")
    back = safe_back()

    if target not in STATUS_TARGETS:
        flash("不支持的状态变更", "error")
        return redirect(back)

    try:
        with transaction() as cur:
            # 条件更新：只有当前仍是"已挂号"才允许改。
            # 这样即使重复点击或两个人同时操作，也不会把已经处理过的记录改乱。
            cur.execute(
                "UPDATE registration SET reg_status = %s "
                "WHERE reg_id = %s AND reg_status = '已挂号'",
                (target, rid),
            )
            affected = cur.rowcount
            if affected and target == "已取消":
                # 取消挂号要把占用的号源释放回去，这个号才能再被别人挂走
                cur.execute(
                    """
                    UPDATE doctor_schedule s
                    JOIN registration r ON r.schedule_id = s.schedule_id
                       SET s.booked_count = CASE WHEN s.booked_count > 0
                                                 THEN s.booked_count - 1 ELSE 0 END
                     WHERE r.reg_id = %s
                    """,
                    (rid,),
                )
    except Exception as e:
        flash("状态变更失败：%s" % e, "error")
        return redirect(back)

    if affected:
        log_action("状态变更", "挂号 #%d" % rid, "状态变更为「%s」" % target)
        flash("挂号编号 %s 已变更为「%s」%s"
              % (rid, target, "，占用的号源已释放" if target == "已取消" else ""), "ok")
    else:
        flash("操作未生效：该挂号可能已被处理过，请刷新后查看", "error")

    sep = "&" if "?" in back else "?"
    return redirect(back + sep + "highlight=%d" % rid)


# ---------------- 排班号源：按天看每位医生的号源与余量 ----------------
def _parse_day(raw, fallback=None):
    """把 ?work_date=2026-10-14 解析成 date，解析不了就用今天"""
    try:
        return datetime.strptime((raw or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return fallback or date.today()


@app.route("/schedules")
def schedules():
    wd = _parse_day(request.args.get("work_date"))
    dept_id = request.args.get("dept_id", type=int)
    status = (request.args.get("status") or "").strip()

    where, args = ["s.work_date = %s"], [wd]
    if dept_id:
        where.append("doc.dept_id = %s")
        args.append(dept_id)
    if status in SCHEDULE_STATUSES:
        where.append("s.status = %s")
        args.append(status)
    where_sql = "WHERE " + " AND ".join(where)

    rows = query_all(
        f"""
        SELECT s.schedule_id, s.work_date, s.time_period, s.total_quota,
               s.booked_count, s.status,
               doc.doctor_id, doc.doctor_name, doc.title,
               d.dept_id, d.dept_name
        FROM doctor_schedule s
        JOIN doctor doc ON s.doctor_id = doc.doctor_id
        LEFT JOIN department d ON doc.dept_id = d.dept_id
        {where_sql}
        ORDER BY doc.dept_id, doc.doctor_id, s.time_period
        """,
        tuple(args),
    )
    for r in rows:
        r["left"] = max(0, r["total_quota"] - r["booked_count"])
        r["rate"] = round(r["booked_count"] * 100 / r["total_quota"]) if r["total_quota"] else 0

    summary = query_one(
        f"""
        SELECT COUNT(*) AS cnt,
               COALESCE(SUM(s.total_quota), 0)  AS quota,
               COALESCE(SUM(s.booked_count), 0) AS booked,
               COALESCE(SUM(s.status = '停诊'), 0) AS stopped
        FROM doctor_schedule s
        JOIN doctor doc ON s.doctor_id = doc.doctor_id
        {where_sql}
        """,
        tuple(args),
    )
    summary["left"] = max(0, summary["quota"] - summary["booked"])

    return render_template(
        "schedules.html", rows=rows, summary=summary,
        depts=query_all("SELECT dept_id, dept_name FROM department ORDER BY dept_id"),
        # "新增排班"的候选医生只列在职的；离职医生的历史排班仍留在上面的列表里
        docs=query_all(
            """
            SELECT doc.doctor_id, doc.doctor_name, doc.title, d.dept_name
            FROM doctor doc LEFT JOIN department d ON doc.dept_id = d.dept_id
            WHERE doc.status = '在职'
            ORDER BY doc.dept_id, doc.doctor_id
            """),
        current_dept=dept_id, current_status=status, current_date=str(wd),
        today=date.today().isoformat(),
        time_periods=TIME_PERIODS, schedule_statuses=SCHEDULE_STATUSES,
    )


@app.route("/schedule/add", methods=["POST"])
def schedule_add():
    doctor_id = request.form.get("doctor_id", type=int)
    wd = _parse_day(request.form.get("work_date"), fallback=None)
    period = (request.form.get("time_period") or "").strip()
    quota = request.form.get("total_quota", type=int)
    back = safe_back("schedules")

    if not doctor_id or period not in TIME_PERIODS:
        flash("请选择医生与时段", "error")
        return redirect(back)
    if wd is None or wd < date.today():
        flash("只能为今天及以后的日期排班", "error")
        return redirect(back)
    if not quota or quota <= 0 or quota > 200:
        flash("号源数量要在 1 ~ 200 之间", "error")
        return redirect(back)

    # 离职医生不能再排新班（前端下拉已过滤，这里防绕过直接提交）
    doc = query_one("SELECT doctor_name, status FROM doctor WHERE doctor_id = %s", (doctor_id,))
    if doc is None:
        flash("该医生不存在", "error")
        return redirect(back)
    if doc["status"] != "在职":
        flash("「%s」已离职，不能再排班" % doc["doctor_name"], "error")
        return redirect(back)

    try:
        execute(
            "INSERT INTO doctor_schedule (doctor_id, work_date, time_period, total_quota) "
            "VALUES (%s, %s, %s, %s)",
            (doctor_id, wd, period, quota),
        )
        log_action("新增排班", "排班 %s %s" % (wd, period),
                   "医生 #%d，放号 %d 个" % (doctor_id, quota))
        flash("已为医生 #%d 添加 %s %s 的排班，放号 %d 个" % (doctor_id, wd, period, quota), "ok")
    except pymysql.err.IntegrityError:
        flash("该医生这一天的这个时段已经有排班了，不能重复添加（可直接改号源）", "error")
    except Exception as e:
        flash("新增排班失败：%s" % e, "error")
    return redirect(back)


@app.route("/schedule/<int:sid>/toggle", methods=["POST"])
def schedule_toggle(sid):
    """停诊 / 恢复出诊。已经挂了号的班次不允许直接停诊。"""
    back = safe_back("schedules")
    row = query_one("SELECT status, booked_count FROM doctor_schedule WHERE schedule_id = %s", (sid,))
    if row is None:
        flash("排班不存在", "error")
        return redirect(back)

    if row["status"] == "正常":
        if row["booked_count"] > 0:
            flash("这个班次已经挂了 %d 个号，不能直接停诊；请先取消这些挂号" % row["booked_count"], "error")
            return redirect(back)
        new_status = "停诊"
    else:
        new_status = "正常"

    execute("UPDATE doctor_schedule SET status = %s WHERE schedule_id = %s", (new_status, sid))
    log_action("排班变更", "排班 #%d" % sid, "状态改为「%s」" % new_status)
    flash("排班 #%d 已%s" % (sid, "停诊" if new_status == "停诊" else "恢复出诊"), "ok")
    return redirect(back)


@app.route("/schedule/<int:sid>/quota", methods=["POST"])
def schedule_quota(sid):
    """调整号源数量：不能小于已经挂出的号数"""
    quota = request.form.get("total_quota", type=int)
    back = safe_back("schedules")
    if not quota or quota <= 0 or quota > 200:
        flash("号源数量要在 1 ~ 200 之间", "error")
        return redirect(back)

    row = query_one("SELECT booked_count FROM doctor_schedule WHERE schedule_id = %s", (sid,))
    if row is None:
        flash("排班不存在", "error")
        return redirect(back)
    if quota < row["booked_count"]:
        flash("已经挂出 %d 个号，号源不能调到比它小" % row["booked_count"], "error")
        return redirect(back)

    execute("UPDATE doctor_schedule SET total_quota = %s WHERE schedule_id = %s", (quota, sid))
    log_action("排班变更", "排班 #%d" % sid, "号源调整为 %d" % quota)
    flash("排班 #%d 的号源已调整为 %d" % (sid, quota), "ok")
    return redirect(back)


# ---------------- 候诊叫号：看某天某时段各医生的候诊队列 ----------------
@app.route("/queue")
def queue():
    wd = _parse_day(request.args.get("work_date"))
    period = (request.args.get("period") or "").strip()
    dept_id = request.args.get("dept_id", type=int)

    where = ["r.reg_status = '已挂号'", "s.work_date = %s"]
    args = [wd]
    if period in TIME_PERIODS:
        where.append("r.time_period = %s")
        args.append(period)
    if dept_id:
        where.append("r.dept_id = %s")
        args.append(dept_id)

    rows = query_all(
        f"""
        SELECT r.reg_id, r.reg_date, r.queue_no, r.time_period, r.called_time,
               r.visit_type, r.parent_reg_id,
               p.patient_id, p.patient_name, p.gender, p.birth_date,
               doc.doctor_id, doc.doctor_name, doc.title,
               d.dept_id, d.dept_name
        FROM registration r
        JOIN doctor_schedule s ON r.schedule_id = s.schedule_id
        JOIN patient    p   ON r.patient_id = p.patient_id
        JOIN doctor     doc ON r.doctor_id  = doc.doctor_id
        JOIN department d   ON r.dept_id    = d.dept_id
        WHERE {" AND ".join(where)}
        ORDER BY doc.dept_id, doc.doctor_id, r.queue_no
        """,
        tuple(args),
    )
    for r in rows:
        r["age"] = calc_age(r["birth_date"])

    # 每位医生的队列：第 1 个还没叫号的算"下一位"
    groups = []
    by_doc = {}
    for r in rows:
        key = r["doctor_id"]
        if key not in by_doc:
            by_doc[key] = {"doctor_id": key, "doctor_name": r["doctor_name"],
                           "title": r["title"] or "", "dept_name": r["dept_name"],
                           "rows": [], "called": 0}
            groups.append(by_doc[key])
        by_doc[key]["rows"].append(r)
        if r["called_time"]:
            by_doc[key]["called"] += 1
    for g in groups:
        g["next_id"] = g["next_no"] = None
        for r in g["rows"]:
            if r["called_time"] is None:
                g["next_id"] = r["reg_id"]
                g["next_no"] = r["queue_no"]
                break

    # 刚叫的那一位：叫号后页面会弹一个大窗口把患者"喊"出来。
    # 信息在这里查好传过去，模板里就不用再从表格里翻数据了。
    called_info = None
    called_id = request.args.get("called", type=int)
    if called_id:
        called_info = query_one(
            """
            SELECT r.reg_id, r.queue_no, r.called_time, r.time_period, r.visit_type,
                   p.patient_id, p.patient_name, p.gender, p.birth_date,
                   doc.doctor_name, doc.title, d.dept_name
            FROM registration r
            JOIN patient    p   ON r.patient_id = p.patient_id
            JOIN doctor     doc ON r.doctor_id  = doc.doctor_id
            JOIN department d   ON r.dept_id    = d.dept_id
            WHERE r.reg_id = %s
            """,
            (called_id,),
        )
        # 只有"确实叫过号"的挂号才弹窗。地址栏是可以手敲的：带 ?called= 打开一条
        # 从没叫过号的记录（书签、浏览器前进后退、直接改 URL）时，
        # 既不该弹一个"正在叫号"的窗口误导人，也不该让 called_time 为 NULL 的
        # 值一路走到模板里做 .strftime() 而 500。
        if called_info and not called_info["called_time"]:
            called_info = None
        if called_info:
            called_info["age"] = calc_age(called_info["birth_date"])

    return render_template(
        "queue.html", groups=groups, waiting=len(rows),
        called=sum(g["called"] for g in groups),
        called_info=called_info,
        back_url=url_for("queue", work_date=str(wd),
                         dept_id=dept_id, period=period or None),
        depts=query_all("SELECT dept_id, dept_name FROM department ORDER BY dept_id"),
        current_dept=dept_id, current_period=period, current_date=str(wd),
        today=date.today().isoformat(), time_periods=TIME_PERIODS,
    )


@app.route("/queue/<int:rid>/call", methods=["POST"])
def queue_call(rid):
    """叫号：把这条挂号的 called_time 刷新成此刻。

    页面那边会根据跳转 URL 上的 ?called=<id> 弹出大号叫号窗口；
    这里的 flash 是"万一把 JS 关了"时的降级提示。
    """
    back = safe_back("queue")
    affected = execute(
        "UPDATE registration SET called_time = NOW() "
        "WHERE reg_id = %s AND reg_status = '已挂号'",
        (rid,),
    )
    if affected:
        log_action("叫号", "挂号 #%d" % rid, "候诊队列叫号")
        flash("已叫号：请挂号 #%d 的患者就诊" % rid, "ok")
        # 回列表页时带上被叫的那条，页面据此弹窗
        return redirect(back + ("&" if "?" in back else "?") + "called=%d" % rid)
    flash("叫号未生效：该挂号可能已经就诊或已取消", "error")
    return redirect(back)


# ---------------- 接诊开处方：就诊 + 开处方 + 扣库存（一个事务）----------------
@app.route("/visit/<int:reg_id>", methods=["GET", "POST"])
def visit(reg_id):
    reg = query_one(
        """
        SELECT r.reg_id, r.reg_date, r.reg_status, r.visit_type, r.reg_fee,
               p.patient_id, p.patient_name, p.gender, p.birth_date, p.phone,
               doc.doctor_id, doc.doctor_name, doc.title, doc.specialty,
               d.dept_name
        FROM registration r
        JOIN patient    p   ON r.patient_id = p.patient_id
        JOIN doctor     doc ON r.doctor_id  = doc.doctor_id
        JOIN department d   ON r.dept_id    = d.dept_id
        WHERE r.reg_id = %s
        """,
        (reg_id,),
    )
    if reg is None:
        abort(404)
    reg["age"] = calc_age(reg["birth_date"])

    # 开处方的候选药品只列"在用"的：停用的药开不出新处方，
    # 但已开出的处方与收费快照照常显示，历史账目不受影响。
    medicines = query_all(
        """
        SELECT medicine_id, medicine_name, spec, unit, price, stock
        FROM medicine WHERE status = '在用'
        ORDER BY medicine_id
        """
    )
    for m in medicines:
        m["level"] = stock_level(m["stock"])
        # price 从数据库取出来是 Decimal，不能直接转 JSON，先转成 float
        m["price"] = float(m["price"])

    if request.method == "POST":
        diagnosis = (request.form.get("diagnosis") or "").strip()
        remark = (request.form.get("remark") or "").strip()
        med_ids = request.form.getlist("medicine_id")
        qtys = request.form.getlist("quantity")
        dosages = request.form.getlist("dosage")

        # 原始处方行（用于提交失败时回填页面，不让用户白填一遍）
        rx_rows = []
        for i, mid in enumerate(med_ids):
            rx_rows.append({
                "medicine_id": mid or "",
                "quantity": qtys[i] if i < len(qtys) else "1",
                "dosage": dosages[i] if i < len(dosages) else "",
            })

        # 把表单里的处方行整理成 [(medicine_id, 数量, 用法)]
        items = []
        for i, mid in enumerate(med_ids):
            if not mid or mid == "0":
                continue
            try:
                qty = int(qtys[i]) if i < len(qtys) and qtys[i] else 0
            except (TypeError, ValueError):
                qty = 0
            if qty <= 0:
                continue
            dosage = (dosages[i] if i < len(dosages) else "").strip()
            items.append((int(mid), qty, dosage))

        # 同一药品重复选了就合并数量，避免出现两条一样明细
        merged = {}
        for mid, qty, dosage in items:
            if mid in merged:
                merged[mid][0] += qty
            else:
                merged[mid] = [qty, dosage]
        items = [(mid, v[0], v[1]) for mid, v in merged.items()]

        if not diagnosis:
            flash("请先填写诊断结果", "error")
            return render_template("visit.html", reg=reg, medicines=medicines,
                                   medicines_json=json_for_script(medicines),
                                   form=request.form, rx_rows=rx_rows)

        try:
            # 关键：这一整段放在一个事务里。
            # 只要有任何一步失败（比如某味药库存不够），前面写进去的东西全部回滚。
            with transaction() as cur:
                cur.execute(
                    "SELECT reg_status FROM registration WHERE reg_id = %s FOR UPDATE",
                    (reg_id,),
                )
                cur_row = cur.fetchone()
                if cur_row is None:
                    raise BizError("挂号记录不存在")
                if cur_row["reg_status"] != "已挂号":
                    raise BizError("该挂号当前状态为「%s」，不能重复接诊" % cur_row["reg_status"])

                summary_parts = []
                # 先校验库存（把药锁住），再统一写入，避免写一半发现不够
                checked = []
                for med_id, qty, dosage in items:
                    cur.execute(
                        "SELECT medicine_name, spec, stock, unit, status FROM medicine "
                        "WHERE medicine_id = %s FOR UPDATE",
                        (med_id,),
                    )
                    med = cur.fetchone()
                    if med is None:
                        raise BizError("药品编号 %s 不存在" % med_id)
                    # 停用的药品不能再开新处方（前端下拉已过滤，这里防绕过直接提交）
                    if med["status"] != "在用":
                        raise BizError("「%s」已停用，不能再开处方" % med["medicine_name"])
                    if med["stock"] < qty:
                        raise BizError("「%s」库存不足：现有 %d%s，本次需要 %d%s，请先入库"
                                       % (med["medicine_name"], med["stock"], med["unit"] or "",
                                          qty, med["unit"] or ""))
                    checked.append((med_id, qty, dosage, med))
                    summary_parts.append("%s×%d %s" % (med["medicine_name"], qty, dosage))

                # 1) 写病历
                cur.execute(
                    """
                    INSERT INTO medical_record
                        (patient_id, doctor_id, reg_id, visit_date, diagnosis,
                         prescription, remark)
                    VALUES (%s, %s, %s, NOW(), %s, %s, %s)
                    """,
                    (reg["patient_id"], reg["doctor_id"], reg_id, diagnosis,
                     "；".join(summary_parts), remark),
                )
                record_id = cur.lastrowid

                # 2) 写处方明细 + 扣库存 + 记一条出库流水
                for med_id, qty, dosage, med in checked:
                    cur.execute(
                        """
                        INSERT INTO prescription_detail
                            (record_id, medicine_id, quantity, dosage)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (record_id, med_id, qty, dosage),
                    )
                    before = med["stock"]
                    after = before - qty
                    cur.execute(
                        "UPDATE medicine SET stock = %s WHERE medicine_id = %s",
                        (after, med_id),
                    )
                    cur.execute(
                        """
                        INSERT INTO medicine_stock_log
                            (medicine_id, change_type, quantity, before_stock,
                             after_stock, reason, create_time)
                        VALUES (%s, '出库', %s, %s, %s, %s, NOW())
                        """,
                        (med_id, qty, before, after,
                         "处方扣减（病历 #%d / 挂号 #%d）" % (record_id, reg_id)),
                    )

                # 3) 挂号状态改成已就诊
                cur.execute(
                    "UPDATE registration SET reg_status = '已就诊' "
                    "WHERE reg_id = %s AND reg_status = '已挂号'",
                    (reg_id,),
                )
                if cur.rowcount == 0:
                    raise BizError("挂号状态已变化，请刷新后重试")

            log_action("接诊开方", "病历 #%d" % record_id,
                       "挂号 #%d，诊断：%s，开具 %d 种药品并扣减库存"
                       % (reg_id, diagnosis, len(items)))
            if items:
                flash("接诊完成：已写入病历 #%s，开具 %d 种药品并扣减库存，挂号 #%s 状态已更新为「已就诊」"
                      % (record_id, len(items), reg_id), "ok")
            else:
                flash("接诊完成：已写入病历 #%s（未开处方），挂号 #%s 状态已更新为「已就诊」"
                      % (record_id, reg_id), "ok")
            return redirect(url_for("registrations", highlight=reg_id))

        except BizError as e:
            flash(str(e), "error")
        except Exception as e:                      # 兜底：数据库层异常也不能白屏
            flash("接诊失败：%s" % e, "error")

        return render_template("visit.html", reg=reg, medicines=medicines,
                               medicines_json=json_for_script(medicines),
                               form=request.form, rx_rows=rx_rows)

    # GET：如果这条挂号已经有病历了，提示一下
    exist = query_one(
        "SELECT record_id, diagnosis FROM medical_record WHERE reg_id = %s LIMIT 1", (reg_id,)
    )
    return render_template("visit.html", reg=reg, medicines=medicines,
                           medicines_json=json_for_script(medicines),
                           form=None, exist_record=exist, rx_rows=[])


# ---------------- 收费：记录列表 ----------------
def build_charge_filter():
    """收费列表的筛选条件（列表页与导出 Excel 共用）"""
    status = (request.args.get("status") or "").strip()
    pay = (request.args.get("pay") or "").strip()
    kw = clean_kw(request.args.get("kw"))

    where, args = [], []
    if status in ("已收费", "已退费"):
        where.append("c.charge_status = %s")
        args.append(status)
    if pay in PAY_METHODS:
        where.append("c.pay_method = %s")
        args.append(pay)
    if kw:
        where.append("(c.charge_no LIKE %s OR p.patient_name LIKE %s)")
        args += ["%" + kw + "%", "%" + kw + "%"]

    return {
        "status": status, "pay": pay, "kw": kw,
        "where_sql": ("WHERE " + " AND ".join(where)) if where else "",
        "args": tuple(args),
    }


CHARGE_SELECT = """
    SELECT c.charge_id, c.charge_no, c.reg_id, c.reg_fee, c.drug_fee, c.total_fee,
           c.pay_method, c.charge_status, c.charge_time, c.operator,
           c.refund_time, c.refund_reason, c.refund_operator,
           p.patient_id, p.patient_name, p.gender, p.phone,
           d.dept_name, doc.doctor_name
    FROM charge c
    JOIN patient      p   ON c.patient_id = p.patient_id
    JOIN registration r   ON c.reg_id     = r.reg_id
    JOIN doctor       doc ON r.doctor_id  = doc.doctor_id
    JOIN department   d   ON r.dept_id    = d.dept_id
"""


@app.route("/charges")
def charges():
    f = build_charge_filter()
    page = request.args.get("page", type=int) or 1

    total = query_one(
        "SELECT COUNT(*) AS n FROM charge c "
        "JOIN patient p ON c.patient_id = p.patient_id "
        + f["where_sql"],
        f["args"],
    )["n"]
    page, total_pages, offset = paginate(total, page)

    rows = query_all(
        CHARGE_SELECT + f["where_sql"]
        + " ORDER BY c.charge_time DESC, c.charge_id DESC LIMIT %s OFFSET %s",
        f["args"] + (PAGE_SIZE, offset),
    )
    for r in rows:
        r["phone_mask"] = mask_phone(r["phone"])

    # 顶部统计（不受筛选影响，看的是整体经营数据）
    stat = query_one(
        """
        SELECT
            COUNT(*) AS cnt,
            COALESCE(SUM(CASE WHEN charge_status = '已收费' THEN total_fee END), 0) AS income,
            COALESCE(SUM(CASE WHEN charge_status = '已收费' THEN reg_fee END), 0)   AS reg_income,
            COALESCE(SUM(CASE WHEN charge_status = '已收费' THEN drug_fee END), 0)  AS drug_income,
            COALESCE(SUM(CASE WHEN charge_status = '已退费' THEN total_fee END), 0) AS refund,
            SUM(charge_status = '已退费') AS refund_cnt,
            COALESCE(SUM(CASE WHEN charge_status = '已收费' AND DATE(charge_time) = CURDATE()
                              THEN total_fee END), 0) AS today_income
        FROM charge
        """
    )
    for k in ("income", "reg_income", "drug_income", "refund", "today_income"):
        stat[k] = float(stat[k] or 0)

    base_args = {"status": f["status"], "pay": f["pay"], "kw": f["kw"]}
    pager = page_urls("charges", base_args, total_pages, page)
    prev_url = url_for("charges", **clean_args({**base_args, "page": page - 1})) if page > 1 else None
    next_url = url_for("charges", **clean_args({**base_args, "page": page + 1})) if page < total_pages else None

    return render_template(
        "charges.html", rows=rows, stat=stat, current_status=f["status"],
        current_pay=f["pay"], kw=f["kw"], total=total, page=page,
        total_pages=total_pages, pager=pager, prev_url=prev_url, next_url=next_url,
        export_url=url_for("export_charges", **clean_args(base_args)),
        pay_methods=PAY_METHODS,
        status_counts={r["charge_status"]: r["n"] for r in query_all(
            "SELECT charge_status, COUNT(*) AS n FROM charge GROUP BY charge_status")},
    )


# ---------------- 收费：办理页（看应收明细 / 看已收费信息）----------------
@app.route("/charge/<int:reg_id>")
def charge_page(reg_id):
    reg = query_one(
        """
        SELECT r.reg_id, r.reg_date, r.reg_status, r.visit_type, r.reg_fee,
               p.patient_id, p.patient_name, p.gender, p.birth_date, p.phone, p.id_card,
               doc.doctor_name, doc.title, d.dept_name
        FROM registration r
        JOIN patient    p   ON r.patient_id = p.patient_id
        JOIN doctor     doc ON r.doctor_id  = doc.doctor_id
        JOIN department d   ON r.dept_id    = d.dept_id
        WHERE r.reg_id = %s
        """,
        (reg_id,),
    )
    if reg is None:
        abort(404)
    reg["age"] = calc_age(reg["birth_date"])
    reg["phone_mask"] = mask_phone(reg["phone"])

    record = query_one(
        "SELECT record_id, diagnosis, prescription, visit_date FROM medical_record "
        "WHERE reg_id = %s ORDER BY record_id LIMIT 1",
        (reg_id,),
    )

    charge = query_one("SELECT * FROM charge WHERE reg_id = %s", (reg_id,))
    if charge:
        # 已收费：明细读收费时的快照，不重新按现在的药价算
        items = query_all(
            "SELECT item_type, medicine_id, item_name, unit_price, quantity, amount "
            "FROM charge_detail WHERE charge_id = %s ORDER BY cd_id",
            (charge["charge_id"],),
        )
        for it in items:
            it["unit_price"] = float(it["unit_price"])
            it["amount"] = float(it["amount"])
        charge["reg_fee"] = float(charge["reg_fee"])
        charge["drug_fee"] = float(charge["drug_fee"])
        charge["total_fee"] = float(charge["total_fee"])
        drug_fee = charge["drug_fee"]
        total_fee = charge["total_fee"]
    else:
        items, drug_fee = reg_fee_items(reg_id)
        total_fee = round(float(reg["reg_fee"]) + drug_fee, 2)

    return render_template(
        "charge.html", reg=reg, record=record, charge=charge, items=items,
        drug_fee=drug_fee, total_fee=total_fee, total_upper=rmb_upper(total_fee),
        pay_methods=PAY_METHODS,
    )


# ---------------- 收费：收款入账（写收费单 + 明细快照 + 改状态，一个事务）----------------
@app.route("/charge/<int:reg_id>/pay", methods=["POST"])
def charge_pay(reg_id):
    pay_method = (request.form.get("pay_method") or "").strip()
    if pay_method not in PAY_METHODS:
        flash("请选择正确的支付方式", "error")
        return redirect(url_for("charge_page", reg_id=reg_id))

    try:
        with transaction() as cur:
            # 锁住这条挂号，防止两个人同时点"收费"收两遍
            cur.execute("SELECT reg_status, patient_id FROM registration "
                        "WHERE reg_id = %s FOR UPDATE", (reg_id,))
            reg = cur.fetchone()
            if reg is None:
                raise BizError("挂号记录不存在")
            if reg["reg_status"] != "已就诊":
                raise BizError("该挂号当前状态为「%s」，只有「已就诊」才能收费" % reg["reg_status"])

            cur.execute("SELECT charge_id FROM charge WHERE reg_id = %s", (reg_id,))
            if cur.fetchone():
                raise BizError("这条挂号已经收过费了，不能重复收费")

            # 1) 算清楚要收哪些钱，并写成"费用快照"
            items, drug_fee = reg_fee_items(reg_id, cur=cur)
            reg_fee = items[0]["unit_price"] if items else 0.0
            total_fee = round(reg_fee + drug_fee, 2)

            # 2) 写收费单
            charge_no = next_charge_no(cur)
            cur.execute(
                """
                INSERT INTO charge
                    (charge_no, reg_id, patient_id, reg_fee, drug_fee, total_fee,
                     pay_method, charge_status, charge_time, operator)
                VALUES (%s, %s, %s, %s, %s, %s, %s, '已收费', NOW(), %s)
                """,
                (charge_no, reg_id, reg["patient_id"], reg_fee, drug_fee, total_fee,
                 pay_method, session.get("real_name")),
            )
            charge_id = cur.lastrowid

            # 3) 逐条写费用明细
            for it in items:
                cur.execute(
                    """
                    INSERT INTO charge_detail
                        (charge_id, item_type, medicine_id, item_name, unit_price, quantity, amount)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (charge_id, it["item_type"], it["medicine_id"], it["item_name"],
                     it["unit_price"], it["quantity"], it["amount"]),
                )

            # 4) 挂号状态改成已收费（条件更新，防重复提交）
            cur.execute("UPDATE registration SET reg_status = '已收费' "
                        "WHERE reg_id = %s AND reg_status = '已就诊'", (reg_id,))
            if cur.rowcount == 0:
                raise BizError("挂号状态已变化，请刷新后重试")

        log_action("收费", "收费单 %s" % charge_no,
                   "挂号 #%d，应收 %.2f 元（挂号费 %.2f + 药费 %.2f），支付方式 %s"
                   % (reg_id, total_fee, reg_fee, drug_fee, pay_method))
        flash("收费成功：单号 %s，实收 ￥%.2f（%s），挂号 #%d 状态已更新为「已收费」"
              % (charge_no, total_fee, pay_method, reg_id), "ok")
        return redirect(url_for("charges", kw=charge_no))

    except BizError as e:
        flash(str(e), "error")
    except pymysql.err.IntegrityError:
        flash("收费单号冲突（可能有人同时在收费），请重试一次", "error")
    except Exception as e:
        flash("收费失败：%s" % e, "error")
    return redirect(url_for("charge_page", reg_id=reg_id))


# ---------------- 收费：退费（退钱 + 退药回库存，一个事务）----------------
@app.route("/charge/<int:reg_id>/refund", methods=["POST"])
def charge_refund(reg_id):
    reason = (request.form.get("refund_reason") or "").strip() or "未填写原因"

    try:
        with transaction() as cur:
            cur.execute("SELECT charge_id, charge_no, charge_status, total_fee "
                        "FROM charge WHERE reg_id = %s FOR UPDATE", (reg_id,))
            c = cur.fetchone()
            if c is None:
                raise BizError("这条挂号还没有收费单，不能退费")
            if c["charge_status"] != "已收费":
                raise BizError("该收费单当前状态为「%s」，不能重复退费" % c["charge_status"])

            # 1) 把这次收费里的药全部退回库存，并留下"退药入库"流水
            cur.execute("SELECT medicine_id, item_name, quantity FROM charge_detail "
                        "WHERE charge_id = %s AND item_type = '药品费'", (c["charge_id"],))
            back_drugs = cur.fetchall()
            for d in back_drugs:
                cur.execute("SELECT stock FROM medicine WHERE medicine_id = %s FOR UPDATE",
                            (d["medicine_id"],))
                m = cur.fetchone()
                if m is None:
                    continue
                before, after = m["stock"], m["stock"] + d["quantity"]
                cur.execute("UPDATE medicine SET stock = %s WHERE medicine_id = %s",
                            (after, d["medicine_id"]))
                cur.execute(
                    """
                    INSERT INTO medicine_stock_log
                        (medicine_id, change_type, quantity, before_stock, after_stock,
                         reason, create_time)
                    VALUES (%s, '入库', %s, %s, %s, %s, NOW())
                    """,
                    (d["medicine_id"], d["quantity"], before, after,
                     "退药入库（退费 %s）" % c["charge_no"]),
                )

            # 2) 收费单置为已退费（条件更新，防重复退费）
            cur.execute(
                """
                UPDATE charge
                   SET charge_status = '已退费', refund_time = NOW(),
                       refund_reason = %s, refund_operator = %s
                 WHERE charge_id = %s AND charge_status = '已收费'
                """,
                (reason, session.get("real_name"), c["charge_id"]),
            )
            if cur.rowcount == 0:
                raise BizError("退费未生效，请刷新后重试")

            # 3) 如果药已经发出去过，把发药记录置为「已退回」
            cur.execute(
                "UPDATE dispense SET dispense_status = '已退回', return_time = NOW() "
                "WHERE reg_id = %s AND dispense_status = '已发药'",
                (reg_id,),
            )
            returned_disp = cur.rowcount

            # 4) 挂号状态改成已退费（已收费、已发药两种状态都可以退）
            cur.execute("UPDATE registration SET reg_status = '已退费' "
                        "WHERE reg_id = %s AND reg_status IN ('已收费', '已发药')", (reg_id,))
            if cur.rowcount == 0:
                raise BizError("挂号状态已变化，请刷新后重试")

        log_action("退费", "收费单 %s" % c["charge_no"],
                   "挂号 #%d，退款 %.2f 元，退回药品 %d 种%s，原因：%s"
                   % (reg_id, float(c["total_fee"]), len(back_drugs),
                      "，已发出的药置为已退回" if returned_disp else "", reason))
        flash("退费成功：单号 %s 已作废，退款 ￥%.2f，%d 种药品已退回库存%s"
              % (c["charge_no"], float(c["total_fee"]), len(back_drugs),
                 "，发药记录已置为「已退回」" if returned_disp else ""), "ok")

    except BizError as e:
        flash(str(e), "error")
    except Exception as e:
        flash("退费失败：%s" % e, "error")
    return redirect(url_for("charge_page", reg_id=reg_id))


# ---------------- 收费：收据打印页 ----------------
@app.route("/charge/<int:reg_id>/print")
def print_charge(reg_id):
    charge = query_one(
        """
        SELECT c.*, p.patient_name, p.gender, p.phone, p.id_card,
               r.reg_date, r.visit_type, r.reg_id,
               doc.doctor_name, d.dept_name
        FROM charge c
        JOIN patient      p   ON c.patient_id = p.patient_id
        JOIN registration r   ON c.reg_id     = r.reg_id
        JOIN doctor       doc ON r.doctor_id  = doc.doctor_id
        JOIN department   d   ON r.dept_id    = d.dept_id
        WHERE c.reg_id = %s
        """,
        (reg_id,),
    )
    if charge is None:
        abort(404)
    items = query_all(
        "SELECT item_type, item_name, unit_price, quantity, amount FROM charge_detail "
        "WHERE charge_id = %s ORDER BY cd_id",
        (charge["charge_id"],),
    )
    for it in items:
        it["unit_price"] = float(it["unit_price"])
        it["amount"] = float(it["amount"])
    charge["reg_fee"] = float(charge["reg_fee"])
    charge["drug_fee"] = float(charge["drug_fee"])
    charge["total_fee"] = float(charge["total_fee"])
    return render_template("print_charge.html", charge=charge, items=items,
                           total_upper=rmb_upper(charge["total_fee"]),
                           now=datetime.now())


# ---------------- 药房发药：收费之后由药师核对处方发药 ----------------
@app.route("/dispenses")
def dispenses():
    status = (request.args.get("status") or "").strip()   # 待发药 / 已发药 / 已退回
    kw = clean_kw(request.args.get("kw"))
    page = request.args.get("page", type=int) or 1

    # 待发药 = 已收费但还没发药；已发药 / 已退回 = 发药记录里的两种状态
    where = ["(r.reg_status IN ('已收费', '已发药') OR dp.dispense_id IS NOT NULL)"]
    args = []
    if status == "待发药":
        where.append("r.reg_status = '已收费'")
    elif status in ("已发药", "已退回"):
        where.append("dp.dispense_status = %s")
        args.append(status)
    if kw:
        where.append("(p.patient_name LIKE %s OR c.charge_no LIKE %s)")
        args += ["%" + kw + "%", "%" + kw + "%"]
    where_sql = "WHERE " + " AND ".join(where)

    total = query_one(
        f"""
        SELECT COUNT(*) AS n
        FROM registration r
        JOIN patient p ON r.patient_id = p.patient_id
        LEFT JOIN charge   c  ON c.reg_id  = r.reg_id
        LEFT JOIN dispense dp ON dp.reg_id = r.reg_id
        {where_sql}
        """,
        tuple(args),
    )["n"]
    page, total_pages, offset = paginate(total, page)

    rows = query_all(
        f"""
        SELECT r.reg_id, r.reg_date, r.time_period, r.queue_no, r.reg_status,
               p.patient_id, p.patient_name, p.gender, p.birth_date, p.phone,
               doc.doctor_name, d.dept_name,
               c.charge_no, c.total_fee, c.charge_time,
               dp.dispense_status, dp.pharmacist, dp.dispense_time, dp.return_time,
               (SELECT COUNT(*) FROM prescription_detail pd
                  JOIN medical_record mr ON pd.record_id = mr.record_id
                 WHERE mr.reg_id = r.reg_id) AS med_cnt
        FROM registration r
        JOIN patient    p   ON r.patient_id = p.patient_id
        JOIN doctor     doc ON r.doctor_id  = doc.doctor_id
        JOIN department d   ON r.dept_id    = d.dept_id
        LEFT JOIN charge   c  ON c.reg_id  = r.reg_id
        LEFT JOIN dispense dp ON dp.reg_id = r.reg_id
        {where_sql}
        ORDER BY (r.reg_status = '已收费') DESC, c.charge_time DESC, r.reg_id DESC
        LIMIT %s OFFSET %s
        """,
        tuple(args) + (PAGE_SIZE, offset),
    )
    for r in rows:
        r["phone_mask"] = mask_phone(r["phone"])
        r["age"] = calc_age(r["birth_date"])
        if r["total_fee"] is not None:
            r["total_fee"] = float(r["total_fee"])

    stat = query_one(
        """
        SELECT
            SUM(r.reg_status = '已收费') AS to_dispense,
            SUM(dp.dispense_status = '已发药') AS dispensed,
            SUM(dp.dispense_status = '已退回') AS returned
        FROM registration r
        LEFT JOIN dispense dp ON dp.reg_id = r.reg_id
        """
    )
    for k in ("to_dispense", "dispensed", "returned"):
        stat[k] = stat[k] or 0

    base_args = {"status": status, "kw": kw}
    pager = page_urls("dispenses", base_args, total_pages, page)
    prev_url = url_for("dispenses", **clean_args({**base_args, "page": page - 1})) if page > 1 else None
    next_url = url_for("dispenses", **clean_args({**base_args, "page": page + 1})) if page < total_pages else None

    return render_template("dispenses.html", rows=rows, stat=stat, current_status=status,
                           kw=kw, total=total, page=page, total_pages=total_pages,
                           pager=pager, prev_url=prev_url, next_url=next_url)


@app.route("/dispense/<int:reg_id>", methods=["POST"])
def dispense(reg_id):
    back = safe_back("dispenses")
    try:
        with transaction() as cur:
            cur.execute("SELECT reg_status FROM registration WHERE reg_id = %s FOR UPDATE", (reg_id,))
            reg = cur.fetchone()
            if reg is None:
                raise BizError("挂号记录不存在")
            if reg["reg_status"] != "已收费":
                raise BizError("该挂号当前状态为「%s」，只有「已收费」才能发药" % reg["reg_status"])

            cur.execute("SELECT charge_id FROM charge WHERE reg_id = %s AND charge_status = '已收费'",
                        (reg_id,))
            if cur.fetchone() is None:
                raise BizError("这条挂号没有有效的已收费收费单，不能发药")

            cur.execute("SELECT dispense_id FROM dispense WHERE reg_id = %s", (reg_id,))
            if cur.fetchone():
                raise BizError("这条挂号已经发过药了，不能重复发药")

            cur.execute("SELECT record_id FROM medical_record WHERE reg_id = %s "
                        "ORDER BY record_id LIMIT 1", (reg_id,))
            row = cur.fetchone()
            record_id = row["record_id"] if row else None

            cur.execute(
                """
                INSERT INTO dispense
                    (reg_id, record_id, dispense_status, pharmacist, dispense_time, remark)
                VALUES (%s, %s, '已发药', %s, NOW(), %s)
                """,
                (reg_id, record_id, session.get("real_name"), "按处方核对发药"),
            )
            cur.execute("UPDATE registration SET reg_status = '已发药' "
                        "WHERE reg_id = %s AND reg_status = '已收费'", (reg_id,))
            if cur.rowcount == 0:
                raise BizError("挂号状态已变化，请刷新后重试")

        log_action("发药", "挂号 #%d" % reg_id, "药房核对处方并发药")
        flash("发药成功：挂号 #%d 状态已更新为「已发药」" % reg_id, "ok")
    except BizError as e:
        flash(str(e), "error")
    except Exception as e:
        flash("发药失败：%s" % e, "error")
    return redirect(back)


# ---------------- 住院管理（admission） ----------------
# 住院和门诊是两条并行的线：门诊以"挂号"为单位，住院以"住院登记"为单位。
# 这里做的是住院的最小闭环 —— 办理入院、办理出院、在院一览、患者住院史。
# 住院费用日清单、床位调度、医嘱属于独立子系统，本项目有意不做（见设计文档）。
@app.route("/admissions")
def admissions():
    # 默认只看在院（住院处最常用的一屏）；传 status=全部 才看全部
    status = request.args.get("status") or "在院"
    if status != "全部" and status not in ADMISSION_STATUSES:
        status = "在院"
    dept_id = request.args.get("dept_id", type=int)
    kw = clean_kw(request.args.get("kw"))
    page = request.args.get("page", type=int) or 1
    highlight = request.args.get("highlight", type=int)

    where, args = [], []
    if status in ADMISSION_STATUSES:
        where.append("a.status = %s")
        args.append(status)
    if dept_id:
        where.append("a.dept_id = %s")
        args.append(dept_id)
    if kw:
        where.append("(p.patient_name LIKE %s OR a.admission_no LIKE %s"
                     " OR a.bed_no LIKE %s OR doc.doctor_name LIKE %s)")
        args += ["%" + kw + "%"] * 4
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    base_sql = """
        FROM admission a
        JOIN patient    p   ON a.patient_id = p.patient_id
        JOIN doctor     doc ON a.doctor_id  = doc.doctor_id
        JOIN department d   ON a.dept_id    = d.dept_id
    """

    total = query_one("SELECT COUNT(*) AS n " + base_sql + where_sql, tuple(args))["n"]
    page, total_pages, offset = paginate(total, page)

    rows = query_all(
        """
        SELECT a.admission_id, a.admission_no, a.bed_no, a.admit_diagnosis,
               a.admit_time, a.discharge_time, a.status, a.discharge_summary,
               a.admit_operator, a.discharge_operator,
               p.patient_id, p.patient_name, p.gender, p.birth_date, p.phone,
               doc.doctor_name, doc.title,
               d.dept_id, d.dept_name,
               (DATEDIFF(COALESCE(a.discharge_time, NOW()), a.admit_time) + 1) AS days,
               (SELECT COUNT(*) FROM admission x
                 WHERE x.patient_id = a.patient_id) AS history_cnt
        """ + base_sql + where_sql + """
        ORDER BY (a.status = '已出院'), a.admit_time DESC, a.admission_id DESC
        LIMIT %s OFFSET %s
        """,
        tuple(args) + (PAGE_SIZE, offset),
    )
    for r in rows:
        r["age"] = calc_age(r["birth_date"])
        r["phone_mask"] = mask_phone(r["phone"])

    stat = query_one(
        """
        SELECT
          (SELECT COUNT(*) FROM admission WHERE status = '在院') AS in_ward,
          (SELECT COUNT(*) FROM admission WHERE DATE(admit_time) = CURDATE()) AS today_admit,
          (SELECT COUNT(*) FROM admission
            WHERE discharge_time IS NOT NULL AND DATE(discharge_time) = CURDATE()) AS today_discharge,
          (SELECT COUNT(DISTINCT patient_id) FROM admission) AS patient_cnt,
          (SELECT COUNT(*) FROM admission) AS total_cnt
        """
    )

    status_counts = {}
    for r in query_all("SELECT status, COUNT(*) AS n FROM admission GROUP BY status"):
        status_counts[r["status"]] = r["n"]
    status_counts["全部"] = sum(status_counts.values())

    depts = query_all("SELECT dept_id, dept_name FROM department ORDER BY dept_id")

    base_args = {"status": status, "dept_id": dept_id, "kw": kw}
    pager = page_urls("admissions", base_args, total_pages, page)
    prev_url = url_for("admissions", **clean_args({**base_args, "page": page - 1})) if page > 1 else None
    next_url = url_for("admissions", **clean_args({**base_args, "page": page + 1})) if page < total_pages else None
    export_url = url_for("export_admissions", **clean_args(base_args))

    return render_template(
        "admissions.html", rows=rows, stat=stat, status_counts=status_counts,
        current_status=status, current_dept=dept_id, kw=kw, depts=depts,
        highlight=highlight, total=total, page=page, total_pages=total_pages,
        pager=pager, prev_url=prev_url, next_url=next_url, export_url=export_url,
        # 出院表单里 datetime-local 的默认值
        now_str=datetime.now().strftime("%Y-%m-%dT%H:%M"),
    )


# ---------------- 办理入院 ----------------
@app.route("/admission/new", methods=["GET", "POST"])
def admission_new():
    patient_id = request.values.get("patient_id", type=int)
    dept_id = request.values.get("dept_id", type=int)
    doctor_id = request.values.get("doctor_id", type=int)

    if request.method == "POST":
        bed_no = (request.form.get("bed_no") or "").strip()
        diag = (request.form.get("admit_diagnosis") or "").strip()
        remark = (request.form.get("remark") or "").strip()

        errors = []
        if not patient_id:
            errors.append("请选择要办理入院的患者")
        if not dept_id or not doctor_id:
            errors.append("请选择住院科室与主治医生")
        else:
            # 不信任前端传上来的科室-医生组合，后端自己核对一遍
            doc = query_one("SELECT doctor_id, dept_id FROM doctor WHERE doctor_id = %s",
                            (doctor_id,))
            if doc is None:
                errors.append("主治医生不存在，请重新选择")
            elif doc["dept_id"] != dept_id:
                errors.append("所选医生不属于该科室，请重新选择")

        if not errors:
            try:
                with transaction() as cur:
                    # 锁住这位患者的行：两个人同时点"办理入院"时只有一个能拿到锁
                    cur.execute("SELECT patient_name FROM patient "
                                "WHERE patient_id = %s FOR UPDATE", (patient_id,))
                    pat = cur.fetchone()
                    if pat is None:
                        raise BizError("患者不存在，请刷新后重试")

                    # 先查一遍是为了给友好提示；真正的兜底是表上那个
                    # 生成列 + 唯一键（并发时谁也别想插进第二条「在院」）
                    cur.execute("SELECT admission_no, admit_time FROM admission "
                                "WHERE patient_id = %s AND status = '在院'", (patient_id,))
                    old = cur.fetchone()
                    if old:
                        raise BizError("患者「%s」已在院（住院号 %s，%s 入院），请先办理出院"
                                       % (pat["patient_name"], old["admission_no"],
                                          old["admit_time"].strftime("%m-%d %H:%M")))

                    adm_no = next_admission_no(cur)
                    cur.execute(
                        """
                        INSERT INTO admission
                            (admission_no, patient_id, dept_id, doctor_id, bed_no,
                             admit_diagnosis, admit_time, status, admit_operator, remark)
                        VALUES (%s, %s, %s, %s, %s, %s, NOW(), '在院', %s, %s)
                        """,
                        (adm_no, patient_id, dept_id, doctor_id, bed_no or None,
                         diag or None, session.get("real_name"), remark or None),
                    )
                    aid = cur.lastrowid

                dept_row = query_one("SELECT dept_name FROM department WHERE dept_id = %s",
                                     (dept_id,))
                dept_name = dept_row["dept_name"] if dept_row else ""
                log_action("办理入院", "住院单 %s" % adm_no,
                           "患者 #%d 入住 %s%s" % (patient_id, dept_name,
                                                  "，床位 " + bed_no if bed_no else ""))
                flash("入院办理成功：住院号 %s，患者「%s」已入住 %s%s"
                      % (adm_no, pat["patient_name"], dept_name,
                         "（床位 %s）" % bed_no if bed_no else ""), "ok")
                return redirect(url_for("admissions", highlight=aid))
            except BizError as e:
                errors.append(str(e))
            except pymysql.err.IntegrityError as e:
                # 1062 = 唯一键冲突，多半是并发下两个人同时给同一位患者办了入院
                if e.args and e.args[0] == 1062:
                    errors.append("该患者刚刚已被办理入院，请刷新后查看在院列表")
                else:
                    errors.append("办理失败：%s" % (e.args[1] if len(e.args) > 1 else e))
            except Exception as e:
                errors.append("办理失败：%s" % e)

        for e in errors:
            flash(e, "error")

    prefill = {"patient_id": patient_id, "dept_id": dept_id, "doctor_id": doctor_id}

    # 患者列表带上"是否已在院"，避免把已经住院的人再办一次
    patients = query_all(
        """
        SELECT p.patient_id, p.patient_name, p.gender, p.birth_date, p.phone,
               a.admission_no, a.bed_no
        FROM patient p
        LEFT JOIN admission a ON a.patient_id = p.patient_id AND a.status = '在院'
        ORDER BY p.patient_id
        """
    )
    for p in patients:
        p["age"] = calc_age(p["birth_date"])
        p["phone_mask"] = mask_phone(p["phone"])
        p["in_ward"] = p["admission_no"] is not None
        p["label"] = "%s（%s%s%s）%s" % (
            p["patient_name"], p["gender"] or "",
            "·%d岁" % p["age"] if p["age"] is not None else "",
            "·" + p["phone_mask"] if p["phone_mask"] else "",
            "·已在院" if p["in_ward"] else "",
        )
        p["keyword"] = "%s %s %s" % (p["patient_id"], p["patient_name"], p["phone"] or "")

    # 只能给"在职医生 + 启用科室"办入院，停用的不再出现在下拉里
    docs = query_all(
        "SELECT doctor_id, doctor_name, title, dept_id FROM doctor "
        "WHERE status = '在职' ORDER BY dept_id, doctor_id"
    )
    depts = query_all("SELECT dept_id, dept_name FROM department "
                      "WHERE status = '启用' ORDER BY dept_id")
    doctors_json = json_for_script(
        [{"doctor_id": d["doctor_id"], "doctor_name": d["doctor_name"],
          "title": d["title"] or "", "dept_id": d["dept_id"]} for d in docs]
    )

    return render_template(
        "admission_new.html", patients=patients, depts=depts,
        doctors_json=doctors_json, prefill=prefill,
        form=request.form if request.method == "POST" else None,
    )


# ---------------- 办理出院 ----------------
@app.route("/admission/<int:aid>/discharge", methods=["POST"])
def admission_discharge(aid):
    back = safe_back("admissions")
    adm = query_one("SELECT * FROM admission WHERE admission_id = %s", (aid,))
    if adm is None:
        abort(404)
    if adm["status"] != "在院":
        flash("住院号 %s 已经是「%s」，不能重复办理出院"
              % (adm["admission_no"], adm["status"]), "error")
        return redirect(back)

    summary = (request.form.get("discharge_summary") or "").strip()

    # 出院时间默认"现在"；前端给的是 datetime-local，形如 2026-10-14T15:30
    raw = (request.form.get("discharge_time") or "").strip()
    dtime = None
    if raw:
        try:
            dtime = datetime.strptime(raw.replace("T", " "), "%Y-%m-%d %H:%M")
        except ValueError:
            flash("出院时间格式不对，已按当前时间办理", "error")
    if dtime is None:
        dtime = datetime.now().replace(second=0, microsecond=0)

    # 入院时间入库时带秒（NOW()），出院时间（datetime-local）只精确到分。
    # 两边都按分钟比较，否则"刚入院同一分钟内就办出院"会被误判成时间倒流。
    admit_min = adm["admit_time"].replace(second=0, microsecond=0)
    if dtime < admit_min:
        flash("出院时间不能早于入院时间（%s）"
              % adm["admit_time"].strftime("%Y-%m-%d %H:%M"), "error")
        return redirect(back)
    if dtime > datetime.now():
        flash("出院时间不能晚于当前时间", "error")
        return redirect(back)

    # 前端只精确到分钟，可能出现"同一分钟内出院时间比入院时间少几秒"，
    # 这里补足到入院时刻，免得撞上表上的 CHECK 约束（出院必须晚于入院）
    if dtime < adm["admit_time"]:
        dtime = adm["admit_time"]

    days = (dtime.date() - adm["admit_time"].date()).days + 1

    # 条件更新：只有当前仍是"在院"才允许改，重复提交不会把已出院的记录再改一遍
    try:
        affected = execute(
            """
            UPDATE admission
               SET status = '已出院', discharge_time = %s, discharge_summary = %s,
                   discharge_operator = %s
             WHERE admission_id = %s AND status = '在院'
            """,
            (dtime, summary or None, session.get("real_name"), aid),
        )
    except Exception as e:
        flash("出院办理失败：%s" % e, "error")
        return redirect(back)

    if affected:
        log_action("办理出院", "住院单 %s" % adm["admission_no"],
                   "住院 %d 天，出院时间 %s" % (days, dtime.strftime("%Y-%m-%d %H:%M")))
        flash("出院办理成功：住院号 %s，本次共住院 %d 天，床位已释放"
              % (adm["admission_no"], days), "ok")
    else:
        flash("操作未生效：这条住院记录可能已被处理，请刷新后查看", "error")
    return redirect(back)


# ---------------- 住院单打印 ----------------
@app.route("/admission/<int:aid>/print")
def print_admission(aid):
    adm = query_one(
        """
        SELECT a.admission_id, a.admission_no, a.bed_no, a.admit_diagnosis,
               a.admit_time, a.discharge_time, a.status, a.discharge_summary,
               a.admit_operator, a.discharge_operator, a.remark,
               p.patient_id, p.patient_name, p.gender, p.birth_date, p.phone, p.id_card,
               doc.doctor_name, doc.title, d.dept_name
        FROM admission a
        JOIN patient    p   ON a.patient_id = p.patient_id
        JOIN doctor     doc ON a.doctor_id  = doc.doctor_id
        JOIN department d   ON a.dept_id    = d.dept_id
        WHERE a.admission_id = %s
        """,
        (aid,),
    )
    if adm is None:
        abort(404)
    adm["age"] = calc_age(adm["birth_date"])
    end = adm["discharge_time"] or datetime.now()
    days = (end.date() - adm["admit_time"].date()).days + 1
    return render_template("print_admission.html", adm=adm, days=days,
                           now=datetime.now())


# ---------------- 住院登记导出 Excel ----------------
@app.route("/export/admissions")
def export_admissions():
    status = request.args.get("status") or "在院"
    if status != "全部" and status not in ADMISSION_STATUSES:
        status = "在院"
    dept_id = request.args.get("dept_id", type=int)
    kw = clean_kw(request.args.get("kw"))

    where, args = [], []
    if status in ADMISSION_STATUSES:
        where.append("a.status = %s")
        args.append(status)
    if dept_id:
        where.append("a.dept_id = %s")
        args.append(dept_id)
    if kw:
        where.append("(p.patient_name LIKE %s OR a.admission_no LIKE %s"
                     " OR a.bed_no LIKE %s OR doc.doctor_name LIKE %s)")
        args += ["%" + kw + "%"] * 4
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    rows = query_all(
        """
        SELECT a.admission_no, p.patient_name, p.gender, p.birth_date, d.dept_name,
               doc.doctor_name, a.bed_no, a.admit_diagnosis, a.admit_time,
               a.discharge_time, a.status,
               (DATEDIFF(COALESCE(a.discharge_time, NOW()), a.admit_time) + 1) AS days,
               a.admit_operator, a.discharge_operator
        FROM admission a
        JOIN patient    p   ON a.patient_id = p.patient_id
        JOIN doctor     doc ON a.doctor_id  = doc.doctor_id
        JOIN department d   ON a.dept_id    = d.dept_id
        """ + where_sql + """
        ORDER BY (a.status = '已出院'), a.admit_time DESC, a.admission_id DESC
        """,
        tuple(args),
    )

    headers = ["住院号", "患者", "性别", "年龄", "住院科室", "主治医生", "床位",
               "入院诊断", "入院时间", "出院时间", "住院天数", "状态",
               "入院经办人", "出院经办人"]
    data = []
    for r in rows:
        data.append([
            r["admission_no"], r["patient_name"], r["gender"],
            calc_age(r["birth_date"]) if r["birth_date"] else "",
            r["dept_name"], r["doctor_name"], r["bed_no"] or "",
            r["admit_diagnosis"] or "",
            r["admit_time"].strftime("%Y-%m-%d %H:%M") if r["admit_time"] else "",
            r["discharge_time"].strftime("%Y-%m-%d %H:%M") if r["discharge_time"] else "",
            r["days"], r["status"], r["admit_operator"] or "", r["discharge_operator"] or "",
        ])
    buf = build_xlsx("住院登记", headers, data,
                     widths=[16, 12, 6, 6, 14, 12, 14, 22, 18, 18, 10, 8, 12, 12])
    return send_file(buf, as_attachment=True, download_name="住院登记.xlsx",
                     mimetype=XLSX_MIME)


# ---------------- 患者管理：患者档案列表 ----------------
@app.route("/patients")
def patients():
    kw = clean_kw(request.args.get("kw"))
    page = request.args.get("page", type=int) or 1

    where_sql, args = "", ()
    if kw:
        where_sql = "WHERE p.patient_name LIKE %s OR p.phone LIKE %s"
        args = ("%" + kw + "%", "%" + kw + "%")

    total = query_one(
        f"SELECT COUNT(*) AS n FROM patient p {where_sql}", args
    )["n"]
    page, total_pages, offset = paginate(total, page)

    rows = query_all(
        f"""
        SELECT p.patient_id, p.patient_name, p.gender, p.birth_date,
               p.id_card, p.phone, p.address,
               COUNT(r.reg_id) AS reg_cnt,
               SUM(CASE WHEN r.reg_status = '已就诊' THEN 1 ELSE 0 END) AS visited_cnt,
               SUM(CASE WHEN r.reg_status = '已挂号' THEN 1 ELSE 0 END) AS waiting_cnt,
               MAX(r.reg_date) AS last_reg
        FROM patient p
        LEFT JOIN registration r ON p.patient_id = r.patient_id
        {where_sql}
        GROUP BY p.patient_id, p.patient_name, p.gender, p.birth_date,
                 p.id_card, p.phone, p.address
        ORDER BY p.patient_id
        LIMIT %s OFFSET %s
        """,
        args + (PAGE_SIZE, offset),
    )
    for r in rows:
        r["age"] = calc_age(r["birth_date"])
        r["phone_mask"] = mask_phone(r["phone"])
        r["id_card_mask"] = mask_id_card(r["id_card"])
        r["visited_cnt"] = r["visited_cnt"] or 0
        r["waiting_cnt"] = r["waiting_cnt"] or 0

    pager = page_urls("patients", {"kw": kw}, total_pages, page)
    export_url = url_for("export_patients", **clean_args({"kw": kw}))

    return render_template("patients.html", rows=rows, kw=kw, total=total,
                           page=page, total_pages=total_pages, pager=pager,
                           export_url=export_url)


# ---------------- 患者档案：基本信息 + 挂号历史 + 病历 + 处方 ----------------
@app.route("/patient/<int:pid>")
def patient_detail(pid):
    info = query_one("SELECT * FROM patient WHERE patient_id = %s", (pid,))
    if info is None:
        abort(404)

    info["age"] = calc_age(info["birth_date"])
    info["phone_mask"] = mask_phone(info["phone"])
    info["id_card_mask"] = mask_id_card(info["id_card"])

    regs = query_all(
        """
        SELECT r.reg_id, r.reg_date, r.reg_fee, r.reg_status, r.visit_type,
               r.parent_reg_id,
               doc.doctor_name, doc.title, d.dept_name
        FROM registration r
        JOIN doctor     doc ON r.doctor_id = doc.doctor_id
        JOIN department d   ON r.dept_id   = d.dept_id
        WHERE r.patient_id = %s
        ORDER BY r.reg_date DESC, r.reg_id DESC
        """,
        (pid,),
    )

    records = query_all(
        """
        SELECT m.record_id, m.reg_id, m.visit_date, m.diagnosis, m.prescription, m.remark,
               doc.doctor_name
        FROM medical_record m
        JOIN doctor doc ON m.doctor_id = doc.doctor_id
        WHERE m.patient_id = %s
        ORDER BY m.visit_date DESC
        """,
        (pid,),
    )

    # 每份病历的处方明细（一次查完，在 Python 里分组，避免循环里反复查库）
    record_ids = [r["record_id"] for r in records]
    details = {}
    if record_ids:
        placeholders = ",".join(["%s"] * len(record_ids))
        rows = query_all(
            f"""
            SELECT pd.record_id, pd.quantity, pd.dosage,
                   med.medicine_name, med.spec, med.unit, med.price
            FROM prescription_detail pd
            JOIN medicine med ON pd.medicine_id = med.medicine_id
            WHERE pd.record_id IN ({placeholders})
            ORDER BY pd.record_id, pd.detail_id
            """,
            tuple(record_ids),
        )
        for row in rows:
            details.setdefault(row["record_id"], []).append(row)
    for r in records:
        r["details"] = details.get(r["record_id"], [])
        r["total"] = sum(d["price"] * d["quantity"] for d in r["details"])

    # 住院史：这位患者住过几次院、每次在哪个科、住了多久
    adm_rows = query_all(
        """
        SELECT a.admission_id, a.admission_no, a.bed_no, a.admit_diagnosis,
               a.admit_time, a.discharge_time, a.status, a.discharge_summary,
               doc.doctor_name, d.dept_name,
               (DATEDIFF(COALESCE(a.discharge_time, NOW()), a.admit_time) + 1) AS days
        FROM admission a
        JOIN doctor     doc ON a.doctor_id = doc.doctor_id
        JOIN department d   ON a.dept_id   = d.dept_id
        WHERE a.patient_id = %s
        ORDER BY (a.status = '已出院'), a.admit_time DESC
        """,
        (pid,),
    )
    in_ward = next((a for a in adm_rows if a["status"] == "在院"), None)

    return render_template("patient_detail.html", info=info,
                           regs=regs, records=records,
                           admissions=adm_rows, in_ward=in_ward)


# ---------------- 患者改档：建档之后更正基本信息 ----------------
@app.route("/patient/<int:pid>/edit", methods=["GET", "POST"])
def patient_edit(pid):
    info = query_one("SELECT * FROM patient WHERE patient_id = %s", (pid,))
    if info is None:
        abort(404)

    if request.method == "POST":
        # 直接复用"建档"的那套校验，字段名保持一致（new_name / new_gender …）
        data, errors = validate_new_patient(request.form)

        # 身份证在库里是唯一的；改档时要排除自己那一条，否则自己和自己冲突
        if not errors and data["id_card"]:
            dup = query_one(
                "SELECT patient_id, patient_name FROM patient "
                "WHERE id_card = %s AND patient_id <> %s",
                (data["id_card"], pid),
            )
            if dup:
                errors.append("身份证号已经被患者「%s（编号 %s）」使用，请核对"
                              % (dup["patient_name"], dup["patient_id"]))

        if not errors:
            try:
                execute(
                    """
                    UPDATE patient
                       SET patient_name = %s, gender = %s, birth_date = %s,
                           id_card = %s, phone = %s, address = %s
                     WHERE patient_id = %s
                    """,
                    (data["patient_name"], data["gender"], data["birth_date"],
                     data["id_card"], data["phone"], data["address"], pid),
                )
                log_action("修改档案", "患者 #%d" % pid,
                           "更正为 %s / %s / %s" % (data["patient_name"], data["gender"],
                                                    data["phone"] or "无电话"))
                flash("患者 #%d 的档案已更新" % pid, "ok")
                return redirect(url_for("patient_detail", pid=pid))
            except pymysql.err.IntegrityError:
                errors.append("身份证号与已有患者冲突，请核对后再保存")
            except Exception as e:
                errors.append("保存失败：%s" % e)

        for e in errors:
            flash(e, "error")
        # 保留用户这次填的内容，不用重打一遍
        info = dict(info)
        info.update({k: v for k, v in request.form.items() if k.startswith("new_")})

    info["age"] = calc_age(info["birth_date"])
    return render_template("patient_edit.html", info=info,
                           form=request.form if request.method == "POST" else None)


# ---------------- 新增挂号 ----------------
@app.route("/add", methods=["GET", "POST"])
def add():
    # 默认值：可以直接从 URL 预填（复诊挂号就是点按钮带参数过来的）
    prefill = {
        "patient_id": request.values.get("patient_id", type=int),
        "dept_id": request.values.get("dept_id", type=int),
        "doctor_id": request.values.get("doctor_id", type=int),
        "visit_type": request.values.get("visit_type") or "初诊",
        "parent_reg_id": request.values.get("parent_reg_id", type=int),
        "reg_fee": request.values.get("reg_fee", type=float),
        "work_date": request.values.get("work_date") or date.today().isoformat(),
        "time_period": request.values.get("time_period") or QUEUE_DEFAULT_PERIOD,
    }
    if prefill["visit_type"] not in ("初诊", "复诊"):
        prefill["visit_type"] = "初诊"
    if prefill["time_period"] not in TIME_PERIODS:
        prefill["time_period"] = QUEUE_DEFAULT_PERIOD

    if request.method == "POST":
        # 患者来源：existing=从已有患者里选；new=现场建档（新患者）
        mode = request.form.get("patient_mode") or "existing"
        if mode not in ("existing", "new"):
            mode = "existing"

        patient_id = prefill["patient_id"]
        doctor_id = prefill["doctor_id"]
        dept_id = prefill["dept_id"]
        visit_type = prefill["visit_type"]
        parent_reg_id = prefill["parent_reg_id"] if visit_type == "复诊" else None

        errors = []
        new_patient = None

        if mode == "new":
            new_patient, errors = validate_new_patient(request.form)
        elif not patient_id:
            errors.append("请选择患者，或切换成「新建患者」现场建档")

        if not dept_id or not doctor_id:
            errors.append("请选择科室与医生")

        # 就诊日期 / 时段：挂号必须落在排班范围内，今天的号只能挂今天及之后
        time_period = prefill["time_period"]
        work_date = None
        try:
            work_date = datetime.strptime(prefill["work_date"], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            errors.append("就诊日期格式不正确，请重新选择")
        else:
            if work_date < date.today():
                errors.append("不能挂过去的号（就诊日期不能早于今天）")
            elif work_date > date.today() + timedelta(days=BOOK_AHEAD_DAYS):
                errors.append("当前只放号到今天之后 %d 天内的排班，请重新选择就诊日期"
                              % BOOK_AHEAD_DAYS)
        if time_period not in TIME_PERIODS:
            errors.append("请选择上午或下午时段")

        # 后端必须再校验一次：医生是不是真属于这个科室（前端校验可以被绕过）
        if not errors:
            doc = query_one(
                "SELECT dept_id, doctor_name, status FROM doctor WHERE doctor_id = %s",
                (doctor_id,),
            )
            dept_row = query_one("SELECT status FROM department WHERE dept_id = %s", (dept_id,))
            if doc is None:
                errors.append("该医生不存在，请重新选择")
            elif doc["dept_id"] != dept_id:
                errors.append("医生与所选科室不匹配，请重新选择")
            elif doc["status"] != "在职":
                # 前端下拉已经把离职医生过滤掉了，这里防的是"绕过页面直接提交"
                errors.append("「%s」已离职，不能再挂号" % doc["doctor_name"])
            elif dept_row is None or dept_row["status"] != "启用":
                errors.append("该科室已停用，不能再挂号")

        if not errors:
            try:
                # 建档 + 挂号放在同一个事务里：要么都成功，要么都不写。
                # 否则一旦挂号失败，就会留下一个"没有挂号的空档案"。
                with transaction() as cur:
                    if mode == "new":
                        # 身份证在库里有唯一约束，重复时给一句能照做的提示，而不是抛 500
                        if new_patient["id_card"]:
                            cur.execute(
                                "SELECT patient_id, patient_name FROM patient WHERE id_card = %s",
                                (new_patient["id_card"],),
                            )
                            dup = cur.fetchone()
                            if dup:
                                raise BizError(
                                    "身份证号 %s 已经建过档（患者：%s，编号 %s）。"
                                    "请在上方「已有患者」里直接选择这位患者，或核对身份证号是否填错。"
                                    % (new_patient["id_card"], dup["patient_name"], dup["patient_id"])
                                )
                        cur.execute(
                            """
                            INSERT INTO patient
                                (patient_name, gender, birth_date, id_card, phone, address)
                            VALUES (%s, %s, %s, %s, %s, %s)
                            """,
                            (new_patient["patient_name"], new_patient["gender"],
                             new_patient["birth_date"], new_patient["id_card"],
                             new_patient["phone"], new_patient["address"]),
                        )
                        patient_id = cur.lastrowid

                    # 找号源：按"医生 + 就诊日期 + 时段"定位排班。
                    # 不信任前端传来的排班编号，而是自己按这三项查，
                    # 免得有人改表单把号挂到别的医生 / 时段上。
                    cur.execute(
                        """
                        SELECT schedule_id, total_quota, booked_count, status
                        FROM doctor_schedule
                        WHERE doctor_id = %s AND work_date = %s AND time_period = %s
                        FOR UPDATE
                        """,
                        (doctor_id, work_date, time_period),
                    )
                    sch = cur.fetchone()
                    if sch is None:
                        raise BizError(
                            "%s %s 这位医生没有排班，只能挂有排班的时段（请重新选择日期或时段）"
                            % (work_date, time_period))
                    if sch["status"] != "正常":
                        raise BizError("该医生 %s %s 已停诊，请换一位医生或时段"
                                       % (work_date, time_period))

                    # 原子占号：把"号还有没有"和"占一个号"合成一条条件更新，
                    # 两个人同时抢最后一个号时，只有一个人能成功。
                    cur.execute(
                        "UPDATE doctor_schedule SET booked_count = booked_count + 1 "
                        "WHERE schedule_id = %s AND status = '正常' "
                        "AND booked_count < total_quota",
                        (sch["schedule_id"],),
                    )
                    if cur.rowcount == 0:
                        raise BizError("该时段号源已挂完（%d / %d），请换一个时段或医生"
                                       % (sch["booked_count"], sch["total_quota"]))

                    # 候诊序号：同一位医生、同一就诊日，按挂号先后排号
                    cur.execute(
                        """
                        SELECT COALESCE(MAX(r.queue_no), 0) + 1 AS n
                        FROM registration r
                        JOIN doctor_schedule s ON r.schedule_id = s.schedule_id
                        WHERE r.doctor_id = %s AND s.work_date = %s
                        """,
                        (doctor_id, work_date),
                    )
                    queue_no = cur.fetchone()["n"]

                    cur.execute(
                        """
                        INSERT INTO registration
                            (patient_id, doctor_id, dept_id, schedule_id, reg_date,
                             time_period, queue_no, reg_fee, reg_status, visit_type,
                             parent_reg_id)
                        VALUES (%s, %s, %s, %s, NOW(), %s, %s, %s, '已挂号', %s, %s)
                        """,
                        (patient_id, doctor_id, dept_id, sch["schedule_id"],
                         time_period, queue_no, prefill["reg_fee"] or 0,
                         visit_type, parent_reg_id),
                    )
                    new_id = cur.lastrowid
            except BizError as e:
                errors.append(str(e))
            except pymysql.err.IntegrityError as e:
                errors.append("保存失败：身份证号等唯一信息与已有记录冲突（%s）" % e.args[-1])
            except Exception as e:
                errors.append("挂号失败：%s" % e)
            else:
                log_action("新增挂号", "挂号 #%d" % new_id,
                           "%s挂号，患者 #%d，就诊 %s %s，候诊号 %d，挂号费 %.2f 元"
                           % (visit_type, patient_id, work_date, time_period,
                              queue_no, float(prefill["reg_fee"] or 0)))
                if mode == "new":
                    flash("建档成功！新患者编号 %s；挂号编号 %s（%s，%s %s，候诊号 %d）"
                          % (patient_id, new_id, visit_type, work_date, time_period,
                             queue_no), "ok")
                else:
                    flash("挂号成功！挂号编号 %s（%s，%s %s，候诊号 %d）"
                          % (new_id, visit_type, work_date, time_period, queue_no), "ok")
                return redirect(url_for("registrations", highlight=new_id))

        for e in errors:
            flash(e, "error")

    # 已有患者列表：带上性别/年龄/脱敏手机号，避免同名患者分不清
    patients = query_all(
        "SELECT patient_id, patient_name, gender, birth_date, phone "
        "FROM patient ORDER BY patient_id"
    )
    for p in patients:
        p["age"] = calc_age(p["birth_date"])
        p["phone_mask"] = mask_phone(p["phone"])
        p["label"] = "%s（%s%s%s）" % (
            p["patient_name"], p["gender"] or "",
            "·%d岁" % p["age"] if p["age"] is not None else "",
            "·" + p["phone_mask"] if p["phone_mask"] else "",
        )
        p["keyword"] = "%s %s %s" % (p["patient_id"], p["patient_name"], p["phone"] or "")
    # 挂号页的可选项：只给"在职医生 + 启用科室"，离职医生与停用科室不再出现在下拉里
    docs = query_all(
        """
        SELECT doc.doctor_id, doc.doctor_name, doc.title, doc.dept_id, d.dept_name
        FROM doctor doc
        LEFT JOIN department d ON doc.dept_id = d.dept_id
        WHERE doc.status = '在职' AND d.status = '启用'
        ORDER BY doc.dept_id, doc.doctor_id
        """
    )
    depts = query_all("SELECT dept_id, dept_name FROM department "
                      "WHERE status = '启用' ORDER BY dept_id")

    # 近期排班（含剩余号源），给前端做"还剩多少号"的提示
    sched_rows = query_all(
        """
        SELECT s.doctor_id, s.work_date, s.time_period,
               s.total_quota, s.booked_count, s.status
        FROM doctor_schedule s
        WHERE s.work_date BETWEEN CURDATE() AND CURDATE() + INTERVAL %s DAY
        ORDER BY s.work_date, s.doctor_id, s.time_period
        """,
        (BOOK_AHEAD_DAYS,),
    )
    schedules_json = json_for_script(
        [{"doctor_id": s["doctor_id"], "work_date": str(s["work_date"]),
          "time_period": s["time_period"], "total": s["total_quota"],
          "booked": s["booked_count"], "status": s["status"],
          "left": max(0, s["total_quota"] - s["booked_count"])} for s in sched_rows]
    )

    doctors_json = json_for_script(
        [{"doctor_id": d["doctor_id"], "doctor_name": d["doctor_name"],
          "title": d["title"] or "", "dept_id": d["dept_id"]} for d in docs]
    )

    parent = None
    if prefill["visit_type"] == "复诊" and prefill["parent_reg_id"]:
        parent = query_one(
            """
            SELECT r.reg_id, r.reg_date, p.patient_name, doc.doctor_name
            FROM registration r
            JOIN patient p   ON r.patient_id = p.patient_id
            JOIN doctor  doc ON r.doctor_id  = doc.doctor_id
            WHERE r.reg_id = %s
            """,
            (prefill["parent_reg_id"],),
        )

    return render_template(
        "add.html",
        patients=patients,
        depts=depts,
        doctors_json=doctors_json,
        schedules_json=schedules_json,
        book_ahead_days=BOOK_AHEAD_DAYS,
        today=date.today().isoformat(),
        max_date=(date.today() + timedelta(days=BOOK_AHEAD_DAYS)).isoformat(),
        time_periods=TIME_PERIODS,
        prefill=prefill,
        parent=parent,
        # POST 校验失败时把用户填过的内容原样回填，不用重填一遍
        form=request.form if request.method == "POST" else None,
    )


# ---------------- 药品管理：库存列表 + 入库 / 出库 ----------------
@app.route("/medicines")
def medicines():
    kw = clean_kw(request.args.get("kw"))
    level = (request.args.get("level") or "").strip()      # warn / danger
    cat = clean_kw(request.args.get("cat"))                # 药品分类
    mstat = (request.args.get("mstat") or "").strip()      # 在用 / 停用
    if mstat not in MEDICINE_STATUSES:
        mstat = ""
    page = request.args.get("page", type=int) or 1

    where, args = [], []
    if kw:
        where.append("(medicine_name LIKE %s OR manufacturer LIKE %s)")
        args += ["%" + kw + "%", "%" + kw + "%"]
    if cat:
        where.append("category = %s")
        args.append(cat)
    if mstat:
        where.append("status = %s")
        args.append(mstat)
    if level == "warn":
        where.append("stock < %s AND stock >= %s")
        args += [LOW_STOCK, DANGER_STOCK]
    elif level == "danger":
        where.append("stock < %s")
        args.append(DANGER_STOCK)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    total = query_one(f"SELECT COUNT(*) AS n FROM medicine {where_sql}", tuple(args))["n"]
    page, total_pages, offset = paginate(total, page)

    rows = query_all(
        f"""
        SELECT medicine_id, medicine_name, category, spec, unit, price, stock,
               manufacturer, status
        FROM medicine
        {where_sql}
        ORDER BY medicine_id
        LIMIT %s OFFSET %s
        """,
        tuple(args) + (PAGE_SIZE, offset),
    )
    for r in rows:
        r["level"] = stock_level(r["stock"])

    # 分类清单：按药品编号首次出现的顺序排，和 seed 里的书写顺序一致
    categories = query_all(
        """
        SELECT category, COUNT(*) AS cnt, MIN(medicine_id) AS first_id
        FROM medicine
        WHERE category IS NOT NULL AND category <> ''
        GROUP BY category
        ORDER BY first_id
        """
    )

    # 顶部统计
    summary = query_one(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN stock < %s THEN 1 ELSE 0 END) AS low_cnt,
               SUM(CASE WHEN stock < %s THEN 1 ELSE 0 END) AS danger_cnt,
               SUM(CASE WHEN status = '在用' THEN 1 ELSE 0 END) AS active_cnt,
               SUM(CASE WHEN status = '停用' THEN 1 ELSE 0 END) AS off_cnt,
               SUM(stock * price) AS asset
        FROM medicine
        """,
        (LOW_STOCK, DANGER_STOCK),
    )

    level_counts = {
        "danger": summary["danger_cnt"] or 0,
        "warn": (summary["low_cnt"] or 0) - (summary["danger_cnt"] or 0),
        "ok": summary["total"] - (summary["low_cnt"] or 0),
    }

    pager = page_urls("medicines", {"kw": kw, "level": level, "cat": cat, "mstat": mstat},
                      total_pages, page)

    return render_template("medicines.html", rows=rows, kw=kw, current_level=level,
                           cat=cat, categories=categories, current_mstat=mstat,
                           total=total, page=page, total_pages=total_pages, pager=pager,
                           summary=summary, level_counts=level_counts,
                           low_stock_limit=LOW_STOCK, danger_stock_limit=DANGER_STOCK)


@app.route("/medicine/save", methods=["POST"])
def medicine_save():
    """新增药品 / 修改药品目录信息。

    ⚠️ 这里有个必须守住的规则：新增药品的"期初库存"不能只写进 medicine.stock 就完事。
    本项目的库存是"事件驱动落账"的——每一次库存变动都要在 medicine_stock_log 里留一条流水，
    药品页面的库存是靠流水滚出来的期末值。所以新增时必须在同一个事务里
    既写 stock、又补一条"期初入库"流水，否则这种药的账本从第一天就对不上。
    同理，修改时也不允许直接改 stock（要改就走「入库 / 出库」）。
    """
    mid = request.form.get("medicine_id", type=int) or 0
    back = safe_back("medicines")

    name = (request.form.get("medicine_name") or "").strip()
    cat = clean_kw(request.form.get("category"))
    spec = (request.form.get("spec") or "").strip()
    unit = (request.form.get("unit") or "").strip()
    mfr = (request.form.get("manufacturer") or "").strip()
    price_raw = (request.form.get("price") or "").strip()
    stock_raw = (request.form.get("stock") or "").strip()

    errs = []
    if not name:
        errs.append("药品名称必填")
    elif len(name) > 100:
        errs.append("药品名称不能超过 100 个字")
    if len(spec) > 50:
        errs.append("规格不能超过 50 个字")
    if len(unit) > 10:
        errs.append("单位不能超过 10 个字")
    if len(mfr) > 100:
        errs.append("生产厂家不能超过 100 个字")

    price = None
    try:
        price = float(price_raw)
    except ValueError:
        errs.append("单价必须是数字")
    else:
        if price <= 0 or price > 999999.99:
            errs.append("单价要大于 0 且不超过 999999.99")

    stock = None
    if stock_raw:
        try:
            stock = int(stock_raw)
        except ValueError:
            errs.append("期初库存必须是整数")
        else:
            if stock < 0:
                errs.append("期初库存不能为负数")
    if not mid and stock is None:
        stock = 0
    if errs:
        flash("；".join(errs), "error")
        return redirect(back)

    if mid:
        row = query_one("SELECT medicine_name, stock, unit FROM medicine WHERE medicine_id = %s",
                        (mid,))
        if not row:
            flash("药品不存在", "error")
            return redirect(back)
        if stock is not None and stock != row["stock"]:
            flash("库存不能在这里直接改：请走「入库 / 出库」，"
                  "否则库存流水会对不上账（当前库存 %d%s）"
                  % (row["stock"], row["unit"] or ""), "error")
            return redirect(back)
        execute("UPDATE medicine SET medicine_name = %s, category = %s, spec = %s, unit = %s, "
                "price = %s, manufacturer = %s WHERE medicine_id = %s",
                (name, cat or None, spec or None, unit or None, price, mfr or None, mid))
        log_action("修改药品", "药品 #%d" % mid, "更新 %s 的目录信息" % name)
        flash("药品「%s」的目录信息已更新" % name, "ok")
    else:
        with transaction() as cur:
            cur.execute(
                "INSERT INTO medicine (medicine_name, category, spec, unit, price, stock, "
                "manufacturer, status) VALUES (%s, %s, %s, %s, %s, %s, %s, '在用')",
                (name, cat or None, spec or None, unit or None, price, stock, mfr or None),
            )
            new_id = cur.lastrowid
            if stock:      # 期初库存补一笔入库流水：0 → stock
                cur.execute(
                    "INSERT INTO medicine_stock_log (medicine_id, change_type, quantity, "
                    "before_stock, after_stock, reason, create_time) "
                    "VALUES (%s, '入库', %s, 0, %s, '新增药品期初入库', NOW())",
                    (new_id, stock, stock),
                )
        log_action("新增药品", "药品 #%d" % new_id, "新增 %s，期初库存 %d" % (name, stock))
        flash("已新增药品「%s」（编号 %d，期初库存 %d%s）%s"
              % (name, new_id, stock, unit or "",
                 "，并已在库存流水里记了一笔期初入库" if stock else ""), "ok")
    return redirect(back)


@app.route("/medicine/<int:mid>/toggle", methods=["POST"])
def medicine_toggle(mid):
    """药品停用 / 恢复。停用后不再出现在开处方的下拉里，历史处方与收费快照完整保留。"""
    back = safe_back("medicines")
    row = query_one("SELECT medicine_name, status, stock, unit FROM medicine WHERE medicine_id = %s",
                    (mid,))
    if not row:
        flash("药品不存在", "error")
        return redirect(back)

    if row["status"] == "在用":
        execute("UPDATE medicine SET status = '停用' WHERE medicine_id = %s", (mid,))
        log_action("停用药品", "药品 #%d" % mid, "停用 %s" % row["medicine_name"])
        tip = ("；它还有 %d%s 库存，可以出库清零或原样保留" % (row["stock"], row["unit"] or "")
               if row["stock"] else "")
        flash("药品「%s」已停用，开处方时不再出现%s。历史处方与收费快照不受影响"
              % (row["medicine_name"], tip), "ok")
    else:
        execute("UPDATE medicine SET status = '在用' WHERE medicine_id = %s", (mid,))
        log_action("启用药品", "药品 #%d" % mid, "启用 %s" % row["medicine_name"])
        flash("药品「%s」已恢复在用" % row["medicine_name"], "ok")
    return redirect(back)


@app.route("/medicine/<int:mid>/stock", methods=["POST"])
def medicine_stock(mid):
    """药品入库 / 出库"""
    change_type = request.form.get("change_type", "")
    qty = request.form.get("quantity", type=int)
    reason = (request.form.get("reason") or "").strip()
    back = safe_back("medicines")

    if change_type not in ("入库", "出库"):
        flash("不支持的库存操作", "error")
        return redirect(back)
    if not qty or qty <= 0:
        flash("请输入大于 0 的数量", "error")
        return redirect(back)

    try:
        with transaction() as cur:
            cur.execute(
                "SELECT medicine_name, unit, stock FROM medicine WHERE medicine_id = %s FOR UPDATE",
                (mid,),
            )
            med = cur.fetchone()
            if med is None:
                raise BizError("药品不存在")

            before = med["stock"]
            after = before + qty if change_type == "入库" else before - qty
            if after < 0:
                raise BizError("出库数量超过现有库存（现有 %d%s），无法出库"
                               % (before, med["unit"] or ""))

            cur.execute("UPDATE medicine SET stock = %s WHERE medicine_id = %s", (after, mid))
            cur.execute(
                """
                INSERT INTO medicine_stock_log
                    (medicine_id, change_type, quantity, before_stock, after_stock,
                     reason, create_time)
                VALUES (%s, %s, %s, %s, %s, %s, NOW())
                """,
                (mid, change_type, qty, before, after,
                 reason or ("采购入库" if change_type == "入库" else "手动出库")),
            )

        log_action(change_type, "药品 #%d %s" % (mid, med["medicine_name"]),
                   "%s %d%s，库存 %d → %d，原因：%s"
                   % (change_type, qty, med["unit"] or "", before, after,
                      reason or ("采购入库" if change_type == "入库" else "手动出库")))
        flash("%s成功：「%s」%s %d%s，当前库存 %d%s"
              % (change_type, med["medicine_name"], change_type, qty, med["unit"] or "",
                 after, med["unit"] or ""), "ok")
    except BizError as e:
        flash(str(e), "error")
    except Exception as e:
        flash("库存操作失败：%s" % e, "error")

    return redirect(back)


# ---------------- 库存流水 ----------------
@app.route("/stock-logs")
def stock_logs():
    mid = request.args.get("medicine_id", type=int)
    ctype = (request.args.get("change_type") or "").strip()
    page = request.args.get("page", type=int) or 1

    where, args = [], []
    if mid:
        where.append("l.medicine_id = %s")
        args.append(mid)
    if ctype in ("入库", "出库"):
        where.append("l.change_type = %s")
        args.append(ctype)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    total = query_one(f"SELECT COUNT(*) AS n FROM medicine_stock_log l {where_sql}",
                      tuple(args))["n"]
    page, total_pages, offset = paginate(total, page)

    rows = query_all(
        f"""
        SELECT l.log_id, l.change_type, l.quantity, l.before_stock, l.after_stock,
               l.reason, l.create_time,
               m.medicine_name, m.spec, m.unit
        FROM medicine_stock_log l
        JOIN medicine m ON l.medicine_id = m.medicine_id
        {where_sql}
        ORDER BY l.create_time DESC, l.log_id DESC
        LIMIT %s OFFSET %s
        """,
        tuple(args) + (PAGE_SIZE, offset),
    )

    meds = query_all("SELECT medicine_id, medicine_name FROM medicine ORDER BY medicine_id")
    pager = page_urls("stock_logs", {"medicine_id": mid, "change_type": ctype},
                      total_pages, page)

    return render_template("stock_logs.html", rows=rows, meds=meds,
                           current_medicine=mid, current_type=ctype,
                           total=total, page=page, total_pages=total_pages, pager=pager)


# ---------------- 导出 Excel ----------------
@app.route("/export/registrations")
def export_registrations():
    f = build_reg_filter()
    rows = query_all(
        f"""
        {REG_SELECT}
        {f['where_sql']}
        ORDER BY r.reg_date DESC, r.reg_id DESC
        """,
        f["args"],
    )

    data = []
    for r in rows:
        data.append([
            r["reg_id"], r["reg_date"].strftime("%Y-%m-%d %H:%M"),
            r["patient_name"], r["gender"], calc_age(r["birth_date"]) or "",
            mask_phone(r["phone"]),
            r["dept_name"], r["doctor_name"], r["title"] or "",
            r["visit_type"], float(r["reg_fee"]), r["reg_status"],
            r["med_cnt"] or 0,
        ])

    buf = build_xlsx(
        "挂号记录",
        ["挂号编号", "挂号时间", "患者", "性别", "年龄", "联系电话",
         "科室", "接诊医生", "职称", "就诊类型", "挂号费(元)", "状态", "处方药品种数"],
        data,
        widths=[10, 18, 12, 6, 6, 14, 12, 12, 12, 10, 10, 10, 12],
    )
    return send_file(buf, as_attachment=True, download_name="挂号记录.xlsx",
                     mimetype=XLSX_MIME)


@app.route("/export/patients")
def export_patients():
    kw = clean_kw(request.args.get("kw"))
    where_sql, args = "", ()
    if kw:
        where_sql = "WHERE p.patient_name LIKE %s OR p.phone LIKE %s"
        args = ("%" + kw + "%", "%" + kw + "%")

    rows = query_all(
        f"""
        SELECT p.patient_name, p.gender, p.birth_date, p.id_card, p.phone, p.address,
               COUNT(r.reg_id) AS reg_cnt,
               SUM(CASE WHEN r.reg_status = '已就诊' THEN 1 ELSE 0 END) AS visited_cnt,
               SUM(CASE WHEN r.reg_status = '已挂号' THEN 1 ELSE 0 END) AS waiting_cnt,
               MAX(r.reg_date) AS last_reg
        FROM patient p
        LEFT JOIN registration r ON p.patient_id = r.patient_id
        {where_sql}
        GROUP BY p.patient_id, p.patient_name, p.gender, p.birth_date,
                 p.id_card, p.phone, p.address
        ORDER BY p.patient_id
        """,
        args,
    )

    data = []
    for r in rows:
        data.append([
            r["patient_name"], r["gender"], calc_age(r["birth_date"]) or "",
            mask_id_card(r["id_card"]), mask_phone(r["phone"]), r["address"] or "",
            r["reg_cnt"] or 0, r["visited_cnt"] or 0, r["waiting_cnt"] or 0,
            r["last_reg"].strftime("%Y-%m-%d") if r["last_reg"] else "",
        ])

    buf = build_xlsx(
        "患者档案",
        ["姓名", "性别", "年龄", "身份证号", "联系电话", "住址",
         "累计挂号", "已就诊", "待就诊", "最近挂号"],
        data,
        widths=[12, 6, 6, 22, 14, 30, 10, 10, 10, 14],
    )
    return send_file(buf, as_attachment=True, download_name="患者档案.xlsx",
                     mimetype=XLSX_MIME)


# ---------------- 打印挂号单 ----------------
@app.route("/registration/<int:rid>/print")
def print_registration(rid):
    reg = query_one(
        """
        SELECT r.reg_id, r.reg_date, r.reg_fee, r.reg_status, r.visit_type,
               p.patient_id, p.patient_name, p.gender, p.birth_date, p.phone,
               p.id_card, p.address,
               doc.doctor_name, doc.title, doc.specialty,
               d.dept_name, d.dept_location, d.dept_phone
        FROM registration r
        JOIN patient    p   ON r.patient_id = p.patient_id
        JOIN doctor     doc ON r.doctor_id  = doc.doctor_id
        JOIN department d   ON r.dept_id    = d.dept_id
        WHERE r.reg_id = %s
        """,
        (rid,),
    )
    if reg is None:
        abort(404)
    reg["age"] = calc_age(reg["birth_date"])
    return render_template("print_registration.html", reg=reg, now=datetime.now())


# ---------------- 导出收费记录 Excel ----------------
@app.route("/export/charges")
def export_charges():
    f = build_charge_filter()
    rows = query_all(CHARGE_SELECT + f["where_sql"]
                     + " ORDER BY c.charge_time DESC, c.charge_id DESC", f["args"])

    data = []
    for r in rows:
        data.append([
            r["charge_no"], r["charge_time"].strftime("%Y-%m-%d %H:%M") if r["charge_time"] else "",
            r["patient_name"], r["dept_name"], r["doctor_name"],
            float(r["reg_fee"]), float(r["drug_fee"]), float(r["total_fee"]),
            r["pay_method"], r["charge_status"], r["operator"] or "",
            r["refund_time"].strftime("%Y-%m-%d %H:%M") if r["refund_time"] else "",
            r["refund_reason"] or "",
        ])

    buf = build_xlsx(
        "收费记录",
        ["收费单号", "收费时间", "患者", "科室", "医生", "挂号费", "药费", "合计",
         "支付方式", "状态", "收费员", "退费时间", "退费原因"],
        data,
        widths=[20, 18, 12, 12, 12, 10, 10, 10, 10, 10, 10, 18, 20],
    )
    return send_file(buf, as_attachment=True, download_name="收费记录.xlsx",
                     mimetype=XLSX_MIME)


# ---------------- 操作日志（仅管理员）----------------
@app.route("/logs")
def logs():
    action = (request.args.get("action") or "").strip()
    kw = clean_kw(request.args.get("kw"))
    page = request.args.get("page", type=int) or 1

    where, args = [], []
    if action:
        where.append("action = %s")
        args.append(action)
    if kw:
        where.append("(staff_name LIKE %s OR target LIKE %s OR detail LIKE %s)")
        args += ["%" + kw + "%"] * 3
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    total = query_one("SELECT COUNT(*) AS n FROM operation_log " + where_sql, tuple(args))["n"]
    page, total_pages, offset = paginate(total, page)

    rows = query_all(
        "SELECT * FROM operation_log " + where_sql
        + " ORDER BY create_time DESC, log_id DESC LIMIT %s OFFSET %s",
        tuple(args) + (PAGE_SIZE, offset),
    )

    base_args = {"action": action, "kw": kw}
    return render_template(
        "logs.html", rows=rows, action_counts={r["action"]: r["n"] for r in query_all(
            "SELECT action, COUNT(*) AS n FROM operation_log GROUP BY action ORDER BY n DESC")},
        current_action=action, kw=kw, total=total, page=page, total_pages=total_pages,
        pager=page_urls("logs", base_args, total_pages, page),
        prev_url=url_for("logs", **clean_args({**base_args, "page": page - 1})) if page > 1 else None,
        next_url=url_for("logs", **clean_args({**base_args, "page": page + 1})) if page < total_pages else None,
    )


# ============================================================
# 基础数据维护（主数据）：科室 / 医生 / 员工账号
# ------------------------------------------------------------
# 三处共同的设计取舍，面试可以讲：
#   1. 一律"改状态停用"，不做物理删除。
#      科室牵着医生、医生牵着病历与挂号，删一条会连带搞出一堆孤儿数据；
#      停用只影响"新增时的候选下拉"，列表与历史记录照常显示。
#   2. 唯一的硬约束交给数据库（唯一键、外键、CHECK），应用层只做友好提示。
#   3. 员工账号是权限系统的根，动它之前先做"防自锁"检查——
#      不允许停用/降级当前登录的自己，也不允许干掉最后一个启用中的管理员。
# ============================================================

# ---------------- 科室 ----------------
@app.route("/depts")
def depts():
    kw = clean_kw(request.args.get("kw"))
    status = (request.args.get("status") or "").strip()
    if status not in DEPT_STATUSES:
        status = ""

    where, args = [], []
    if kw:
        where.append("(d.dept_name LIKE %s OR d.dept_location LIKE %s OR d.dept_phone LIKE %s)")
        args += ["%" + kw + "%"] * 3
    if status:
        where.append("d.status = %s")
        args.append(status)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    rows = query_all(
        f"""
        SELECT d.dept_id, d.dept_name, d.dept_location, d.dept_phone, d.status, d.create_time,
               (SELECT COUNT(*) FROM doctor x WHERE x.dept_id = d.dept_id)               AS doctor_cnt,
               (SELECT COUNT(*) FROM doctor x WHERE x.dept_id = d.dept_id
                                                  AND x.status = '在职')                 AS active_doctor_cnt,
               (SELECT COUNT(*) FROM registration r WHERE r.dept_id = d.dept_id)         AS reg_cnt,
               (SELECT COUNT(*) FROM doctor_schedule s JOIN doctor x ON s.doctor_id = x.doctor_id
                 WHERE x.dept_id = d.dept_id AND s.work_date >= CURDATE())               AS future_sched
        FROM department d
        {where_sql}
        ORDER BY d.dept_id
        """,
        tuple(args),
    )

    all_rows = query_all("SELECT status FROM department")
    summary = {
        "total": len(all_rows),
        "on": sum(1 for r in all_rows if r["status"] == "启用"),
        "off": sum(1 for r in all_rows if r["status"] == "停用"),
        "shown": len(rows),
    }
    return render_template("depts.html", rows=rows, kw=kw,
                           current_status=status, summary=summary)


@app.route("/dept/save", methods=["POST"])
def dept_save():
    """新增或修改科室：带了 dept_id 就是改，不带就是新增"""
    did = request.form.get("dept_id", type=int) or 0
    name = (request.form.get("dept_name") or "").strip()
    loc = (request.form.get("dept_location") or "").strip()
    phone = (request.form.get("dept_phone") or "").strip()

    errs = []
    if not name:
        errs.append("科室名称必填")
    elif len(name) > 50:
        errs.append("科室名称不能超过 50 个字")
    if len(loc) > 100:
        errs.append("科室位置不能超过 100 个字")
    if len(phone) > 20:
        errs.append("科室电话不能超过 20 个字")
    # 科室名没有数据库唯一键，这里做应用层查重（改名时排除自己那条）
    if name and query_one("SELECT dept_id FROM department WHERE dept_name = %s AND dept_id <> %s",
                          (name, did)):
        errs.append("已经有叫「%s」的科室了" % name)
    if errs:
        flash("；".join(errs), "error")
        return redirect(url_for("depts"))

    if did:
        if not query_one("SELECT 1 AS x FROM department WHERE dept_id = %s", (did,)):
            flash("科室不存在", "error")
            return redirect(url_for("depts"))
        execute("UPDATE department SET dept_name = %s, dept_location = %s, dept_phone = %s "
                "WHERE dept_id = %s",
                (name, loc or None, phone or None, did))
        log_action("修改科室", "科室 #%d" % did, "改名为 %s" % name)
        flash("科室「%s」已更新" % name, "ok")
    else:
        new_id = insert("INSERT INTO department (dept_name, dept_location, dept_phone) "
                        "VALUES (%s, %s, %s)", (name, loc or None, phone or None))
        log_action("新增科室", "科室 #%d" % new_id, "新增科室 %s" % name)
        flash("已新增科室「%s」（编号 %d）" % (name, new_id), "ok")
    return redirect(url_for("depts"))


@app.route("/dept/<int:did>/toggle", methods=["POST"])
def dept_toggle(did):
    row = query_one("SELECT dept_name, status FROM department WHERE dept_id = %s", (did,))
    if not row:
        flash("科室不存在", "error")
        return redirect(url_for("depts"))

    if row["status"] == "启用":
        n = query_one("SELECT COUNT(*) AS n FROM doctor WHERE dept_id = %s AND status = '在职'",
                      (did,))["n"]
        execute("UPDATE department SET status = '停用' WHERE dept_id = %s", (did,))
        log_action("停用科室", "科室 #%d" % did, "停用 %s" % row["dept_name"])
        tip = "；注意该科室下还有 %d 名在职医生，挂号时会选不到这个科室" % n if n else ""
        flash("科室「%s」已停用%s，历史挂号记录不受影响" % (row["dept_name"], tip), "ok")
    else:
        execute("UPDATE department SET status = '启用' WHERE dept_id = %s", (did,))
        log_action("启用科室", "科室 #%d" % did, "启用 %s" % row["dept_name"])
        flash("科室「%s」已恢复启用" % row["dept_name"], "ok")
    return redirect(url_for("depts"))


# ---------------- 医生 ----------------
@app.route("/doctors")
def doctors():
    kw = clean_kw(request.args.get("kw"))
    dept_id = request.args.get("dept_id", type=int)
    status = (request.args.get("status") or "").strip()
    if status not in DOCTOR_STATUSES:
        status = ""

    where, args = [], []
    if kw:
        where.append("(d.doctor_name LIKE %s OR d.specialty LIKE %s OR d.phone LIKE %s)")
        args += ["%" + kw + "%"] * 3
    if dept_id:
        where.append("d.dept_id = %s")
        args.append(dept_id)
    if status:
        where.append("d.status = %s")
        args.append(status)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    rows = query_all(
        f"""
        SELECT d.doctor_id, d.doctor_name, d.gender, d.title, d.dept_id, d.specialty,
               d.phone, d.status, d.create_time, dp.dept_name, dp.status AS dept_status,
               (SELECT COUNT(*) FROM registration r WHERE r.doctor_id = d.doctor_id)      AS reg_cnt,
               (SELECT COUNT(*) FROM medical_record m WHERE m.doctor_id = d.doctor_id)     AS rec_cnt,
               (SELECT COUNT(*) FROM doctor_schedule s WHERE s.doctor_id = d.doctor_id
                                                        AND s.work_date >= CURDATE())       AS future_sched
        FROM doctor d LEFT JOIN department dp ON d.dept_id = dp.dept_id
        {where_sql}
        ORDER BY d.dept_id, d.doctor_id
        """,
        tuple(args),
    )

    all_rows = query_all("SELECT status FROM doctor")
    summary = {
        "total": len(all_rows),
        "on": sum(1 for r in all_rows if r["status"] == "在职"),
        "off": sum(1 for r in all_rows if r["status"] == "离职"),
        "shown": len(rows),
    }
    # 科室下拉列全部科室（含已停用的），否则编辑老医生时他的科室会选不回来
    depts_all = query_all("SELECT dept_id, dept_name, status FROM department ORDER BY dept_id")
    return render_template("doctors.html", rows=rows, kw=kw, depts=depts_all,
                           current_dept=dept_id, current_status=status,
                           summary=summary, titles=DOCTOR_TITLES)


@app.route("/doctor/save", methods=["POST"])
def doctor_save():
    """新增或修改医生：带了 doctor_id 就是改，不带就是新增"""
    did = request.form.get("doctor_id", type=int) or 0
    name = (request.form.get("doctor_name") or "").strip()
    gender = (request.form.get("gender") or "").strip()
    title = (request.form.get("title") or "").strip()
    dept_id = request.form.get("dept_id", type=int)
    specialty = (request.form.get("specialty") or "").strip()
    phone = (request.form.get("phone") or "").strip()

    errs = []
    # 数据库里 doctor_name / gender 是 NOT NULL，这两项必须校验
    if not name:
        errs.append("医生姓名必填")
    elif len(name) > 30:
        errs.append("医生姓名不能超过 30 个字")
    if gender not in GENDERS:
        errs.append("请选择性别（男 / 女）")
    if title and title not in DOCTOR_TITLES:
        errs.append("职称只能是：%s" % " / ".join(DOCTOR_TITLES))
    if len(specialty) > 100:
        errs.append("擅长领域不能超过 100 个字")
    if phone and not re.match(r"^1[3-9]\d{9}$", phone):
        errs.append("手机号格式不正确（应为 11 位数字）")
    # dept_id 上有外键，先自己查一遍，别把数据库的 1452 错误直接甩给用户
    if dept_id and not query_one("SELECT 1 AS x FROM department WHERE dept_id = %s", (dept_id,)):
        errs.append("所选科室不存在")
    if errs:
        flash("；".join(errs), "error")
        return redirect(url_for("doctors"))

    if did:
        if not query_one("SELECT 1 AS x FROM doctor WHERE doctor_id = %s", (did,)):
            flash("医生不存在", "error")
            return redirect(url_for("doctors"))
        execute("UPDATE doctor SET doctor_name = %s, gender = %s, title = %s, dept_id = %s, "
                "specialty = %s, phone = %s WHERE doctor_id = %s",
                (name, gender, title or None, dept_id, specialty or None, phone or None, did))
        log_action("修改医生", "医生 #%d" % did, "更新 %s 的档案" % name)
        flash("医生「%s」的档案已更新" % name, "ok")
    else:
        new_id = insert("INSERT INTO doctor (doctor_name, gender, title, dept_id, specialty, phone) "
                        "VALUES (%s, %s, %s, %s, %s, %s)",
                        (name, gender, title or None, dept_id, specialty or None, phone or None))
        log_action("新增医生", "医生 #%d" % new_id, "新增医生 %s" % name)
        flash("已新增医生「%s」（编号 %d）。到「排班号源」给他放号后即可挂号"
              % (name, new_id), "ok")
    return redirect(url_for("doctors"))


@app.route("/doctor/<int:did>/toggle", methods=["POST"])
def doctor_toggle(did):
    """医生离职 / 复职。离职只改状态，历史病历、挂号、排班全部保留。"""
    row = query_one("SELECT doctor_name, status FROM doctor WHERE doctor_id = %s", (did,))
    if not row:
        flash("医生不存在", "error")
        return redirect(url_for("doctors"))

    if row["status"] == "在职":
        # 他还挂着"今天及以后"的号怎么办？提示清楚，但允许离职（现实里医生离职，
        # 已挂的号由医院去改约/退号，系统这边保留记录即可）
        n = query_one("SELECT COUNT(*) AS n FROM doctor_schedule "
                      "WHERE doctor_id = %s AND work_date >= CURDATE()", (did,))["n"]
        execute("UPDATE doctor SET status = '离职' WHERE doctor_id = %s", (did,))
        log_action("医生离职", "医生 #%d" % did, "标记 %s 离职" % row["doctor_name"])
        tip = ("；他还有 %d 个未过期的排班，挂号时将不再出现他，"
               "需要的话请到「排班号源」逐条停诊" % n) if n else ""
        flash("已标记「%s」离职%s。他的历史病历与挂号记录完整保留"
              % (row["doctor_name"], tip), "ok")
    else:
        execute("UPDATE doctor SET status = '在职' WHERE doctor_id = %s", (did,))
        log_action("医生复职", "医生 #%d" % did, "恢复 %s 在职" % row["doctor_name"])
        flash("「%s」已恢复在职" % row["doctor_name"], "ok")
    return redirect(url_for("doctors"))


# ---------------- 员工账号与角色（仅管理员）----------------
def _active_admin_count(exclude_id=None):
    """系统里"启用中的管理员"还剩几个"""
    if exclude_id:
        row = query_one("SELECT COUNT(*) AS n FROM staff "
                        "WHERE role = '管理员' AND status = 1 AND staff_id <> %s", (exclude_id,))
    else:
        row = query_one("SELECT COUNT(*) AS n FROM staff WHERE role = '管理员' AND status = 1")
    return row["n"]


def _guard_admin_lockout(target_id, new_role=None, new_status=None):
    """动员工账号前的"防自锁"检查，返回错误信息，没问题返回 None。

    两道锁，都是真实运维里踩过的坑：
      1. 不允许停用/降级"当前登录的自己"——点错一下就把自己关在门外，只能去改数据库。
      2. 不允许干掉最后一个"启用中的管理员"——否则系统管理模块再也没人能进。
    """
    me = session.get("staff_id")
    row = query_one("SELECT username, real_name, role, status FROM staff WHERE staff_id = %s",
                    (target_id,))
    if not row:
        return "员工不存在"

    role = row["role"] if new_role is None else new_role
    status = row["status"] if new_status is None else new_status

    was_admin = (row["role"] == "管理员" and row["status"])
    still_admin = (role == "管理员" and status)
    if was_admin and not still_admin:
        if target_id == me:
            return "不能停用或降级当前登录的自己，否则会把自己锁在系统外"
        if _active_admin_count(exclude_id=target_id) == 0:
            return "这是系统里最后一个启用中的管理员，停用后没人能再进入系统管理"
    return None


@app.route("/staff")
def staff_list():
    rows = query_all(
        """
        SELECT s.*, (SELECT COUNT(*) FROM operation_log o WHERE o.staff_id = s.staff_id) AS op_cnt
        FROM staff s ORDER BY s.staff_id
        """
    )
    me = session.get("staff_id")
    active_admin = _active_admin_count()
    for r in rows:
        # 两个"不能动"的标记，模板据此把按钮置灰并说明原因
        r["is_me"] = (r["staff_id"] == me)
        r["last_admin"] = (r["role"] == "管理员" and r["status"] and active_admin <= 1)
    # 每个角色有哪些权限，展示成表格方便看"谁能干什么"
    role_table = [{"role": r, "perms": sorted(p)} for r, p in ROLE_PERMS.items()]
    return render_template("staff.html", rows=rows, role_table=role_table,
                           roles=list(ROLE_PERMS.keys()), active_admin=active_admin)


@app.route("/staff/save", methods=["POST"])
def staff_save():
    """新增或修改员工账号：带了 staff_id 就是改，不带就是新增"""
    sid = request.form.get("staff_id", type=int) or 0
    username = (request.form.get("username") or "").strip()
    real_name = (request.form.get("real_name") or "").strip()
    role = (request.form.get("role") or "").strip()
    phone = (request.form.get("phone") or "").strip()
    password = request.form.get("password") or ""

    errs = []
    if not username:
        errs.append("登录账号必填")
    elif not re.match(r"^[A-Za-z0-9_]{3,30}$", username):
        errs.append("登录账号只能用字母 / 数字 / 下划线，3~30 位")
    if not real_name:
        errs.append("姓名必填")
    elif len(real_name) > 30:
        errs.append("姓名不能超过 30 个字")
    if role not in ROLE_PERMS:
        errs.append("角色只能是：%s" % " / ".join(ROLE_PERMS.keys()))
    if phone and not re.match(r"^1[3-9]\d{9}$", phone):
        errs.append("手机号格式不正确（应为 11 位数字）")
    if not sid and not password:
        errs.append("新增员工必须设置初始密码")
    elif password and len(password) < 6:
        errs.append("密码至少 6 位")
    if username and query_one("SELECT staff_id FROM staff WHERE username = %s AND staff_id <> %s",
                              (username, sid)):
        errs.append("登录账号「%s」已被占用" % username)
    if errs:
        flash("；".join(errs), "error")
        return redirect(url_for("staff_list"))

    if sid:
        err = _guard_admin_lockout(sid, new_role=role)
        if err:
            flash(err, "error")
            return redirect(url_for("staff_list"))
        if password:
            execute("UPDATE staff SET username = %s, real_name = %s, role = %s, phone = %s, "
                    "password_hash = %s WHERE staff_id = %s",
                    (username, real_name, role, phone or None,
                     generate_password_hash(password), sid))
        else:
            execute("UPDATE staff SET username = %s, real_name = %s, role = %s, phone = %s "
                    "WHERE staff_id = %s",
                    (username, real_name, role, phone or None, sid))
        log_action("修改员工", "员工 #%d" % sid, "改为 %s（%s）" % (real_name, role))
        flash("员工「%s」已更新%s" % (real_name, "，密码已重置" if password else ""), "ok")
    else:
        new_id = insert("INSERT INTO staff (username, password_hash, real_name, role, phone) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (username, generate_password_hash(password), real_name, role,
                         phone or None))
        log_action("新增员工", "员工 #%d" % new_id, "新增 %s（%s）" % (real_name, role))
        flash("已新增员工「%s」，登录账号 %s" % (real_name, username), "ok")
    return redirect(url_for("staff_list"))


@app.route("/staff/<int:sid>/toggle", methods=["POST"])
def staff_toggle(sid):
    """停用 / 启用员工的登录权限（保留账号，不删）"""
    row = query_one("SELECT real_name, role, status FROM staff WHERE staff_id = %s", (sid,))
    if not row:
        flash("员工不存在", "error")
        return redirect(url_for("staff_list"))

    if row["status"]:
        err = _guard_admin_lockout(sid, new_status=0)
        if err:
            flash(err, "error")
            return redirect(url_for("staff_list"))
        execute("UPDATE staff SET status = 0 WHERE staff_id = %s", (sid,))
        log_action("停用员工", "员工 #%d" % sid, "停用 %s" % row["real_name"])
        flash("已停用「%s」，该账号将无法登录，历史操作日志保留" % row["real_name"], "ok")
    else:
        execute("UPDATE staff SET status = 1 WHERE staff_id = %s", (sid,))
        log_action("启用员工", "员工 #%d" % sid, "启用 %s" % row["real_name"])
        flash("已恢复「%s」的登录权限" % row["real_name"], "ok")
    return redirect(url_for("staff_list"))


@app.route("/staff/<int:sid>/password", methods=["POST"])
def staff_password(sid):
    """重置某个员工的登录密码"""
    pwd = request.form.get("password") or ""
    row = query_one("SELECT real_name FROM staff WHERE staff_id = %s", (sid,))
    if not row:
        flash("员工不存在", "error")
        return redirect(url_for("staff_list"))
    if len(pwd) < 6:
        flash("新密码至少 6 位", "error")
        return redirect(url_for("staff_list"))

    execute("UPDATE staff SET password_hash = %s WHERE staff_id = %s",
            (generate_password_hash(pwd), sid))
    log_action("重置密码", "员工 #%d" % sid, "重置 %s 的登录密码" % row["real_name"])
    flash("已重置「%s」的登录密码" % row["real_name"], "ok")
    return redirect(url_for("staff_list"))


# ---------------- 数据存档：把整库导成快照，乱操作后一键倒回去 ----------------
@app.route("/backups")
def backups():
    """存档列表：当前库行数 + 磁盘上所有快照"""
    snaps = snapshot.list_snapshots()
    counts = snapshot.table_counts()
    return render_template(
        "backups.html",
        snaps=snaps,
        counts=counts,
        total=sum(counts.values()),
        baseline_name=snapshot.BASELINE_NAME,
        dir_name=snapshot.SNAPSHOT_DIR.name,
    )


@app.route("/backups/create", methods=["POST"])
def backup_create():
    """把当前数据库导出成一个命名快照"""
    name = snapshot.safe_name(request.form.get("name"))
    if not name:
        flash("请先填写存档名称", "error")
        return redirect(url_for("backups"))

    existed = snapshot.exists(name)
    try:
        info = snapshot.create_snapshot(
            name, session.get("real_name") or session.get("username") or "system")
    except Exception as exc:
        flash("存档失败：%s" % exc, "error")
        return redirect(url_for("backups"))

    log_action("新建存档", name, "导出 %d 行数据（%s）" % (info["rows"], "覆盖原有" if existed else "新建"))
    flash("存档「%s」已%s，共 %d 行数据" % (name, "覆盖" if existed else "保存", info["rows"]), "ok")
    return redirect(url_for("backups"))


@app.route("/backups/restore", methods=["POST"])
def backup_restore():
    """用快照覆盖当前数据库"""
    name = snapshot.safe_name(request.form.get("name"))
    try:
        res = snapshot.restore_snapshot(name)
    except FileNotFoundError:
        flash("存档不存在，可能已被删除", "error")
        return redirect(url_for("backups"))
    except Exception as exc:
        flash("恢复失败：%s" % exc, "error")
        return redirect(url_for("backups"))

    log_action("恢复存档", name, "执行 %d 条语句" % res["statements"])
    flash("已恢复到存档「%s」，当前数据已被覆盖为该存档内容" % name, "ok")
    return redirect(url_for("backups"))


@app.route("/backups/delete", methods=["POST"])
def backup_delete():
    """删除一个快照文件"""
    name = snapshot.safe_name(request.form.get("name"))
    if snapshot.delete_snapshot(name):
        log_action("删除存档", name, "删除快照文件")
        flash("已删除存档「%s」" % name, "ok")
    else:
        flash("存档不存在", "error")
    return redirect(url_for("backups"))


if __name__ == "__main__":
    app.run(debug=True, port=5000)
