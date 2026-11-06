"""数据快照（存档）：把当前数据库整体导出成一个 SQL 文件，需要时一键倒回去。

为什么不用 mysqldump
    · Windows 上 mysqldump 不一定在 PATH 里，脚本不好保证能在别人机器上跑；
    · 表里有生成列（admission.current_patient_id），mysqldump 处理它要额外参数；
    · 自己导还能顺手算出行数，直接显示在页面上。

快照文件长什么样
    就是一个普通的 .sql 文本，里面每行一条语句，顺序是：
        关外键检查 → 逆序清空所有表 → 正序写入数据 → 开回外键检查
    所以既可以由本模块恢复，也可以直接用 mysql 客户端 source 进去。
"""

import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from db import get_conn, query_all

# 快照文件放这里（跟 Web 项目同一个盘，方便打包带走）
SNAPSHOT_DIR = Path(__file__).resolve().parent / "snapshots"

# 基准快照的固定名字：这就是"定死的那批演示数据"，页面里会单独标出来
BASELINE_NAME = "基准演示数据"

# 表的顺序很关键：写入要按"被引用的表在前"，清空要反过来。
# 顺序错了 MySQL 会因外键约束拒绝执行。
TABLES = [
    "department", "doctor", "patient", "medicine", "staff",
    "doctor_schedule", "registration", "medical_record", "prescription_detail",
    "medicine_stock_log", "charge", "charge_detail", "dispense",
    "admission", "operation_log",
]

NAME_MAX = 30
_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]')


def ensure_dir():
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)


def safe_name(raw):
    """把用户输入的存档名清洗成安全的文件名片段。

    不能直接拿用户输入当路径：`../../` 这种会把文件写到项目外面去。
    """
    name = _ILLEGAL.sub("", (raw or "").strip())
    name = name.strip(". ")
    return name[:NAME_MAX]


def _path(name):
    return SNAPSHOT_DIR / (safe_name(name) + ".sql")


# ----------------------------------------------------------------------
# 把 Python 值转成 SQL 字面量
# ----------------------------------------------------------------------
_ESCAPES = (("\\", "\\\\"), ("'", "\\'"), ("\n", "\\n"), ("\r", "\\r"), ("\x1a", "\\Z"))


def _lit(v):
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float, Decimal)):
        return str(v)
    if isinstance(v, datetime):
        return "'" + v.strftime("%Y-%m-%d %H:%M:%S") + "'"
    if isinstance(v, date):
        return "'" + v.strftime("%Y-%m-%d") + "'"
    if isinstance(v, time):
        return "'" + v.strftime("%H:%M:%S") + "'"
    if isinstance(v, timedelta):
        # TIME 类型在 pymysql 里回来是 timedelta
        total = int(v.total_seconds())
        sign = "-" if total < 0 else ""
        total = abs(total)
        return "'%s%02d:%02d:%02d'" % (sign, total // 3600, total % 3600 // 60, total % 60)
    if isinstance(v, (bytes, bytearray)):
        return "0x" + bytes(v).hex()
    s = str(v)
    for a, b in _ESCAPES:
        s = s.replace(a, b)
    return "'" + s + "'"


def _writable_columns(cur, table):
    """列出可写入的列，跳过生成列。

    注意不要用 EXTRA 里有没有 "GENERATED" 来判断：
    MySQL 给"有默认值"的列标的也是 DEFAULT_GENERATED（例如各种 create_time），
    那种列是要正常导出的，漏了它恢复之后时间就全变成恢复那一刻了。
    真正的生成列只有 STORED GENERATED / VIRTUAL GENERATED，
    特征是 GENERATION_EXPRESSION 非空——本项目里就是 admission.current_patient_id。
    """
    cur.execute(
        """
        SELECT COLUMN_NAME, EXTRA, GENERATION_EXPRESSION
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s
        ORDER BY ORDINAL_POSITION
        """,
        (table,),
    )
    cols = []
    for row in cur.fetchall():
        gen = (row.get("GENERATION_EXPRESSION") or "").strip()
        if gen:
            continue        # 生成列由数据库自己算，插进去会报错
        cols.append(row["COLUMN_NAME"])
    return cols


# ----------------------------------------------------------------------
# 导出
# ----------------------------------------------------------------------
def create_snapshot(name, operator="system"):
    """把当前库导成一个快照文件，返回 {name, path, size, rows}"""
    ensure_dir()
    clean = safe_name(name)
    if not clean:
        raise ValueError("存档名不能为空")

    out = _path(clean)
    conn = get_conn()
    lines = []
    total = 0
    per_table = {}

    try:
        with conn.cursor() as cur:
            for t in TABLES:
                cur.execute("SELECT * FROM `%s`" % t)
                rows = cur.fetchall()
                cols = _writable_columns(cur, t) if rows else []
                per_table[t] = len(rows)
                total += len(rows)

                if rows:
                    collist = ", ".join("`%s`" % c for c in cols)
                    for r in rows:
                        vals = ", ".join(_lit(r.get(c)) for c in cols)
                        lines.append("INSERT INTO `%s` (%s) VALUES (%s);" % (t, collist, vals))
    finally:
        conn.close()

    head = [
        "-- 医院信息管理系统 · 数据快照",
        "-- 存档名称: %s" % clean,
        "-- 创建时间: %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "-- 创建人: %s" % operator,
        "-- 总行数: %d" % total,
        "-- 明细: " + " ".join("%s=%d" % (k, v) for k, v in per_table.items()),
        "-- 说明: 每行一条语句，可直接用 mysql 客户端 source 导入；恢复由数据存档页面完成。",
        "",
        "SET NAMES utf8mb4;",
        "SET FOREIGN_KEY_CHECKS = 0;",
        "",
        "-- 清空（逆序，先删被子表引用的）",
    ]
    head += ["DELETE FROM `%s`;" % t for t in reversed(TABLES)]
    head += ["", "-- 写入（正序）"]

    tail = ["", "SET FOREIGN_KEY_CHECKS = 1;"]

    text = "\n".join(head + lines + tail) + "\n"
    out.write_text(text, encoding="utf-8")

    return {
        "name": clean,
        "path": str(out),
        "size": out.stat().st_size,
        "rows": total,
        "per_table": per_table,
    }


# ----------------------------------------------------------------------
# 列出 / 删除
# ----------------------------------------------------------------------
def _read_head(path, limit=6):
    """读快照文件头部的注释，取回创建时间、创建人、总行数"""
    info = {"created": "", "operator": "", "rows": ""}
    try:
        with path.open("r", encoding="utf-8") as f:
            for _ in range(limit):
                line = f.readline()
                if not line.startswith("--"):
                    break
                if "创建时间:" in line:
                    info["created"] = line.split("创建时间:")[1].strip()
                elif "创建人:" in line:
                    info["operator"] = line.split("创建人:")[1].strip()
                elif "总行数:" in line:
                    info["rows"] = line.split("总行数:")[1].strip()
    except OSError:
        pass
    return info


def list_snapshots():
    """列出磁盘上所有快照，基准快照排最前"""
    ensure_dir()
    items = []
    for p in SNAPSHOT_DIR.glob("*.sql"):
        st = p.stat()
        info = _read_head(p)
        items.append({
            "name": p.stem,
            "size": st.st_size,
            "mtime": datetime.fromtimestamp(st.st_mtime),
            "created": info["created"] or datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "operator": info["operator"] or "—",
            "rows": info["rows"] or "—",
            "is_baseline": p.stem == safe_name(BASELINE_NAME),
        })
    items.sort(key=lambda x: (not x["is_baseline"], -x["mtime"].timestamp()))
    return items


def delete_snapshot(name):
    p = _path(name)
    if p.exists():
        p.unlink()
        return True
    return False


def exists(name):
    return _path(name).exists()


# ----------------------------------------------------------------------
# 恢复
# ----------------------------------------------------------------------
def restore_snapshot(name):
    """把快照文件里的语句按顺序执行回去。

    文件里已经带了"清空 + 写入 + 关外键检查"，所以这里只要照着跑就行。
    整个过程在一个连接里完成：FOREIGN_KEY_CHECKS 是会话级设置，
    换连接就失效了。
    """
    p = _path(name)
    if not p.exists():
        raise FileNotFoundError("存档不存在：%s" % name)

    sql_lines = []
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("--"):
            continue
        sql_lines.append(line)

    conn = get_conn()
    done = 0
    try:
        with conn.cursor() as cur:
            for stmt in sql_lines:
                cur.execute(stmt)
                done += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {"name": safe_name(name), "statements": done}


def table_counts():
    """当前库每张表的行数，页面上用来对比"现在"和"存档时"的差别。

    用一条 UNION ALL 查完，省得开 15 次连接。
    """
    parts = ["SELECT '%s' AS t, COUNT(*) AS c FROM `%s`" % (t, t) for t in TABLES]
    rows = query_all(" UNION ALL ".join(parts))
    return {r["t"]: r["c"] for r in rows}
