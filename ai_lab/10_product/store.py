# -*- coding: utf-8 -*-
"""
第 10 步 · 持久化层

第 9 步的"记忆"是一个 Python 字典：

    SESSIONS = {}      # sid -> [消息...]

它有三个致命问题，任何一条都足以让演示当场翻车：

  ① 进程一重启，全没了。面试前跑过一次，面试时打开是空的。
  ② 只能存两份东西：role 和 content。当时调了哪个工具、花了多少秒、
     为什么被叫停 —— 全丢了。想复盘"它上次怎么答的"根本做不到。
  ③ 想看"我一共问过多少问题"这类统计，只能自己数屏幕。

所以这一步把它搬进 SQLite。SQLite 不是"将就用的数据库"——
单机、单文件、零配置、Python 标准库自带，正好是这个场景的最优解。
（真上多用户再换 MySQL，接口不用变。）

──────────────────────────────────────────────────────────────────────
表只有两张：

    chat_session  一次对话一行
        id          32 位 hex，跟 cookie 里的 sid 是同一个东西
        title       标题，取第一条问题的前 24 字，可改名
        created_at  建会话的时间
        updated_at  最后一次说话的时间 —— 侧栏按它倒序排

    chat_message  一条消息一行（user 和 assistant 都存在这里）
        id          自增，天然就是"第几条"
        session_id  外键，会话删了消息跟着删（ON DELETE CASCADE）
        role        user / assistant
        content     正文
        ts          时间
        elapsed     这一轮跑了多少秒        ← 第 9 步没有的
        rounds      跑了几轮                ← 第 9 步没有的
        tokens      估算 token 数           ← 第 9 步没有的
        status      ok / timeout / aborted / error
        tools       JSON 数组，本轮调了哪些工具、参数是什么、结果摘要

后四个字段就是"从 demo 到产品"的实质差别：
**它让每一次回答都变成可复盘的事件，而不只是一段话。**

──────────────────────────────────────────────────────────────────────
三个必须知道的 SQLite 脾气（都是实打实踩出来的）：

  ① 外键约束默认是**关的**。
     `PRAGMA foreign_keys = ON` 是"每连接"设置，不是建库时设一次就永久生效。
     不开的话 ON DELETE CASCADE 完全不工作 —— 删了会话，消息全变成孤儿。

  ② 并发写会直接报 "database is locked"。
     开 WAL（读写不互斥）+ `timeout=5`（等锁而不是立刻放弃）。
     SSE 是长连接，一个问句要跑 8~20 秒 —— 这期间绝不能抱着事务不放，
     所以下面的写法全是「开连接 → 写一条 → 立刻关」。

  ③ 连接不能跨线程复用。
     Flask 开了 threaded=True，每个请求一个线程。
     每次操作新建连接最省心（SQLite 开连接是微秒级，比想象中便宜），
     不用 threalocal，也不会出现"连接正被另一个线程使用"。
"""

# ── 编码兜底（所有脚本统一，别删）───────────────────────────────
# 中文 Windows 的 PowerShell / cmd 默认是 GBK，而 ✅ ❌ ✗ ⚠ 在 GBK 里
# 根本不存在 —— 一旦输出被重定向或接管道，Python 会 UnicodeEncodeError
# 直接崩。更阴的是控制台直连时它走 WriteConsoleW，什么都不报，测不出来。
#
# 所以要分两条路：
#   · 控制台（isatty）→ 用 utf-8，WriteConsoleW 能正确显示 ✅
#   · 管道 / 重定向   → 沿用系统编码（GBK），只加 errors="replace"
#                       编不出来的符号变 ?，至少不崩、不乱码
import sys as _sys
try:
    if _sys.stdout.isatty():
        _sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        _sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    else:
        _sys.stdout.reconfigure(errors="replace")
        _sys.stderr.reconfigure(errors="replace")
except Exception:
    pass
# ────────────────────────────────────────────────────────────────

import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "_data")
DB_PATH = os.path.join(DATA_DIR, "app.db")

TITLE_LEN = 24          # 标题取前多少字

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_session (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '新对话',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_message (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES chat_session(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL DEFAULT '',
    ts          TEXT NOT NULL,
    elapsed     REAL,
    rounds      INTEGER,
    tokens      INTEGER,
    status      TEXT DEFAULT 'ok',
    tools       TEXT
);

-- 侧栏要按"最后一次说话时间"倒序取会话，这条索引就是为它建的
CREATE INDEX IF NOT EXISTS idx_session_updated ON chat_session(updated_at DESC);

-- 打开某个会话时按 id 顺序取它的全部消息
CREATE INDEX IF NOT EXISTS idx_msg_session ON chat_message(session_id, id);
"""


# ══════════════════════════════════════════════════════════════════════
# 一、连接与初始化
# ══════════════════════════════════════════════════════════════════════

def now():
    """统一的时间口径。存本地时间字符串，人看和排序都方便。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def connect():
    """开一个连接。用完必须关，所以下面全用 with。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row          # 让查询结果能按列名取值
    conn.execute("PRAGMA foreign_keys = ON")  # ← 坑 ①，每条连接都要设
    return conn


def init_db(verbose=True):
    """建表。IF NOT EXISTS，所以重复调用无害。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    with connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")   # ← 坑 ②，写进文件后长期有效
        conn.executescript(SCHEMA)
    if verbose:
        n_s = count_sessions()
        n_m = count_messages()
        print(f"数据库就绪：{DB_PATH}")
        print(f"  已有会话 {n_s} 个 / 消息 {n_m} 条", flush=True)


def new_id():
    return uuid.uuid4().hex


def valid_id(s):
    """会话 id 只可能是 32 位小写 hex。

    这个校验看着多余 —— 下面全是参数化查询，注入不进来。
    但它挡住的是另一类东西：手敲 URL 打错一个字符、
    从别处复制来一个不存在的 id。先挡在门口，后面就不用到处判空。
    """
    if not isinstance(s, str) or len(s) != 32:
        return False
    return all(c in "0123456789abcdef" for c in s)


# ══════════════════════════════════════════════════════════════════════
# 二、会话
# ══════════════════════════════════════════════════════════════════════

def create_session(title="新对话"):
    sid = new_id()
    t = now()
    with connect() as conn:
        conn.execute(
            "INSERT INTO chat_session(id, title, created_at, updated_at) VALUES (?,?,?,?)",
            (sid, title or "新对话", t, t),
        )
    return {"id": sid, "title": title or "新对话", "created_at": t, "updated_at": t,
            "msg_count": 0, "tool_count": 0}


def get_session(sid):
    if not valid_id(sid):
        return None
    with connect() as conn:
        row = conn.execute(
            """SELECT s.*,
                      (SELECT COUNT(*) FROM chat_message m
                        WHERE m.session_id = s.id AND m.role='user')   AS msg_count,
                      (SELECT COUNT(*) FROM chat_message m
                        WHERE m.session_id = s.id AND m.tools IS NOT NULL
                          AND m.tools != '[]')                          AS tool_count
                 FROM chat_session s WHERE s.id = ?""",
            (sid,),
        ).fetchone()
    return dict(row) if row else None


def list_sessions(limit=100, include_empty=False):
    """侧栏和记录页共用。倒序：最近说过的排最前。

    include_empty=False 时过滤掉一条消息都没有的会话 ——
    不然"新对话"按钮点五次，侧栏就多五个空条目，看着像坏了。
    记录页要看到全部（包括空的），所以那边传 True。
    """
    sub = """(SELECT COUNT(*) FROM chat_message m WHERE m.session_id = s.id)"""
    where = "" if include_empty else f"WHERE {sub} > 0"
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT s.id, s.title, s.created_at, s.updated_at,
                      (SELECT COUNT(*) FROM chat_message m
                        WHERE m.session_id = s.id AND m.role='user')   AS msg_count,
                      (SELECT COUNT(*) FROM chat_message m
                        WHERE m.session_id = s.id AND m.tools IS NOT NULL
                          AND m.tools != '[]')                          AS tool_count,
                      (SELECT COALESCE(SUM(m.elapsed), 0) FROM chat_message m
                        WHERE m.session_id = s.id)                      AS total_elapsed
                 FROM chat_session s
                 {where}
                ORDER BY s.updated_at DESC
                LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def rename_session(sid, title):
    title = (title or "").strip()[:60] or "新对话"
    if not valid_id(sid):
        return False
    with connect() as conn:
        cur = conn.execute("UPDATE chat_session SET title = ? WHERE id = ?", (title, sid))
        return cur.rowcount > 0


def delete_session(sid):
    if not valid_id(sid):
        return False
    with connect() as conn:
        cur = conn.execute("DELETE FROM chat_session WHERE id = ?", (sid,))
        return cur.rowcount > 0          # 靠 CASCADE 带走消息


def touch_session(sid, first_question=None):
    """说话之后更新 updated_at；如果标题还是默认值，用第一句话命名。"""
    t = now()
    with connect() as conn:
        row = conn.execute("SELECT title FROM chat_session WHERE id = ?", (sid,)).fetchone()
        if not row:
            return
        title = row["title"]
        if first_question and title in ("", "新对话"):
            title = first_question.strip().replace("\n", " ")[:TITLE_LEN] or "新对话"
        conn.execute("UPDATE chat_session SET title = ?, updated_at = ? WHERE id = ?",
                     (title, t, sid))


# ══════════════════════════════════════════════════════════════════════
# 三、消息
# ══════════════════════════════════════════════════════════════════════

def add_message(sid, role, content, *, elapsed=None, rounds=None,
                tokens=None, status="ok", tools=None):
    """写一条消息。

    tools 传 Python 列表进来，这里序列化成 JSON 存 —— 数据库不懂 list。
    注意 ensure_ascii=False：不然中文全变成 \\u54c1 这种，自己都看不懂。
    """
    if not valid_id(sid):
        return None
    payload = None
    if tools:
        payload = json.dumps(tools, ensure_ascii=False, default=str)
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO chat_message
                 (session_id, role, content, ts, elapsed, rounds, tokens, status, tools)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (sid, role, content, now(), elapsed, rounds, tokens, status, payload),
        )
        return cur.lastrowid


def get_messages(sid, limit=None):
    """取一个会话的全部消息，转成前端能直接用的形状。"""
    if not valid_id(sid):
        return []
    sql = """SELECT id, role, content, ts, elapsed, rounds, tokens, status, tools
               FROM chat_message WHERE session_id = ? ORDER BY id"""
    params = [sid]
    if limit:
        # 要末尾 N 条，得先倒序取再翻回来
        sql = sql.replace("ORDER BY id", "ORDER BY id DESC LIMIT ?")
        params.append(limit)
    with connect() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    out = [dict(r) for r in reversed(rows)] if limit else [dict(r) for r in rows]
    for m in out:
        try:
            m["tools"] = json.loads(m["tools"]) if m["tools"] else []
        except (json.JSONDecodeError, TypeError):
            m["tools"] = []
    return out


def history_for_model(sid, keep=6):
    """喂给模型的上下文。

    ★ 这里比第 9 步多了两件事，两件都是踩出来的：

    ① 成对取，且只取"答成了"的对话对。
       第 9 步的做法是整段切最后 N 条。但库里现在存着超时、中断、
       报错的消息 —— 把一条 "⚠ 超时" 的 assistant 喂进去，
       下一轮模型会以为"我上次就是这么答的"，接着半截往下编。
       所以按 (user, assistant) 配对，assistant.status 不是 ok 的整对丢掉。

    ② 保证第一条是 user。
       从中间截断很容易截出一条孤零零的 assistant，接口直接 400。
    """
    msgs = get_messages(sid)

    pairs = []
    i = 0
    while i < len(msgs):
        u = msgs[i]
        if u["role"] != "user":
            i += 1
            continue
        a = msgs[i + 1] if i + 1 < len(msgs) and msgs[i + 1]["role"] == "assistant" else None
        if a and (a["status"] or "ok") == "ok" and a["content"]:
            pairs.append((u, a))
        i += 2 if a else 1

    if keep:
        pairs = pairs[-max(1, keep // 2):]      # keep 条 = keep//2 组

    out = []
    for u, a in pairs:
        out.append({"role": "user", "content": u["content"]})
        out.append({"role": "assistant", "content": a["content"]})
    return out


# ══════════════════════════════════════════════════════════════════════
# 四、统计（管理页用）
# ══════════════════════════════════════════════════════════════════════

def count_sessions():
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) c FROM chat_session").fetchone()["c"]


def count_messages():
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) c FROM chat_message").fetchone()["c"]


def stats():
    """管理页顶部那几个数字。

    工具排行没法用一句 SQL 干净地算（tools 是 JSON 字符串），
    直接拉出来在 Python 里数 —— 几百条消息的量级，这样最清楚。
    """
    with connect() as conn:
        row = conn.execute(
            """SELECT
                 COUNT(*)                                              AS answers,
                 ROUND(AVG(elapsed), 2)                                AS avg_elapsed,
                 ROUND(AVG(rounds), 2)                                 AS avg_rounds,
                 SUM(CASE WHEN status != 'ok' THEN 1 ELSE 0 END)       AS abnormal,
                 SUM(tokens)                                           AS tokens
               FROM chat_message WHERE role = 'assistant'"""
        ).fetchone()
        tool_rows = conn.execute(
            "SELECT tools FROM chat_message WHERE tools IS NOT NULL AND tools != '[]'"
        ).fetchall()

    tool_rank = {}
    for r in tool_rows:
        try:
            for t in json.loads(r["tools"]):
                name = t.get("name") or "?"
                tool_rank[name] = tool_rank.get(name, 0) + 1
        except (json.JSONDecodeError, TypeError):
            continue

    answers = row["answers"] or 0
    d = dict(row)
    d["sessions"] = count_sessions()
    d["messages"] = count_messages()
    d["answers"] = answers
    d["avg_elapsed"] = row["avg_elapsed"] or 0
    d["avg_rounds"] = row["avg_rounds"] or 0
    d["abnormal"] = row["abnormal"] or 0
    d["ok_rate"] = round((answers - (row["abnormal"] or 0)) / answers * 100, 1) if answers else 0
    d["tokens"] = row["tokens"] or 0
    d["tool_total"] = sum(tool_rank.values())
    d["tool_rank"] = sorted(tool_rank.items(), key=lambda kv: -kv[1])
    return d


def daily_counts(days=7):
    """最近 N 天每天问了几句。侧栏那根小趋势线用得上。"""
    out = []
    today = datetime.now().date()
    with connect() as conn:
        for i in range(days - 1, -1, -1):
            d = today - timedelta(days=i)
            n = conn.execute(
                "SELECT COUNT(*) c FROM chat_message WHERE role='user' AND ts LIKE ?",
                (d.strftime("%Y-%m-%d") + "%",),
            ).fetchone()["c"]
            out.append({"date": d.strftime("%m-%d"), "n": n})
    return out


# ══════════════════════════════════════════════════════════════════════
# 五、导出
# ══════════════════════════════════════════════════════════════════════

def export_markdown(sid):
    """把一次对话导出成 Markdown。

    为什么要有这个：面试时你说"它调过工具、8.3 秒答完"，
    不如直接把这份文件发过去。而且它顺手就是一份可读的记录。
    """
    s = get_session(sid)
    if not s:
        return None
    msgs = get_messages(sid)
    L = [f"# {s['title']}", "",
         f"- 会话 ID：`{sid}`",
         f"- 开始时间：{s['created_at']}",
         f"- 消息条数：{len(msgs)}", ""]

    for m in msgs:
        if m["role"] == "user":
            L.append(f"## 🙋 {m['content']}")
            L.append(f"<sub>{m['ts']}</sub>\n")
        else:
            L.append(m["content"])
            if m["tools"]:
                L.append("\n**工具调用**\n")
                for t in m["tools"]:
                    L.append(f"- `{t.get('name')}({t.get('args')})` → {t.get('brief')}")
            meta = []
            if m["elapsed"]:
                meta.append(f"{m['elapsed']} 秒")
            if m["rounds"]:
                meta.append(f"{m['rounds']} 轮")
            if m["tokens"]:
                meta.append(f"约 {m['tokens']} tokens")
            if m["status"] and m["status"] != "ok":
                meta.append(f"状态 {m['status']}")
            if meta:
                L.append(f"\n<sub>{' · '.join(meta)}</sub>")
            L.append("")
    return "\n".join(L)


# ══════════════════════════════════════════════════════════════════════
# 六、自测：直接跑这个文件能验证所有函数
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 62)
    print("  store.py 自测")
    print("=" * 62)

    init_db()

    s = create_session()
    sid = s["id"]
    print(f"\n建会话：{sid}")

    add_message(sid, "user", "近 30 天哪个科室收入最高？")
    add_message(sid, "assistant", "内科 1159.40 元。",
                elapsed=5.4, rounds=2, tokens=312,
                tools=[{"name": "dept_revenue_rank", "args": '{"days":30}',
                        "brief": "12 行｜第一名：内科 1159.4"}])
    touch_session(sid, "近 30 天哪个科室收入最高？")

    got = get_session(sid)
    print(f"标题自动命名：{got['title']}")
    print(f"消息数：{got['msg_count']}  含工具调用的回答：{got['tool_count']}")

    print(f"\n喂给模型的上下文（{len(history_for_model(sid))} 条）：")
    for m in history_for_model(sid):
        print(f"  {m['role']}: {m['content'][:30]}")

    print("\n统计：")
    st = stats()
    for k in ("sessions", "messages", "answers", "avg_elapsed",
              "avg_rounds", "ok_rate", "tool_total"):
        print(f"  {k} = {st[k]}")
    print(f"  tool_rank = {st['tool_rank']}")

    print("\n非法 id 挡在门口：", valid_id("../../etc/passwd"), valid_id(sid))

    md = export_markdown(sid)
    print(f"\n导出 Markdown {len(md)} 字符，前 3 行：")
    for line in md.split("\n")[:3]:
        print("  " + line)

    # 清掉这次自测造的数据，别污染真实记录
    delete_session(sid)
    print(f"\n删除会话后剩余：{count_sessions()} 个")
    print("=" * 62)
