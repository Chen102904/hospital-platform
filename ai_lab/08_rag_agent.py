# -*- coding: utf-8 -*-
"""
第 8 步：把 RAG 接进 Agent —— 一个既会查数据库、也会查文档的助手

跑法（在 ai_lab 目录下）：
    C:/Users/dell/.workbuddy/binaries/python/envs/default/Scripts/python.exe 08_rag_agent.py

前置：
    mykey.py    —— 第 1 步填好的
    db_ro.py    —— 第 6 步建好的只读账号
    MySQL 正在跑

这一步是把前面三样东西拼起来：
    第 5 步的 while 循环   -> Agent 骨架
    第 6 步的只读数据库    -> 真实数字
    第 7 步的向量检索      -> 真实文档

**为什么必须拼**：真实用户不会分「这是数据问题」还是「这是文档问题」。
他问的是「哪个科室收入最高？他们是怎么收费的？」——
前一句要查库，后一句要查文档。一个 Agent 得自己判断、自己排顺序。

四个实验：
    A  只给数据库工具，问文档问题   -> 它答不了（也不会瞎编，因为没工具就会说没能力）
    B  加上 search_docs            -> 答对，还能说出出处
    C  一句话要两边都查             -> 它自己排顺序，连跑两轮工具
    D  文档里没有的问题             -> 检索空结果，它老实说查不到

注：为了让这个文件能单独跑，我把切块/检索/查库的关键函数各复制了一份精简版。
    真实项目里这些应该抽成一个模块被 import —— 教学脚本分开写是为了每一步都能独立运行。
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
import re
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np
import pymysql
import requests

# ★ 原来这行是：
#       sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", ...)
#   它有个隐患：每 import 一次这个模块就**新建**一个 wrapper 包住当前的
#   sys.stdout.buffer。包第二次时，第一个 wrapper 变成垃圾被回收，而它析构时
#   会关掉底层 buffer —— 于是之后再 print 全部报 "I/O operation on closed file"。
#   单独跑脚本只 import 一次，看不出来；被 09 / 10 / 测试反复加载就会炸。
#   reconfigure 是就地改编码，不新建对象，调多少次都无害。
#   （实际那次调用已经提到文件顶部「编码兜底」里了，这里只留这条经验。）

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
CACHE = HERE / "_cache"

try:
    from mykey import API_KEY, API_URL, MODEL
except ImportError:
    sys.exit("✗ 没找到 mykey.py。先把 mykey.py.example 复制成 mykey.py，再填值。")

try:
    from mykey import PROXY
except ImportError:
    PROXY = ""

# embedding 模型名各平台不一样，所以放进 mykey.py：
#   通义   text-embedding-v3
#   硅基流动 BAAI/bge-m3      （免费版就用这个名字，收费版才加 Pro/ 前缀）
#   智谱   embedding-3
#   模力方舟 bge-m3           （这家不带 BAAI/ 前缀，别画蛇添足）
# 没配就沿用原来的默认值 —— 老配置一行不改照常跑，不会因为这次改动而行为变化。
try:
    from mykey import EMB_MODEL
except ImportError:
    EMB_MODEL = "text-embedding-v3"

try:
    from db_ro import DB_RO
except ImportError:
    sys.exit("✗ 没找到 db_ro.py。先把 db_ro.py.example 复制成 db_ro.py 并填好只读账号。")


def build_proxies():
    p = (PROXY or "").strip()
    if not p:
        return None
    if p.lower() == "none":
        return {"http": None, "https": None}
    return {"http": p, "https": p}


API_URL, API_KEY, MODEL = (v.strip() for v in (API_URL, API_KEY, MODEL))
BASE = API_URL.rsplit("/chat/completions", 1)[0]
EMB_MODEL = EMB_MODEL.strip()          # 向量化模型名（从 mykey.py 来，见上）
HEADERS = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
PROXIES = build_proxies()

SEP = "═" * 70
SUB = "─" * 70

KB_FILES = [
    ROOT / "hospital_web" / "README.md",
    ROOT / "hospital_db_project" / "数据库设计文档.md",
    ROOT / "hospital_db_project" / "README.md",
]


# ══════════════════════════════════════════════════════════════════════
# 一、知识库：切块 + 向量（第 7 步的精简版，只保留向量检索）
# ══════════════════════════════════════════════════════════════════════

def cut_blocks(text, max_chars=900):
    """按 Markdown 标题切块；一节太长再按空行切段落组。"""
    lines = text.splitlines()
    blocks, title, buf, in_code = [], "（文首）", [], False

    def flush():
        body = "\n".join(buf).strip()
        if not body:
            return
        if len(body) <= max_chars:
            blocks.append((title, body))
            return
        paras = re.split(r"\n\s*\n", body)
        cur = ""
        for p in paras:
            # 至少攒够 120 字符才切，否则会把标题行单独切成空壳块
            if cur and len(cur) + len(p) > max_chars and len(cur.strip()) >= 120:
                blocks.append((title, cur.strip()))
                cur = ""
            cur += p + "\n\n"
        if cur.strip():
            blocks.append((title, cur.strip()))

    for line in lines:
        if line.strip().startswith("```"):
            in_code = not in_code
        if (not in_code) and re.match(r"^#{1,4}\s+\S", line):
            flush()
            title, buf = line.lstrip("#").strip(), [line]
        else:
            buf.append(line)
    flush()
    return blocks


def build_chunks():
    chunks = []
    for path in KB_FILES:
        if not path.exists():
            continue
        name = path.relative_to(ROOT).as_posix()
        for i, (title, body) in enumerate(cut_blocks(path.read_text(encoding="utf-8"))):
            if len(body.strip()) < 80:
                continue
            chunks.append({
                "id": f"{name}#{i:03d}",
                "doc": name,
                "title": title,
                "text": f"【{name} > {title}】\n{body}",
            })
    return chunks


def embed(texts, batch=10, quiet=False):
    vecs = []
    for i in range(0, len(texts), batch):
        r = requests.post(
            BASE + "/embeddings", headers=HEADERS,
            json={"model": EMB_MODEL, "input": texts[i:i + batch]},
            timeout=180, proxies=PROXIES,
        )
        if r.status_code != 200:
            raise RuntimeError(f"embedding 失败 HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()["data"]
        data.sort(key=lambda x: x.get("index", 0))
        vecs.extend(d["embedding"] for d in data)
        if not quiet:
            done = min(i + batch, len(texts))
            print(f"    向量化 {done}/{len(texts)}", end="\r" if sys.stdout.isatty() else "\n")
    if not quiet and sys.stdout.isatty():
        print()
    return np.array(vecs, dtype=np.float32)


class DocIndex:
    """文档向量索引。缓存文件名和第 7 步一致，所以跑过 07 就不用再花钱。"""

    def __init__(self, chunks):
        self.chunks = chunks
        self._qcache = {}
        CACHE.mkdir(exist_ok=True)
        # 模型名不能直接拼进文件名：硅基流动的 embedding 叫 `BAAI/bge-m3`，
        # 那个斜杠会被 pathlib 当成分隔符，拼出来变成 _cache/emb_BAAI/bge-m3_63.json，
        # 而 _cache/emb_BAAI 这个目录根本不存在 —— 报 FileNotFoundError，
        # 而且报错点在建缓存之后，看着像"向量化失败"。
        # 所以把非法字符一律换成 '-'，换个平台也不会撞名。
        safe = re.sub(r'[^0-9A-Za-z._-]+', '-', EMB_MODEL).strip('-') or "emb"
        fp = CACHE / f"emb_{safe}_{len(chunks)}.json"
        ids = [c["id"] for c in chunks]

        if fp.exists():
            d = json.loads(fp.read_text(encoding="utf-8"))
            if d.get("ids") == ids:
                self.mat = np.array(d["vectors"], dtype=np.float32)
                print(f"  （向量复用缓存 {fp.name}，没花钱）")
                return

        print(f"  首次运行，向量化 {len(chunks)} 块：")
        self.mat = embed([c["text"] for c in chunks])
        fp.write_text(json.dumps({"model": EMB_MODEL, "ids": ids,
                                  "vectors": self.mat.tolist()}, ensure_ascii=False),
                      encoding="utf-8")

    def search(self, query, top_k=3):
        if query not in self._qcache:
            self._qcache[query] = embed([query], quiet=True)[0]
        qv = self._qcache[query]
        m = self.mat / np.linalg.norm(self.mat, axis=1, keepdims=True)
        sims = m @ (qv / np.linalg.norm(qv))
        order = np.argsort(-sims)[:top_k]
        return [(float(sims[i]), int(i)) for i in order]


# ══════════════════════════════════════════════════════════════════════
# 二、数据库：只读账号 + SQL 写死（第 6 步的规矩，一条不少）
# ══════════════════════════════════════════════════════════════════════
MAX_ROWS = 30


def run_select(sql, params=()):
    try:
        conn = pymysql.connect(**DB_RO, cursorclass=pymysql.cursors.DictCursor)
    except pymysql.MySQLError as e:
        return {"ok": False, "reason": f"数据库连不上：{e}"}
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchmany(MAX_ROWS)
        return {"ok": True, "rows": rows, "row_count": len(rows)}
    except pymysql.MySQLError as e:
        return {"ok": False, "reason": f"SQL 出错：{e}"}
    finally:
        conn.close()


def dept_revenue_rank(days=30):
    """近 N 天各科室收入排名。"""
    try:
        days = int(days)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "days 必须是整数"}
    if not 1 <= days <= 365:
        return {"ok": False, "reason": "days 必须在 1~365 之间"}

    res = run_select(
        """SELECT d.dept_name                    AS 科室,
                  COUNT(DISTINCT c.charge_id)   AS 收费单数,
                  ROUND(SUM(cd.amount), 2)      AS 收入
           FROM charge_detail cd
           JOIN charge        c ON c.charge_id = cd.charge_id
           JOIN registration  r ON r.reg_id    = c.reg_id
           JOIN department    d ON d.dept_id   = r.dept_id
           WHERE c.charge_status = '已收费'
             AND c.charge_time >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
           GROUP BY d.dept_name
           ORDER BY 收入 DESC""", (days,))

    if res.get("ok") and res.get("row_count") == 0:
        mx = run_select("SELECT DATE_FORMAT(MAX(charge_time), '%%Y-%%m-%%d') AS d FROM charge")
        newest = (mx.get("rows") or [{}])[0].get("d", "未知")
        res["note"] = (f"近 {days} 天没有任何收费记录（库里最新收费日期是 {newest}）。"
                       f"请如实说没数据，不要推测金额。")
    return res


def low_stock_medicines(threshold=20):
    """库存低于阈值的在用药品（告警清单，答案是"哪些药在这个区间里"）。

    注意它回答不了"库存最少的是哪个"——那是排序语义，交给 medicine_stock。
    """
    try:
        threshold = int(threshold)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "threshold 必须是整数"}
    threshold = max(0, min(threshold, 100000))

    res = run_select(
        """SELECT medicine_name AS 药品, category AS 分类, stock AS 库存, unit AS 单位
           FROM medicine
           WHERE status = '在用' AND stock < %s
           ORDER BY stock ASC""", (threshold,))

    # 0 行必须补 note。否则模型看到空数组容易两种错误：
    # 要么编数字，要么把"没有告警"当成"没有库存数据"，答非所问。
    # note 里顺带指明正确的工具——这是工具层面的自我纠正，不是靠 prompt 求模型聪明。
    if res.get("ok") and res.get("row_count") == 0:
        res["note"] = (f"没有库存低于 {threshold} 的在用药品。"
                       f"这只说明该阈值下没有告警，不代表能回答「库存最少/最多的是哪个」"
                       f"或「某个药还剩多少」——那种问题请改用 medicine_stock 按库存排序查。")
    return res


def medicine_stock(name=None, order="asc", limit=10):
    """按库存查药品：既能点查某个药，也能按库存排序取前 N。

    和 low_stock_medicines 的分工（两者别混）：
      · low_stock_medicines：给定一条阈值线，问"哪些药低于这条线"→ 答案是集合
      · medicine_stock    ：问"最少/最多的是哪个""某某药还剩多少"→ 答案是具体值

    参数
      name  : 药品名关键字，模糊匹配。不传就查全部在用药品
      order : asc 从少到多（默认）/ desc 从多到少
      limit : 取前几条，1~30
    """
    order = (order or "asc").strip().lower()
    if order in ("ascending", "少到多", "升序"):
        order = "asc"
    if order in ("descending", "多到少", "降序"):
        order = "desc"
    if order not in ("asc", "desc"):
        return {"ok": False, "reason": "order 只能是 asc（从少到多）或 desc（从多到少）"}

    try:
        limit = int(limit if limit is not None else 10)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "limit 必须是整数"}
    # 上限压到 MAX_ROWS：run_select 用 fetchmany(MAX_ROWS) 取数，
    # 允许传 50 却只拿到 30 条，等于悄悄骗了调用方。
    limit = max(1, min(limit, MAX_ROWS))

    where = ["status = '在用'"]
    params = []
    if name is not None and str(name).strip():
        kw = str(name).strip()
        if len(kw) > 50:
            return {"ok": False, "reason": "name 太长（超过 50 字），请只给药品名关键字"}
        # 转义 LIKE 通配符，否则用户传个 "%" 就把全表捞出来了。
        # ESCAPE '!' 比反斜杠稳：反斜杠在 MySQL 字符串里还要再转义一层。
        kw = kw.replace("!", "!!").replace("%", "!%").replace("_", "!_")
        where.append("medicine_name LIKE %s ESCAPE '!'")
        params.append(f"%{kw}%")

    sql = ("SELECT medicine_name AS 药品, category AS 分类, stock AS 库存, "
           "unit AS 单位, status AS 状态 FROM medicine WHERE "
           + " AND ".join(where)
           + f" ORDER BY stock {order.upper()}, medicine_name LIMIT %s")
    res = run_select(sql, tuple(params) + (limit,))

    if res.get("ok"):
        res["order"] = order
        if res.get("row_count") == 0 and name:
            res["note"] = (f"库里没有名字含「{name}」的在用药品。"
                           f"请如实说没查到，不要凭印象给库存数字。")
        elif res.get("row_count") == 0:
            res["note"] = "库里没有在用药品。"
    return res


# ══════════════════════════════════════════════════════════════════════
# 三、工具层：四个工具，三个查库、一个查文档
# ══════════════════════════════════════════════════════════════════════
IDX = None                 # DocIndex，main 里初始化
MIN_SCORE = 0.45           # 相似度低于这个值就当作"没检索到"


def search_docs(query, top_k=3):
    """在项目文档里检索。这是整个 RAG 环节对模型暴露的唯一入口。"""
    top_k = max(1, min(int(top_k or 3), 5))
    hits = IDX.search(query, top_k=top_k)

    top = hits[0][0] if hits else 0
    if not hits or top < MIN_SCORE:
        return {"ok": True, "hits": [],
                "note": (f"知识库里没有找到相关内容（最高相似度只有 {top:.2f}，"
                         f"低于 {MIN_SCORE} 的阈值）。请如实告诉用户文档里没有，不要凭印象作答。")}

    out = []
    for rank, (score, i) in enumerate(hits, 1):
        c = IDX.chunks[i]
        out.append({
            "rank": rank,
            "score": round(score, 3),
            "source": c["doc"],
            "section": c["title"],
            "text": c["text"][:1200],      # 单块截断，防止 token 爆炸
        })
    return {"ok": True, "hits": out}


TOOLS = [
    {"type": "function", "function": {
        "name": "dept_revenue_rank",
        "description": ("查数据库：近 N 天各科室收入排名（按已收费单统计）。"
                        "返回每一行同时带「收入」和「收费单数」两列，"
                        "所以问「哪个科室赚得多」和问「某科室开了多少张/多少笔收费单」都用它。"),
        "parameters": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "统计最近多少天，1~365，默认 30"},
            },
            "additionalProperties": False,
        },
    }},
    {"type": "function", "function": {
        "name": "low_stock_medicines",
        "description": ("查数据库：库存低于某个阈值的在用药品清单（告警/补货场景）。"
                        "只回答「哪些药低于这条线」，回答不了「哪个药库存最少」"
                        "或「某个药还剩多少」——后者用 medicine_stock。"),
        "parameters": {
            "type": "object",
            "properties": {
                "threshold": {"type": "integer", "description": "库存低于多少算告警，默认 20"},
            },
            "additionalProperties": False,
        },
    }},
    {"type": "function", "function": {
        "name": "medicine_stock",
        "description": ("查数据库：按库存查药品，两种用法。"
                        "①问「库存最少/最多的是哪个药」「库存排名」→ 不传 name，用 order 排序取前几条；"
                        "②问「某某药还剩多少库存」→ 传 name 按药品名查。"
                        "这个工具不需要猜阈值，任何库存量都能查到。"),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "药品名关键字，如「布洛芬」。查具体某个药时必填；问排名时不填"},
                "order": {"type": "string", "enum": ["asc", "desc"],
                          "description": "库存排序：asc 从少到多（问「最少」用这个，默认）/ desc 从多到少"},
                "limit": {"type": "integer", "description": "返回前几条，1~30，默认 10。只问「最少那一个」传 1 就够"},
            },
            "additionalProperties": False,
        },
    }},
    {"type": "function", "function": {
        "name": "search_docs",
        "description": ("查文档：在本项目的设计文档、README 里检索。凡是问「为什么这样设计」"
                        "「怎么实现的」「业务流程是什么」「某个功能怎么用」这类文档问题，都必须调用它，"
                        "不允许凭你自己的知识回答。返回结果带 source 和 section，回答时要标出处。"),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索词。用文档里可能出现的说法，比原问题更简洁更专业"},
                "top_k": {"type": "integer", "description": "返回几块，1~5，默认 3"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    }},
]


def dispatch(name, arguments_json):
    try:
        args = json.loads(arguments_json or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "reason": f"参数不是合法 JSON：{arguments_json}"}

    table = {
        "dept_revenue_rank": dept_revenue_rank,
        "low_stock_medicines": low_stock_medicines,
        "medicine_stock": medicine_stock,
        "search_docs": search_docs,
    }
    fn = table.get(name)
    if not fn:
        return {"ok": False, "reason": f"没有这个工具：{name}"}

    # 模型给什么参数由它决定，但函数签名是我们定的 —— 多余的键直接丢掉
    allowed = set(fn.__code__.co_varnames[:fn.__code__.co_argcount])
    kwargs = {k: v for k, v in args.items() if k in allowed}
    try:
        return fn(**kwargs)
    except Exception as e:      # 工具自己崩了，也要把错误交给模型，别让整个 Agent 挂掉
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"}


# ══════════════════════════════════════════════════════════════════════
# 四、Agent 循环（第 5 步的骨架，砍到最小可用）
# ══════════════════════════════════════════════════════════════════════
SYSTEM = (
    "你是一家医院信息系统的助手，手上有两类工具：\n"
    "  · 查数据库的：dept_revenue_rank（科室收入）、low_stock_medicines（低于阈值的告警清单）、"
    "medicine_stock（按库存点查或排序）—— 拿真实的数字\n"
    "  · 查文档的：search_docs —— 拿设计说明、业务流程\n"
    "规则：\n"
    "1. 问数字、金额、排名、库存，必须调数据库工具，不许估。\n"
    "2. 问「哪些药低于某条线、要不要补货」用 low_stock_medicines；"
    "问「库存最少/最多的是哪个」「某个药还剩多少」用 medicine_stock，别拿阈值去凑。\n"
    "3. 问设计、为什么、怎么实现、业务流程，必须调 search_docs，不许凭自己知识答。\n"
    "4. 一句话里既有数据又有设计，就两个都调（可以先调一个再调另一个）。\n"
    "5. 工具返回空或报错，如实说，不要编数字。引用文档时要标出原文所在章节。\n"
    "6. 用大白话回答，不要罗列大纲。"
)


def real_model(messages, tools=None, timeout=120):
    body = {"model": MODEL, "messages": messages}
    if tools:
        body["tools"] = tools
    r = requests.post(API_URL, headers=HEADERS, json=body, timeout=timeout, proxies=PROXIES)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    d = r.json()
    ch = d["choices"][0]
    return ch["message"], d.get("usage") or {}, ch.get("finish_reason")


def run_agent(question, max_rounds=6, max_seconds=150, repeat_limit=1, verbose=True):
    """一个带刹车的 Agent 循环。返回 dict，status 说明它是怎么结束的。"""
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": question},
    ]
    trace, seen = [], Counter()
    tokens = 0
    retried_empty = False
    t0 = time.time()

    for rnd in range(1, max_rounds + 1):
        if time.time() - t0 > max_seconds:
            return {"answer": "（超时）", "status": "timeout", "trace": trace,
                    "tokens": tokens, "elapsed": round(time.time() - t0, 1)}

        msg, usage, finish = real_model(messages, TOOLS)
        tokens += usage.get("total_tokens", 0)

        if not msg.get("tool_calls"):
            content = (msg.get("content") or "").strip()
            if content:
                return {"answer": content, "status": "ok", "trace": trace,
                        "tokens": tokens, "elapsed": round(time.time() - t0, 1), "rounds": rnd}

            # 模型回了个空壳：不调工具、一个字不写。API 层面这是合法的（finish_reason=stop），
            # 但用户视角就是"发出去没反应"。要是当成功返回，上层和评测都会被骗过去——
            # 实测 A02 就这么在 status=ok、2.4 秒、0 次工具调用下交了个空白，前两轮还全对。
            if not retried_empty:
                retried_empty = True
                if verbose:
                    print("     ⚠ 模型回了空内容，追问一次")
                messages.append({"role": "user",
                                 "content": "你上一条回复是空的。请直接回答上面的问题，别留空。"})
                continue
            return {"answer": "", "status": "empty", "trace": trace,
                    "tokens": tokens, "elapsed": round(time.time() - t0, 1), "rounds": rnd}

        if verbose:
            print(f"  ── 第 {rnd} 轮，模型要求：")
        messages.append(msg)          # 必须先把这一轮 assistant 消息放回去

        for tc in msg["tool_calls"]:
            name = tc["function"]["name"]
            raw = tc["function"]["arguments"]
            key = (name, raw)
            seen[key] += 1

            if seen[key] > repeat_limit + 1:
                result = {"ok": False,
                          "reason": "这个调用已经重复过了，结果不会变。请换思路或直接用现有信息回答。"}
                if verbose:
                    print(f"     ⛔ 拦截重复：{name}({raw[:60]})")
            else:
                result = dispatch(name, raw)
                if verbose:
                    print(f"     → {name}({raw[:70]})")
                    brief = json.dumps(result, ensure_ascii=False, default=str)
                    print(f"       {brief[:150]}")

            trace.append({"round": rnd, "tool": name, "args": raw,
                          "ok": result.get("ok")})
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": json.dumps(result, ensure_ascii=False, default=str),
            })

    return {"answer": "（达到轮次上限）", "status": "max_rounds", "trace": trace,
            "tokens": tokens, "elapsed": round(time.time() - t0, 1)}


# ══════════════════════════════════════════════════════════════════════
# 五、四个实验
# ══════════════════════════════════════════════════════════════════════

def show(question, r, note=""):
    print(f"\n  问：{question}")
    print(f"  答：{r['answer'].strip()[:420]}")
    print(f"     （{r['status']}｜工具 {len(r['trace'])} 次｜{r['elapsed']} 秒｜{r['tokens']} tokens）")
    if note:
        print(f"  {note}")


def exp_a():
    print("\n" + SEP)
    print("实验 A：只给数据库工具，问一个文档问题")
    print(SEP)
    print("""
  先看看没有 search_docs 的时候会怎样。
  这里临时把 TOOLS 里的文档工具摘掉，模拟「第 6 步那个只会查库的 Agent」。""")

    global TOOLS
    full = TOOLS
    TOOLS = [t for t in TOOLS if t["function"]["name"] != "search_docs"]
    try:
        r = run_agent("这个系统的登录密码是怎么存的？用的什么哈希？")
    finally:
        TOOLS = full

    show("这个系统的登录密码是怎么存的？用的什么哈希？", r)
    print("""
  ↑ 它的回答只有两种可能：一是我查不了文档，二是从通用知识里给你一份清单。
  这次它选了前者，还主动点出「不能瞎猜说用的是 bcrypt」——说明「不许估」那条提示还在管用。
  但两种都不是你要的答案：**手上没有这个工具，就永远给不出「你项目里到底是哪个」。**

  这不是它笨，是能力的边界画在工具清单上。""")
    print(SUB)
    print("  所以要加一个工具。但加什么很讲究：")
    print("  · 不能给「读任意文件」这种万能工具 —— 权限太大，等于把整个磁盘交给它")
    print("  · 只能给「按语义检索知识库」这一个 —— 它能拿到的永远只有那 63 块文档")
    print("  **工具的形状 = 权限的形状。**这和第 6 步「SQL 写死、模型只填参数」是同一个道理。")
    print(SUB)


def exp_b():
    print("\n" + SEP)
    print("实验 B：加上 search_docs，同一个问题再问一次")
    print(SEP)
    q = "这个系统的登录密码是怎么存的？用的什么哈希？"
    r = run_agent(q)
    show(q, r)
    print("""
  ↑ 看清楚它做了什么：先调 search_docs 检索，拿到原文片段，再基于片段作答。
  第 7 步实验 A 里它是给一份「bcrypt / Argon2 / PBKDF2」的通用清单，
  现在它给的是**你项目里的那一个答案 + 出处**。""")

    print("\n  再问一个「为什么」的问题，这是文档检索最典型的场景：")
    q2 = "为什么科室和医生要停用而不是直接删除？"
    r2 = run_agent(q2)
    show(q2, r2)


def exp_c():
    print("\n" + SEP)
    print("实验 C：一句话里既要数据又要设计 —— 多工具编排")
    print(SEP)
    print("""
  用户不会分「这是数据问题」还是「文档问题」。
  下面这句话前半句要查库、后半句要查文档，看它怎么排顺序。""")

    q = "近 30 天哪个科室收入最高？他们是怎么收费的、费用为什么存快照？"
    r = run_agent(q)
    show(q, r)

    print("\n" + SUB)
    print("  看工具调用顺序：")
    for t in r["trace"]:
        print(f"    [{t['round']}] {t['tool']}({t['args'][:60]})")
    print("""
  **这个顺序是模型自己排的，代码里没写死。**
  它得先判断「收入」要查库、「为什么存快照」要查文档，然后决定先查哪个。

  这就是第 5 步那个循环真正的用处：
  单个工具只是「会查库」或「会查文档」，**编排起来才是「会办事」。**""")
    print(SUB)


def exp_d():
    print("\n" + SEP)
    print("实验 D：文档里根本没有的问题")
    print(SEP)
    q = "机房那台 UPS 的电池一般多久更换一次？"
    print(f"\n  问：{q}")
    print("  （先看工具层返回了什么）")

    res = search_docs(q)
    if res["hits"]:
        top = res["hits"][0]["score"]
        print(f"    search_docs -> {len(res['hits'])} 条，最高相似度 {top}，阈值 {MIN_SCORE}")
        print(f"    -> {top} > {MIN_SCORE}，**阈值没拦住**，还是把这几块喂进去了：")
        print(f"       第 1 名「{res['hits'][0]['section'][:34]}」—— 讲的是基础数据维护，跟 UPS 无关")
    else:
        print(f"    search_docs -> 0 条（{res['note'][:60]}…）")

    r = run_agent(q)
    show(q, r)

    print(f"""
  ↑ 这一题的结论比"挡住了一个坏问题"更值得记住：

    第一层（阈值 {MIN_SCORE}）**没生效** —— 最高分 {res['hits'][0]['score'] if res['hits'] else 0} 过了线，
    不相关的片段照样被喂了进去。
    真正让它拒答的是**模型自己**：它读完那几块，判断"这讲的是科室/医生/药品维护，不是机房设备"。

  **能拒答是好事，但这次拦住它的是模型的判断力，不是你的代码。**
  换个弱一点的模型、或者知识库里恰好有一块「设备维护」的文档，它很可能就顺着硬答了。

  所以阈值不能随手写 {MIN_SCORE}。它得在你自己的数据上标定：
    拿一批"该答"和"不该答"的问题各跑一遍，看两边的相似度分布，取中间那个点。
  这套语料的正经问题 top-1 相似度在 0.57~0.74，噪音题在 0.47~0.50 —— 刀口其实很窄。

  **闸门要放在代码里，提示词只是第二道。** 这和第 6 步「只读账号 + 提示词」是一个套路，
  区别是这一次，闸门没关严。
""")
    print(SUB)


def exp_e():
    print("\n" + SEP)
    print("实验 E：复盘 —— 一次完整问答到底发生了什么")
    print(SEP)
    q = "近 30 天哪个科室收入最高？他们是怎么收费的、费用为什么存快照？"
    r = run_agent(q, verbose=True)
    show(q, r)

    print("\n  调用链：")
    for t in r["trace"]:
        mark = "✓" if t.get("ok") else "✗"
        print(f"    {mark} 第 {t['round']} 轮  {t['tool']}({t['args'][:64]})")
    print(f"\n  共 {len(r['trace'])} 次工具调用、{r['elapsed']} 秒、{r['tokens']} tokens")


def main():
    global IDX

    print(SEP)
    print("第 8 步：RAG + Agent —— 一个既能查库又能查文档的助手")
    print(SEP)
    print(f"模型：{MODEL}")
    print(f"数据库：只读账号 {DB_RO.get('user')}")

    chunks = build_chunks()
    IDX = DocIndex(chunks)
    print(f"知识库：{len(chunks)} 块")

    exp_a()
    exp_b()
    exp_c()
    exp_d()
    exp_e()

    print("\n" + SEP)
    print("七句话收尾 —— 到这里，从调通 API 到能用的小助手，全链路走完了")
    print(SEP)
    print("  1. 工具的形状就是权限的形状：只给它 search_docs，它就只可能碰到那几十块文档。")
    print("  2. 一句话里既有数据又有设计，靠的是模型自己排顺序，不是代码写死的流程。")
    print("  3. 空结果要显式告诉模型「这里没东西，别猜」—— 空集最容易被它自动补全。")
    print("  4. 检索阈值放代码里，提示词只是第二道。别指望一段话管住模型。")
    print("  5. 引用要带 source/section，否则你无法判断这个回答是查到的还是编的。")
    print("  6. 每一步都要能复盘：trace 记下第几轮、调了什么、参数是什么、成没成。")
    print("  7. 这套东西的边界很清楚：**它只读。** 能做决定的是人，不是它。")
    print()


if __name__ == "__main__":
    main()
