# -*- coding: utf-8 -*-
"""
第 7 步：RAG —— 让模型回答"你系统文档里到底写了什么"

跑法（在 ai_lab 目录下）：
    C:/Users/dell/.workbuddy/binaries/python/envs/default/Scripts/python.exe 07_rag_basics.py

前置：mykey.py 已填好（第 1 步就做完了）。本脚本会额外用到平台的 embedding 接口，
      也就是把一段文字变成一串数字（向量）的那个接口，地址是同一个 BaseURL 下的 /embeddings。

知识库直接用你自己的三份文档，不是玩具语料：
    hospital_web/README.md          30 KB  前端功能、权限、业务流程
    hospital_db_project/数据库设计文档.md  32 KB  15 张表 + 12 个"为什么这样设计"
    hospital_db_project/README.md   14 KB  表结构总览、ER 说明

七个实验，按顺序看，别跳：
    A  不检索，直接问实现细节        -> 模型给的是"世界上通常怎么做"，不是你的答案
    B  把三份文档全塞进 prompt       -> 答对了，但看真实 token 账单（两万多）
    C  切块（chunking）             -> 4 万字切成多少块、每块多大
    D  关键词检索（零依赖，纯 Python）-> 最土的办法，先建立直觉，也先看它怎么错
    E  向量检索（embedding + 余弦）  -> 把文字变成 1024 个数字比"意思"
    F  评测：标金标准、算命中率      -> **本脚本最值钱的一节**，见下面的真实数据
    G  完整 RAG：检索 -> 拼 prompt -> 带出处回答 -> 查不到就说查不到

实验 F 的实测结果（6 道题 / 63 块）。注意这里有一个"标错金标准"的坑：
    按「答案在哪一节」标：  关键词 2/6，向量 2/6     <- 结论"换算法没用"（错）
    按「答案关键词在哪块」标：关键词 4/6，向量 5/6   <- 真实差距
    同一个系统、同一套代码，只换金标准口径，结论完全相反。
    k 取 1/3/5/8 试过，k=3 就到顶，再大只是烧 token。
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
import io
import json
import math
import re
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np
import requests

# 这里原本有一行 sys.stdout = io.TextIOWrapper(...)，已删除。原因：每 import
# 一次就新建一层包装，包第二次时前一层被回收、析构时把底层 buffer 关掉，
# 之后所有 print 都报 I/O operation on closed file。上面那段 reconfigure 已够用。

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent            # 工作区根目录，三份文档都在这下面
CACHE = HERE / "_cache"

try:
    from mykey import API_KEY, API_URL, MODEL
except ImportError:
    sys.exit("✗ 没找到 mykey.py。先把 mykey.py.example 复制成 mykey.py，再填三个值。")

try:
    from mykey import PROXY
except ImportError:
    PROXY = ""


def build_proxies():
    p = (PROXY or "").strip()
    if not p:
        return None
    if p.lower() == "none":
        return {"http": None, "https": None}
    return {"http": p, "https": p}


API_URL, API_KEY, MODEL = (v.strip() for v in (API_URL, API_KEY, MODEL))
# 接口地址是 .../compatible-mode/v1/chat/completions，去掉尾巴就是 BaseURL
BASE = API_URL.rsplit("/chat/completions", 1)[0]
EMB_MODEL = "text-embedding-v3"      # 实测这家平台可用，1024 维
HEADERS = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
PROXIES = build_proxies()

KB_FILES = [
    ROOT / "hospital_web" / "README.md",
    ROOT / "hospital_db_project" / "数据库设计文档.md",
    ROOT / "hospital_db_project" / "README.md",
]

SEP = "═" * 70
SUB = "─" * 70


# ══════════════════════════════════════════════════════════════════════
# 基础设施：调模型、算钱、切块、检索
# ══════════════════════════════════════════════════════════════════════

USAGE = {"prompt": 0, "completion": 0, "emb": 0}


def chat(messages, temperature=0.3, timeout=180):
    """问一次模型，返回 (正文, usage)。"""
    body = {"model": MODEL, "messages": messages, "temperature": temperature}
    r = requests.post(API_URL, headers=HEADERS, json=body, timeout=timeout, proxies=PROXIES)
    if r.status_code != 200:
        return f"[HTTP {r.status_code}] {r.text[:300]}", {}
    d = r.json()
    u = d.get("usage") or {}
    USAGE["prompt"] += u.get("prompt_tokens", 0)
    USAGE["completion"] += u.get("completion_tokens", 0)
    return d["choices"][0]["message"].get("content") or "", u


def embed(texts, batch=10, quiet=False):
    """把一批文字变成向量。返回 (len(texts), 1024) 的 numpy 数组。"""
    vecs = []
    for i in range(0, len(texts), batch):
        part = texts[i:i + batch]
        r = requests.post(
            BASE + "/embeddings", headers=HEADERS,
            json={"model": EMB_MODEL, "input": part},
            timeout=180, proxies=PROXIES,
        )
        if r.status_code != 200:
            raise RuntimeError(f"embedding 失败 HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()["data"]
        data.sort(key=lambda x: x.get("index", 0))
        vecs.extend(d["embedding"] for d in data)
        if not quiet:
            done = min(i + len(part), len(texts))
            # 终端里原地刷新，重定向到文件时每批一行（否则日志里全是回车）
            print(f"    已向量化 {done}/{len(texts)} 段",
                  end="\r" if sys.stdout.isatty() else "\n")
    if not quiet and sys.stdout.isatty():
        print()
    return np.array(vecs, dtype=np.float32)


def load_corpus():
    """读三份文档，返回 [(Path, 全文), ...]。返回 Path 而不是字符串名，下面要用相对路径。"""
    out = []
    for p in KB_FILES:
        if not p.exists():
            print(f"  ⚠️ 找不到 {p}，跳过")
            continue
        out.append((p, p.read_text(encoding="utf-8")))
    return out


def cut_blocks(text, max_chars=900):
    """按 Markdown 标题切块。标题换了就是新块；某一节太长，再按空行切。"""
    lines = text.splitlines()
    blocks = []
    title, buf, in_code = "（文首）", [], False

    def flush():
        body = "\n".join(buf).strip()
        if not body:
            return
        if len(body) <= max_chars:
            blocks.append((title, body))
            return
        # 太长：按空行切成段落组，再把段落组攒到接近 max_chars
        paras = re.split(r"\n\s*\n", body)
        cur = ""
        for p in paras:
            # ≥120 字符才允许切出去。否则会把"标题行"单独切成一个空壳块
            # （真踩过：`## 功能` 只有 5 个字符就成了一块，检索命中它等于没命中）
            if cur and len(cur) + len(p) > max_chars and len(cur.strip()) >= 120:
                blocks.append((title, cur.strip()))
                cur = ""
            cur += p + "\n\n"
        if cur.strip():
            blocks.append((title, cur.strip()))

    for line in lines:
        if line.strip().startswith("```"):
            in_code = not in_code
        is_heading = (not in_code) and re.match(r"^#{1,4}\s+\S", line)
        if is_heading:
            flush()
            title = line.lstrip("#").strip()
            buf = [line]
        else:
            buf.append(line)
    flush()
    return blocks


def build_chunks():
    """把三份文档切成带出处的块。每块开头都带上「文档 > 章节」，检索时它也是线索。"""
    chunks = []
    skipped = 0
    for path, text in load_corpus():
        # 用相对路径作文档名，不用 .name。
        # 踩过真坑：hospital_web/README.md 和 hospital_db_project/README.md 都叫 README.md，
        # 直接用文件名会让两份文档的块 id 撞在一起（实测撞了 16 个），
        # 后果是检索结果分不清是哪一份、向量缓存也认错。
        name = path.relative_to(ROOT).as_posix()
        for i, (title, body) in enumerate(cut_blocks(text)):
            if len(body.strip()) < 80:
                skipped += 1      # 标题下面没内容的空壳块，命中它也没用
                continue
            chunks.append({
                "id": f"{name}#{i:03d}",
                "doc": name,
                "title": title,
                # 前缀很重要：让"这块来自哪个文档的哪一节"跟着块一起进向量、进 prompt
                "text": f"【{name} > {title}】\n{body}",
            })
    if skipped:
        print(f"（跳过了 {skipped} 个空壳块 —— 只有标题没有正文的）")
    return chunks


# ── 检索法 1：关键词（中文用 2-gram，零依赖） ──────────────────────────

def grams(text):
    """把文字切成检索用的碎片。

    中文没有空格，不能按空格切词。这里用最土但有效的办法：连续两个汉字算一个碎片。
    "药品库存" -> 药品 / 品库 / 库存
    不装 jieba 也能跑，而且对医疗术语（布洛芬、白蛋白）反而比词典分词更稳。
    """
    text = text.lower()
    out = re.findall(r"[a-z0-9_]+", text)
    for seg in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(seg) == 1:
            out.append(seg)
        else:
            out += [seg[i:i + 2] for i in range(len(seg) - 1)]
    return out


class KeywordIndex:
    def __init__(self, chunks):
        self.chunks = chunks
        self.sets = [set(grams(c["text"])) for c in chunks]
        df = Counter()
        for s in self.sets:
            df.update(s)
        n = len(chunks)
        # IDF：越少见的碎片越值钱。"的""是"这种到处都有的碎片权重接近 0
        self.idf = {t: math.log((n + 1) / (d + 0.5)) for t, d in df.items()}

    def search(self, q, k=3):
        qt = set(grams(q))
        scored = []
        for i, ts in enumerate(self.sets):
            hit = sum(self.idf.get(t, 0) for t in qt if t in ts)
            if hit <= 0:
                continue
            # 除以块长的 0.25 次方：轻微惩罚长块，免得"最长的那块总能命中最多词"
            scored.append((hit / (len(ts) ** 0.25), i))
        scored.sort(reverse=True)
        return scored[:k]


# ── 检索法 2：向量 ────────────────────────────────────────────────────

class VectorIndex:
    def __init__(self, chunks):
        self.chunks = chunks
        self._qcache = {}          # 同一个问题的向量只算一次（离线评测会反复问同一批题）
        CACHE.mkdir(exist_ok=True)
        tag = f"emb_{EMB_MODEL}_{len(chunks)}"
        fp = CACHE / f"{tag}.json"

        if fp.exists():
            d = json.loads(fp.read_text(encoding="utf-8"))
            if d.get("ids") == [c["id"] for c in chunks]:
                self.mat = np.array(d["vectors"], dtype=np.float32)
                print(f"  （向量从缓存读：{fp.name}，没花钱）")
                return

        print(f"  第一次跑，要把 {len(chunks)} 段文字送去向量化（花钱，之后走缓存）：")
        t0 = time.time()
        mat = embed([c["text"] for c in chunks])
        print(f"  向量化完成：{mat.shape[0]} 段 × {mat.shape[1]} 维，用了 {time.time() - t0:.1f} 秒")
        self.mat = mat
        fp.write_text(json.dumps({
            "model": EMB_MODEL,
            "ids": [c["id"] for c in chunks],
            "vectors": mat.tolist(),
        }, ensure_ascii=False), encoding="utf-8")

    def search(self, q, k=3):
        if q not in self._qcache:
            self._qcache[q] = embed([q], quiet=True)[0]
        qv = self._qcache[q]
        # 余弦相似度 = 点积 / 两个模长相乘。向量先归一化，之后点积就是余弦
        m = self.mat / np.linalg.norm(self.mat, axis=1, keepdims=True)
        qn = qv / np.linalg.norm(qv)
        sims = m @ qn
        order = np.argsort(-sims)[:k]
        return [(float(sims[i]), int(i)) for i in order]


def show_hits(hits, chunks, max_len=60):
    for rank, (score, idx) in enumerate(hits, 1):
        head = chunks[idx]["text"].replace("\n", " ")[:max_len]
        print(f"    {rank}. [{score:6.2f}] {chunks[idx]['id']:<44} {head}…")


# ══════════════════════════════════════════════════════════════════════
# 实验
# ══════════════════════════════════════════════════════════════════════

# 四道题，答案全都只存在于你的文档里（"scrypt"、"ROLE_PERMS"这种词模型猜不出来）
QUESTIONS = [
    "这个系统的登录密码是明文存的吗？用的什么哈希？",
    "权限表里没登记的路由，默认是什么行为？",
    "科室停用之后，以前的挂号记录还能不能查到？",
    "住院患者'同一时间只能住院一次'是怎么保证的？",
]

# 一道文档里根本没有的题，用来看 RAG 会不会老实说"查不到"
OUT_OF_SCOPE = "机房那台 UPS 的电池一般多久更换一次？"

# ══════════════════════════════════════════════════════════════════════
# 金标准 —— 评测的基准。这里有两套口径，会得出完全相反的结论。
# 两套我都跑给你看，因为这是整步里最重要的一课。
# ══════════════════════════════════════════════════════════════════════

# 口径一（想当然的标法，也是错的）：按"答案在哪一节"标，每题只认一个标题。
GOLD_BY_TITLE = [
    ("这个系统的登录密码是明文存的吗？用的什么哈希？", "技术栈"),
    ("权限表里没登记的路由，默认是什么行为？", "登录与角色权限"),
    ("科室停用之后，以前的挂号记录还能不能查到？", "停用"),
    ("住院患者同一时间只能住院一次，是怎么保证的？", "3.11"),
    ("药品的库存数量是怎么保证不出错的？", "库存流水"),
    ("为什么收费金额要存一份快照？", "快照"),
]

# 口径二（对的标法）：按"答案关键词"标 —— 哪个块里能读到答案，就算命中。
# 因为同一个知识点往往散落在好几节（实测 "scrypt" 在 6 个块里都有）。
GOLD = [
    ("这个系统的登录密码是明文存的吗？用的什么哈希？", ["scrypt"]),
    ("权限表里没登记的路由，默认是什么行为？", ["白名单之外", "必须登录"]),
    ("科室停用之后，以前的挂号记录还能不能查到？", ["历史挂号、病历、住院照常"]),
    ("住院患者同一时间只能住院一次，是怎么保证的？", ["一条记录 = 一次住院", "current_patient_id"]),
    ("药品的库存数量是怎么保证不出错的？", ["before_stock", "change_type"]),
    ("为什么收费金额要存一份快照？", ["快照"]),
]


def gold_by_title(title_kw, chunks):
    return {i for i, c in enumerate(chunks) if title_kw in c["title"]}


def gold_by_answer(needles, chunks):
    return {i for i, c in enumerate(chunks) if any(n in c["text"] for n in needles)}


def run_eval(search_fn, chunks, gold_list, k, gold_fn):
    """算 top-1 和 top-k 命中率。返回 (top1, topk, 逐题明细)。"""
    top1 = topk = 0
    rows = []
    for q, spec in gold_list:
        gold = gold_fn(spec, chunks)
        hits = [i for _, i in search_fn(q, k=k)]      # 注意取的是搜出来的块序号
        h1 = bool(hits and hits[0] in gold)
        hk = bool(gold & set(hits))
        top1 += h1
        topk += hk
        rows.append((q, h1, hk, hits))
    return top1, topk, rows


def exp_a():
    print("\n" + SEP)
    print("实验 A：不检索，直接问 —— 这就是没有 RAG 的世界")
    print(SEP)
    print("\n模型从没见过你的代码和文档。问它实现细节，它会怎么办？\n")

    q = QUESTIONS[0]
    print(f"问：{q}")
    ans, u = chat([{"role": "user", "content": q}], temperature=0.3)
    print(f"\n答：{ans.strip()[:600]}")
    print(f"\n（花了 {u.get('prompt_tokens', 0)} + {u.get('completion_tokens', 0)} tokens）")

    print("\n" + SUB)
    print("看清楚它干了什么 —— 分两步：")
    print("  第一步先撇清：「你没说是哪个系统，我无法给出确切结论。」（这话没错，也很鸡贼）")
    print("  第二步给一份通用清单：bcrypt / Argon2 / PBKDF2 / scrypt 一类的候选都摆出来。")
    print()
    print("  问题就在这儿：**它给的是一份候选清单，不是你的答案。**")
    print("  你真在用的是 werkzeug 的 scrypt —— 它可能顺带提到，也可能压根不提；")
    print("  但不管提没提，它都不会说「你这个项目用的就是它」，因为文档不在它手里。")
    print("  用户照着这份清单去改，很容易改成 bcrypt —— 改了不该改的，还觉得自己有了依据。")
    print()
    print("  所以幻觉最危险的形态不是\"说得天花乱坠\"，而是")
    print("  **\"说得挺专业，但你没法判断它跟你这个系统有没有关系\"**。")
    print("  它在用\"世界上通常怎么做\"回答\"你这里具体怎么做\"——")
    print("  训练数据里有前者，没有后者。")
    print(SUB)


def exp_b():
    print("\n" + SEP)
    print("实验 B：把三份文档全塞进 prompt —— 最朴素的办法，先看账单")
    print(SEP)

    corpus = load_corpus()
    total_chars = sum(len(t) for _, t in corpus)
    print(f"\n知识库规模：{len(corpus)} 份文档，共 {total_chars:,} 字符")
    for path, text in corpus:
        print(f"    {path.relative_to(ROOT).as_posix():<40} {len(text):>7,} 字符")

    docs = "\n\n".join(f"===== {p.relative_to(ROOT).as_posix()} =====\n{t}" for p, t in corpus)
    q = QUESTIONS[0]
    prompt = f"以下是项目文档。请只根据文档回答问题。\n\n{docs}\n\n问题：{q}"

    print(f"\n问：{q}")
    print(f"（本次请求输入约 {len(prompt):,} 字符，等它算完看真实 token）\n")

    t0 = time.time()
    try:
        ans, u = chat([{"role": "user", "content": prompt}], temperature=0.2, timeout=300)
    except requests.Timeout:
        print("✗ 超时了。这本身就是答案：全文塞进去，慢到不可用。")
        return
    print(f"答：{ans.strip()[:400]}")
    pt = u.get("prompt_tokens", 0)
    print(f"\n耗时 {time.time() - t0:.1f} 秒 | prompt_tokens = {pt:,} | completion = {u.get('completion_tokens', 0)}")

    print("\n" + SUB)
    print("答对了 —— 因为答案确实在文档里。但代价是：")
    print(f"  · 问**一个**问题，就要把 {pt:,} 个 token 全发一遍")
    print(f"  · 用户聊 10 轮 = {pt * 10:,} 个 token（每轮都要重发全文，第 2 步学过的）")
    print("  · 真实医院的知识库不是 3 份文档，是几千份制度、手册、接口文档")
    print("  · 超过模型上下文长度就直接报错，连错都答不了")
    print()
    print("  所以 RAG 的思路不是\"塞得更满\"，而是：")
    print("  **先挑出最相关的几段，只塞那几段。**")
    print("  挑 = 检索（Retrieval），剩下的照旧喂给模型生成（Generation）。")
    print(SUB)


def exp_c(chunks):
    print("\n" + SEP)
    print("实验 C：切块 —— RAG 的第一个正经决策")
    print(SEP)
    print("\n检索的最小单位是\"块\"。块切得好不好，直接决定后面能不能搜到。\n")

    by_doc = Counter(c["doc"] for c in chunks)
    lens = [len(c["text"]) for c in chunks]
    print(f"  共切成 {len(chunks)} 块")
    for doc, n in by_doc.items():
        print(f"    {doc:<40} {n:>3} 块")
    print(f"\n  块长度：最短 {min(lens)} 字符 / 中位 {sorted(lens)[len(lens) // 2]} / 最长 {max(lens)}")
    print(f"  平均 {sum(lens) // len(lens)} 字符 —— 一块大概 500~800 token，塞 3 块也不到 3000")

    print("\n  切块规则（本脚本的做法）：")
    print("    1. 按 Markdown 标题切 —— 标题天然就是\"一个话题\"的边界")
    print("    2. 一节超过 900 字符，再按空行切段落组")
    print("    3. **每块开头拼上「文档名 > 章节名」** —— 这一段也是检索线索")

    print("\n  抽一块给你看 —— 就是能回答「权限怎么设计」的那块：")
    demo = next((c for c in chunks if "登录与角色权限" in c["title"]), chunks[0])
    print(f"  块 id：{demo['id']}，{len(demo['text'])} 字符")
    print(SUB)
    print("\n".join(demo["text"].splitlines()[:9]))
    print("  …")
    print(SUB)

    print("\n  为什么第 3 条那么重要：")
    print("  用户问\"权限怎么设计的\"，正文里可能一个\"权限\"两个字都没有，")
    print("  但标题写着「登录与角色权限」—— 拼进块里，关键词和向量都能命中。")
    print("  **标题是免费的高质量摘要，不用白不用。**")
    return chunks


def exp_d(chunks, kw):
    print("\n" + SEP)
    print("实验 D：关键词检索 —— 最土的办法，先建立直觉")
    print(SEP)
    print("\n不调任何模型、不花钱、不联网。纯 Python 数碎片重叠。\n")

    for q in QUESTIONS[:3]:
        print(f"问：{q}")
        show_hits(kw.search(q, k=3), chunks)
        print()

    print(SUB)
    print("先看第一名对不对，别急着看分数：")
    print("  第 1 题问「密码怎么存的」，第一名给的是「基础数据维护」—— 讲药品入库流水的段落。")
    print("  第 3 题问「停用后挂号记录还能不能查」，第一名是「3.1 为什么挂号表要存 dept_id」。")
    print("  只有第 2 题（问句里就有「权限」「路由」「登记」这些原词）第 1 名直接命中。")
    print()
    print("所以这个「土办法」的真相是：")
    print("  **问句和文档用词一致时，它又快又准；换一个说法，立刻废掉。**")
    print("  它不是「大概能用」，是「要么全对要么全错」—— 实验 F 会用 6 道题量化这件事。")
    print()
    print("它另外两个毛病：")
    print("  · 同义词、简称、错别字全无能为力：\"开方\"vs\"处方\"、\"收费\"vs\"结账\"")
    print("  · 2-gram 切法会制造垃圾碎片（\"的权\"\"限角\"这种），偶尔凑巧命中，纯属运气")
    print(SUB)


def exp_e(chunks, vec):
    print("\n" + SEP)
    print("实验 E：向量检索 —— 把文字变成 1024 个数字，比\"意思\"")
    print(SEP)
    print("\n先把每块文字过一遍 embedding 模型，变成一串数字（向量）。\n")

    print(f"  向量矩阵形状：{vec.mat.shape}   （{vec.mat.shape[0]} 块 × {vec.mat.shape[1]} 维）")
    print("  这 1024 个数字没有一个是人能读懂的，但它们之间的距离有意义：")
    print("  意思越接近，两个向量越\"像\"（余弦相似度越接近 1）。")

    q = QUESTIONS[0]
    print(f"\n问：{q}")
    show_hits(vec.search(q, k=3), chunks)

    print("\n换个口语化的问法试试：")
    q2 = "谁能进后台管科室和医生账号？"
    print(f"问：{q2}")
    show_hits(vec.search(q2, k=3), chunks)

    print("\n" + SUB)
    print("看清楚这两次结果：")
    print("  第 1 题第一名是「2.11 staff 员工与登录账号表」—— 那节里写着")
    print("  「password_hash｜密码哈希（scrypt），不存明文」，**它确实能答对这道题**。")
    print("  第 2 题（口语化问「谁能进后台」）也是第一名就中。")
    print()
    print("  但别急着下结论。这两题本来就是「文档里到处都有线索」的题。")
    print("  真正难的是「科室停用后还能不能查」那种 —— 关键词把它甩到第 18 名，向量第 2 名就找到了。")
    print("  到底准不准、比关键词强多少，靠「看一眼」说不清，下一节直接量化。")
    print(SUB)


def exp_f(chunks, kw, vec):
    print("\n" + SEP)
    print("实验 F：评测 —— 别靠感觉，靠数字（以及我踩过的坑）")
    print(SEP)
    print("""
  实验 D 和 E 都只是「看一眼觉得还行」。这在工程上等于没做：
  换个说法、换一天、换个同事来问，你就不知道它好还是坏了。

  RAG 的标准做法是**人工标一份金标准**，然后算命中率。
  但这里有个坑，我自己踩了一次：**金标准怎么标，直接决定结论。**
""")

    n = len(GOLD_BY_TITLE)
    print("【第一步】按「答案在哪一节」标，每题只认一个块（最容易想到的标法）：\n")
    a1, ak, _ = run_eval(kw.search, chunks, GOLD_BY_TITLE, 3, gold_by_title)
    b1, bk, _ = run_eval(vec.search, chunks, GOLD_BY_TITLE, 3, gold_by_title)
    print(f"    {'':<10}{'top-1':>9}{'top-3':>9}")
    print(f"    {'关键词':<11}{a1:>6}/{n}{ak:>8}/{n}")
    print(f"    {'向量':<12}{b1:>6}/{n}{bk:>8}/{n}")
    print("""
  两边一模一样。如果就停在这里，结论会是「换检索算法没用」。
  我第一版就是这么写的，还配了一段"为什么换算法没用"的解说。**那是错的。**
""")

    scrypt_blocks = gold_by_answer(["scrypt"], chunks)
    print("【第二步】核对那道被判成失败的题，发现不对劲：")
    print("    第一题模型明明答对了 scrypt，评测却判它失败。")
    print(f"    去库里搜 \"scrypt\" —— 它其实出现在 {len(scrypt_blocks)} 个块里：")
    for i in sorted(scrypt_blocks)[:4]:
        print(f"      · {chunks[i]['title'][:44]}")
    print("""
    而第一题检索到的**第 1 名**就是「2.11 staff 员工与登录账号表」，那一节白纸黑字写着：
        password_hash | VARCHAR(255) | 密码哈希（scrypt），不存明文
    **模型答对了，是我的金标准标窄了。**

    一个知识点往往散落在好几节。把答案只钉在某一节上，评测就会冤枉检索，
    然后你拿着错误的数据去做决策 —— 比如"算法没用，别折腾了"。
""")

    print("【第三步】换成「哪个块里能读到答案关键词，就算命中」，重测：\n")
    n = len(GOLD)
    a1, ak, rows = run_eval(kw.search, chunks, GOLD, 3, gold_by_answer)
    b1, bk, _ = run_eval(vec.search, chunks, GOLD, 3, gold_by_answer)
    print(f"    {'':<10}{'top-1':>9}{'top-3':>9}")
    print(f"    {'关键词':<11}{a1:>6}/{n}{ak:>8}/{n}")
    print(f"    {'向量':<12}{b1:>6}/{n}{bk:>8}/{n}")

    print("\n    逐题看关键词（✓ 第 1 名对 / △ 前 3 名里有 / ✗ 全没中）：")
    for q, h1, hk, hits in rows:
        mark = "✓" if h1 else ("△" if hk else "✗")
        got = chunks[hits[0]]["title"][:30] if hits else "-"
        print(f"      {mark} {q[:22]:<24} -> 第 1 名：{got}")

    print("""
  **同一个系统、同一批问题、同一套检索代码，只换了金标准的标法，
  结论就从「两边一样烂」变成「向量明显更好」。**

  所以记住这句：**评测本身可不可信，比选哪个算法重要一百倍。**
  以后看到别人的 RAG 评测数字，先问一句"你的金标准怎么标的"。
""")

    print(SUB)
    print("那向量到底好在哪？把「正确答案实际排第几名」打出来：\n")
    print(f"    {'问句':<24}{'关键词':>10}{'向量':>10}")
    for q, needles in GOLD:
        gold = gold_by_answer(needles, chunks)

        def rank(fn, _g=gold, _q=q):
            hits = [i for _, i in fn(_q, k=len(chunks))]
            ps = [p + 1 for p, i in enumerate(hits) if i in _g]
            return min(ps) if ps else None

        rk, rv = rank(kw.search), rank(vec.search)
        print(f"    {q[:22]:<24}{(str(rk) + ' 名'):>10}{(str(rv) + ' 名'):>10}")

    print("""
  看「科室停用」那一行：关键词把正确答案甩到第 18 名，向量还稳在第 2 名。
  **这就是向量真正的价值 —— 不是「更准」，是「不会错得太远」。**
  关键词是「要么第一，要么失踪」；向量是「常年第二第三，但从不失踪」。
  碰到一个刁钻问法，前者直接不可用，后者还能兜住。

  那 k 到底该取多少？只能测：
""")

    print(f"    {'k':<5}{'关键词 top-1 / top-k':>26}{'向量 top-1 / top-k':>24}")
    for k in (1, 3, 5, 8):
        a1, ak, _ = run_eval(kw.search, chunks, GOLD, k, gold_by_answer)
        b1, bk, _ = run_eval(vec.search, chunks, GOLD, k, gold_by_answer)
        sa = f"{a1}/{len(GOLD)}   {ak}/{len(GOLD)}"
        sb = f"{b1}/{len(GOLD)}   {bk}/{len(GOLD)}"
        print(f"    {k:<5}{sa:>26}{sb:>24}")

    print("""
  k=1 到 k=3 有提升，再往上就平了 —— **k=3 就是这个小知识库的甜点**，再大只是白烧 token。
  （我第一版因为金标准标错，得出过"k 从 3 提到 5 就全中"的结论，那是假象。）
""")

    print(SUB)
    print("顺便说两个「听起来高级但没救」的办法，我都真跑过：")
    print("  · 混合检索（关键词分 + 向量分归一化加权）：没有提升。")
    print("    因为两路错的恰好是同一批题，取并集自然也没变。")
    print("  · HyDE（先让模型编一段假答案，拿假答案去检索）：更差。")
    print("    它编的假答案里写的是「PBKDF2」，而你项目实际用的是 scrypt。")
    print("    **用幻觉去检索，只会检索到更像幻觉的东西。**")
    print()
    print("  正确顺序：先定金标准 -> 再量基线 -> 再调（k、切块、标题）-> 最后才谈换算法。")
    print("  跳过评测直接换算法，你连「到底变好没有」都说不清。")
    print(SUB)


def exp_g(chunks, vec):
    print("\n" + SEP)
    print("实验 G：完整 RAG —— 检索 → 拼 prompt → 带出处回答")
    print(SEP)

    def rag_answer(question, k=3):
        hits = vec.search(question, k)
        ctx = "\n\n".join(
            f"[片段 {i + 1}]（{chunks[idx]['doc']} > {chunks[idx]['title']}）\n{chunks[idx]['text']}"
            for i, (_, idx) in enumerate(hits)
        )
        sys_prompt = (
            "你是这个医院信息管理系统的文档助手。规则：\n"
            "1. 只能依据下面给出的资料回答，不许用你自己的通用知识补全。\n"
            "2. 资料里没有的，直接回答「文档里没有写」，并说明缺什么信息。\n"
            "3. 回答末尾用 [片段 N] 标注依据。\n"
            "4. 用大白话，两三句说完，不要罗列大纲。\n\n"
            f"资料：\n{ctx}"
        )
        ans, u = chat([
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": question},
        ], temperature=0.1)
        return ans, hits, u

    # ── 先做一个对照：同模型、同提示词，只改喂几块 ──
    q = "科室停用之后，以前的挂号记录还能不能查到？"
    print("\n先做个对照：同一道题、同一个模型、同一句提示词，只改喂进去的块数。")
    print("（这题是实验 F 里关键词最惨的一题 —— 正确答案被它排到第 18 名）\n")

    got = {}
    for k in (1, 3):
        ans, hits, u = rag_answer(q, k=k)
        got[k] = ans
        print(f"  ── k={k}（喂 {k} 块，prompt={u.get('prompt_tokens', 0)} tokens）")
        print(f"     答：{ans.strip().replace(chr(10), ' ')[:300]}")
        print()

    # 模型不是每次都一模一样，所以解说按实际回答走，别写死
    refused = any(w in got[1] for w in ("没有写", "没写", "未提及", "未包含", "无法回答"))

    print(SUB)
    if refused:
        print("  k=1 那次直接说「文档里没有写」—— 它手上只有「2.1 department 科室表」，")
        print("  那块只定义了 status 字段，确实说不了「历史记录还能不能查」。**它没有编，这是对的。**")
        print("  k=3 喂到了「3.12 为什么停用而不是删除」，它立刻答对，还讲清了哪几类记录照常查。")
        print()
        print("  k=1 那次的反应值得单独夸一句：**信息不够的时候，它老实承认了。**")
        print("  这比硬凑一个答案好得多，说明提示词里那条「资料里没有就说没写」真的有用。")
    else:
        print("  k=1 只有一块可看，它只能从表结构的 status 字段往回推，答得笼统；")
        print("  k=3 把「3.12 为什么停用而不是删除」也喂进来了，它就能说清哪几类记录照常查。")
        print()
        print("  注意 k=1 这次它仍然答了 —— 这时候要盯着看**它答得对不对、细节够不够**，")
        print("  而不是只看「有没有回答」。答了不等于答对了。")
    print()
    print("  **模型没变、提示词没变、检索算法没变 —— 只改了喂几块。**")
    print("  喂得多，它能说清的细节多；但喂太多就是白烧 token（看实验 F 的 k 曲线）。")
    print(SUB)

    print("\n再跑两题，注意它给的依据：")
    for q2 in QUESTIONS[1:3]:
        print(f"\n问：{q2}")
        ans, hits, u = rag_answer(q2)
        print(f"答：{ans.strip()[:400]}")
        print(f"    （喂 {len(hits)} 块，prompt={u.get('prompt_tokens', 0)} tokens）")

    print("\n" + SUB)
    print("把三个数字放一起看：")

    print("  A 不检索：prompt 几十个 token    -> 答案看着像真的，其实是编的")
    print("  B 全塞：  prompt 20,000+ token  -> 答案对，但每次问答都付全价")
    print("  G RAG：   prompt 一两千 token   -> 答案对，还能指出出处")

    print("\n最后一件最关键的事：查不到的时候，它会不会装懂？")
    print(f"\n问：{OUT_OF_SCOPE}（你的文档里绝对没有这一条）")
    ans, hits, u = rag_answer(OUT_OF_SCOPE)
    print(f"答：{ans.strip()[:400]}")

    print("\n" + SUB)
    print("注意 system 里那句「资料里没有的，直接回答文档里没有写」——")
    print("**这句话是 RAG 系统里最值钱的一句提示词。**")
    print("没有它，模型会拿检索到的无关片段硬凑一个答案；")
    print("有了它，前端才能对用户说\"这个我查不到，转人工\"，而不是给一个看起来很像的错答案。")
    print()
    print("但光靠提示词还不够 —— 提示词是\"请求\"，不是\"保证\"（第 3 步学过）。")
    print("生产环境还要加两层：")
    print("  · **相似度阈值**：top-1 相似度低于阈值就不调模型，直接返回「未检索到」。")
    print("    但阈值不能抄 —— 这套语料的相似度普遍在 0.5~0.7，抄个 0.8 会把所有问题都挡掉。")
    print("    得拿一批「该答」和「不该答」的问题各跑一遍，取中间那个点。")
    print("  · **引用校验**：模型标的 [片段 N] 里如果根本没有答案关键词，判为幻觉，打回重答。")
    print(SUB)


def main():
    print(SEP)
    print("第 7 步：RAG")
    print(SEP)
    print(f"模型：{MODEL}")
    print(f"向量模型：{EMB_MODEL}")
    print(f"知识库：你自己的 HIS 文档（{len(KB_FILES)} 份）")

    chunks = build_chunks()
    if not chunks:
        sys.exit("✗ 一块都没切出来，检查 KB_FILES 路径")

    exp_a()
    exp_b()
    exp_c(chunks)

    kw = KeywordIndex(chunks)
    exp_d(chunks, kw)

    print("\n  准备向量（第一次会调接口，之后走 _cache/ 缓存）：\n")
    vec = VectorIndex(chunks)
    exp_e(chunks, vec)

    exp_f(chunks, kw, vec)
    exp_g(chunks, vec)

    print("\n" + SEP)
    print("本次总消耗")
    print(SEP)
    print(f"  chat  prompt={USAGE['prompt']:,}  completion={USAGE['completion']:,}")
    print(f"  向量化 {len(chunks)} 段（首次跑才花，之后缓存）")
    print()
    print("  换成钱的话是几分钱级别。但注意实验 B 那种用法 ——")
    print("  全量塞文档，一次问答就够你把这块额度用完。")
    print()
    print(SEP)
    print("六句话收尾")
    print(SEP)
    print("  1. RAG = 检索 + 生成。模型不变，只是把\"相关的那几段\"提前塞给它。")
    print("  2. 没有 RAG，模型用通用知识回答你的具体实现 —— 看起来对，实际上是编的。")
    print("  3. 切块按标题切，块头拼上「文档 > 章节」，这是零成本的质量提升。")
    print("  4. 向量比关键词强在「不会错得太远」，不在「更准」；但先别换算法，先标金标准。")
    print("  5. 提示词里的\"查不到就说查不到\"必须写，但还要有相似度阈值兜底。")
    print("  6. 每一步都要能看见数字：token、命中块、相似度、排名。（看不见就没法调）")


if __name__ == "__main__":
    main()
