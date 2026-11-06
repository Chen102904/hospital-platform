"""
用 SQLite 验证数据库设计逻辑（无需安装 MySQL）

说明：SQLite 语法与 MySQL 基本兼容，本项目仅用于本地验证表结构与查询逻辑。
两边确实不同的地方，由下面 translate_functions() 做等价替换，
这样"校验脚本里报红"只会是真正的 SQL 写错，而不是方言差异。

注意：SQLite 只是本地校验手段，真实运行环境仍然是 MySQL 8.0
（生成列、CHECK 约束、行级锁这些特性本脚本只能验语法，不能验行为）。
"""
import sqlite3
import re

conn = sqlite3.connect(":memory:")
cur = conn.cursor()


def translate_functions(sql_text: str) -> str:
    """把 MySQL 专有函数/写法替换成 SQLite 的等价写法"""

    # 日期相减：CURDATE() - INTERVAL 6 DAY  ->  date('now','localtime','-6 days')
    sql_text = re.sub(
        r"(CURDATE\(\)|NOW\(\))\s*-\s*INTERVAL\s+(\d+)\s+DAY",
        lambda m: "date('now','localtime','-{} days')".format(m.group(2)),
        sql_text, flags=re.IGNORECASE)
    # 日期相加：+ INTERVAL n DAY（本项目暂时没用到，顺手支持）
    sql_text = re.sub(
        r"(CURDATE\(\)|NOW\(\))\s*\+\s*INTERVAL\s+(\d+)\s+DAY",
        lambda m: "date('now','localtime','+{} days')".format(m.group(2)),
        sql_text, flags=re.IGNORECASE)

    # 时间差要放在 CURDATE()/NOW() 替换之前做，否则 NOW() 已被换掉、正则匹配不到；
    # 而且 SQLite 没有 TIMESTAMPDIFF / DATEDIFF，统一用儒略日相减换算。
    _FACTOR = {"SECOND": 86400, "MINUTE": 1440, "HOUR": 24, "DAY": 1, "MONTH": 1 / 30.44, "YEAR": 1 / 365.25}

    def _diff(unit, a, b):
        expr = "julianday({1}) - julianday({0})".format(a.strip(), b.strip())
        f = _FACTOR.get((unit or "DAY").upper(), 1)
        if f != 1:
            expr = "(({}) * {:.6f})".format(expr, f)
        return "CAST({} AS INTEGER)".format(expr)

    ts_head = r"TIMESTAMPDIFF\s*\(\s*(SECOND|MINUTE|HOUR|DAY|MONTH|YEAR)\s*,\s*"
    # 先处理 TIMESTAMPDIFF(单位, x, NOW())——第三段带括号，要单独匹配
    sql_text = re.sub(
        ts_head + r"([^,()]+?)\s*,\s*NOW\s*\(\s*\)\s*\)",
        lambda m: _diff(m.group(1), m.group(2), "'now','localtime'"),
        sql_text, flags=re.IGNORECASE)
    # 再处理两段都不带括号的常规写法
    sql_text = re.sub(
        ts_head + r"([^,()]+?)\s*,\s*([^,()]+?)\s*\)",
        lambda m: _diff(m.group(1), m.group(2), m.group(3)),
        sql_text, flags=re.IGNORECASE)
    # DATEDIFF(a, b) 只到"天"，没有单位参数
    sql_text = re.sub(
        r"DATEDIFF\s*\(\s*([^,()]+?)\s*,\s*([^,()]+?)\s*\)",
        lambda m: _diff("DAY", m.group(1), m.group(2)),
        sql_text, flags=re.IGNORECASE)

    # 当前日期 / 当前时间
    sql_text = re.sub(r"\bCURDATE\s*\(\s*\)", "date('now','localtime')", sql_text, flags=re.IGNORECASE)
    sql_text = re.sub(r"\bNOW\s*\(\s*\)", "datetime('now','localtime')", sql_text, flags=re.IGNORECASE)

    # 字符串拼接：CONCAT(a, b) -> (a || b)；本项目里最多两三个参数
    def _concat(m):
        parts = [p.strip() for p in m.group(1).split(",")]
        return "(" + " || ".join(parts) + ")"

    sql_text = re.sub(r"\bCONCAT\s*\(([^()]*)\)", _concat, sql_text, flags=re.IGNORECASE)

    # IF(a,b,c) 在 SQLite 3.48+ 已是内置函数，旧版本退回 IIF
    if sqlite3.sqlite_version_info < (3, 48, 0):
        sql_text = re.sub(r"\bIF\s*\(", "IIF(", sql_text, flags=re.IGNORECASE)

    return sql_text


def run_mysql_sql_as_sqlite(sql_text: str, label: str):
    """把 MySQL 语法粗略转换为 SQLite 可执行，逐条执行"""
    # 去掉 MySQL 专有语法
    sql_text = re.sub(r"DEFAULT CHARACTER SET \w+ COLLATE \w+", "", sql_text)
    sql_text = re.sub(r"DEFAULT CHARACTER SET \w+", "", sql_text)
    sql_text = re.sub(r"ENGINE=\w+", "", sql_text)
    sql_text = re.sub(r"COMMENT\s+'[^']*'", "", sql_text)
    sql_text = re.sub(r"COMMENT\s+'[^']*'", "", sql_text)
    sql_text = sql_text.replace("INT PRIMARY KEY AUTO_INCREMENT", "INTEGER PRIMARY KEY AUTOINCREMENT")
    sql_text = sql_text.replace("AUTO_INCREMENT", "AUTOINCREMENT")
    # 清理行内注释里的 -- 导致的分句问题
    sql_text = re.sub(r"--[^\n]*", "", sql_text)
    sql_text = sql_text.replace("DATETIME DEFAULT CURRENT_TIMESTAMP", "TEXT DEFAULT CURRENT_TIMESTAMP")
    sql_text = translate_functions(sql_text)

    # 移除 DROP DATABASE / CREATE DATABASE / USE
    lines = []
    for line in sql_text.split("\n"):
        s = line.strip()
        if s.upper().startswith("DROP DATABASE"):
            continue
        if s.upper().startswith("CREATE DATABASE"):
            continue
        if s.upper().startswith("USE "):
            continue
        lines.append(line)
    sql_text = "\n".join(lines)

    # 提取语句
    stmts = [s.strip() for s in sql_text.split(";") if s.strip()]
    ok, fail = 0, []
    for st in stmts:
        # 跳过 SQLite 不支持的 MySQL 专有语句
        if re.fullmatch(r"USE\s+\w+", st.strip(), flags=re.IGNORECASE):
            continue
        if re.match(r"SET\s+SQL_SAFE_UPDATES", st.strip(), flags=re.IGNORECASE):
            continue
        st = re.sub(r"^\s*USE\s+\w+\s*", "", st, flags=re.IGNORECASE).strip()
        if not st:
            continue
        try:
            cur.execute(st)
            ok += 1
        except Exception as e:
            fail.append((st[:60], str(e)))
    print(f"[{label}] 成功 {ok} 条，失败 {len(fail)} 条")
    for f in fail:
        print(f"   失败: {f[0]}... -> {f[1]}")
    return fail


print("=" * 60)
print("【第 1 步】建表")
print("=" * 60)
with open("01_create_tables.sql", encoding="utf-8") as f:
    create_sql = f.read()
fail1 = run_mysql_sql_as_sqlite(create_sql, "建表")

print()
print("=" * 60)
print("【第 2 步】插入测试数据")
print("=" * 60)
with open("02_insert_data.sql", encoding="utf-8") as f:
    insert_sql = f.read()
fail2 = run_mysql_sql_as_sqlite(insert_sql, "插入")

print()
print("=" * 60)
print("【第 3 步】数据核对")
print("=" * 60)
tables = ["department", "doctor", "patient", "registration",
          "medical_record", "medicine", "prescription_detail", "admission"]
for t in tables:
    cur.execute(f"SELECT COUNT(*) FROM {t}")
    print(f"  {t:<22} {cur.fetchone()[0]} 条")

print()
print("=" * 60)
print("【第 4 步】执行查询语句")
print("=" * 60)
with open("03_queries.sql", encoding="utf-8") as f:
    query_sql = f.read()

# 清除 SQL 注释（-- 开头），避免注释与语句粘连，并把 MySQL 专有函数换成 SQLite 等价写法
query_sql = re.sub(r"--[^\n]*", "", query_sql)
query_sql = translate_functions(query_sql)

# 逐条执行查询，展示结果
stmts = [s.strip() for s in query_sql.split(";") if s.strip()]
q_ok, q_fail = 0, []
for st in stmts:
    if st.upper().startswith("USE "):
        continue
    try:
        if re.fullmatch(r"USE\s+\w+", st.strip(), flags=re.IGNORECASE):
            continue
        if re.match(r"SET\s+SQL_SAFE_UPDATES", st.strip(), flags=re.IGNORECASE):
            continue
        st_clean = re.sub(r"^\s*USE\s+\w+\s*", "", st, flags=re.IGNORECASE).strip()
        if not st_clean:
            continue
        cur.execute(st_clean)
        rows = cur.fetchall()
        q_ok += 1
        if len(rows) > 0:
            head = st.replace("\n", " ")[:55]
            print(f"  查询成功({len(rows)}行): {head}...")
    except Exception as e:
        q_fail.append((st[:60], str(e)))

print(f"\n  查询语句：成功 {q_ok} 条，失败 {len(q_fail)} 条")
for f in q_fail:
    print(f"   失败: {f[0]}... -> {f[1]}")

conn.close()
print()
print("=" * 60)
if not fail1 and not fail2 and not q_fail:
    print("全部通过：数据库设计逻辑正确")
else:
    print("存在问题，请查看上方失败项")
print("=" * 60)
