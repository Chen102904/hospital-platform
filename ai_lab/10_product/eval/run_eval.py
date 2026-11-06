#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
第 10 步·端到端评测（evaluation）——把「我觉得它答得挺好」变成「这一轮有几题没答对，是哪几题」。

跑法（在 10_product/eval 目录下，或者在 ai_lab 目录下写全路径）：

    # 只看真值，不花钱、不调模型
    python run_eval.py --truth

    # 只跑 2 题，先确认链路通
    python run_eval.py --limit 2

    # 全量跑，报告写到默认位置
    python run_eval.py

    # 只跑指定的几题 / 某一类
    python run_eval.py --only A01,B02,F01
    python run_eval.py --cat 查文档

    # 换个模型对比（改环境变量后跑）
    python run_eval.py --tag before-change

它和 tests/check_all.py 的分工：
    check_all.py  → 「代码没坏」：HTTP 200、记录落库、状态是 ok、工具被调用
    run_eval.py   → 「答得对」  ：金额有没有抄错、该拒答的拒了没、空结果有没有瞎编

这两件事没有交集。之前 87 项全绿的时候，模型把 1159.40 说成别的数照样全绿。
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
import argparse
import importlib.util
import io
import json
import re
import sys
import time
import unicodedata
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]                       # ai_lab/
CASES_FILE = HERE / "cases.json"
REPORT_DIR = HERE / "reports"

# 08_rag_agent.py 里有 `from mykey import ...` `import db_ro` 这类同级导入。
# 用 importlib 按路径加载时，Python 不会自动把文件所在目录加进 sys.path，
# 于是它会报「没找到 mykey.py」——明明文件就在那儿。必须先手动加进去。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ══════════════════════════════════════════════════════════════════════
# 一、加载第 8 步的 core（不复制任何业务代码）
# ══════════════════════════════════════════════════════════════════════

def load_core(quiet=True):
    spec = importlib.util.spec_from_file_location("rag08", ROOT / "08_rag_agent.py")
    if spec is None:
        raise SystemExit(f"✗ 找不到 {ROOT / '08_rag_agent.py'}")
    m = importlib.util.module_from_spec(spec)
    if quiet:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            spec.loader.exec_module(m)
    else:
        spec.loader.exec_module(m)
    return m


def init_index(core):
    """search_docs 依赖模块级 IDX，main() 里才给它赋值。这里替它做掉。"""
    with redirect_stdout(io.StringIO()):
        core.IDX = core.DocIndex(core.build_chunks())
    return len(core.IDX.chunks)


# ══════════════════════════════════════════════════════════════════════
# 二、取真值 —— 独立于工具的 SQL，顺带交叉验证工具实现有没有错
# ══════════════════════════════════════════════════════════════════════
# 注意：这里刻意不复用 08 里的 dept_revenue_rank()/low_stock_medicines()。
# 如果两边算出来的数不一样，说明工具实现的 SQL 有问题 —— 那是比"模型答错"
# 更严重的 bug，必须在跑分之前就发现。

def one(core, sql, params=()):
    r = core.run_select(sql, params)
    if not r.get("ok"):
        raise SystemExit(f"✗ 取真值失败：{r.get('reason')}\n  SQL: {sql}")
    rows = r.get("rows") or []
    return rows[0] if rows else {}


def build_truth(core, days=30):
    """把题库里所有 ${XXX} 占位符算成真实值。"""
    t = {}

    rev = core.run_select(
        """SELECT d.dept_name      AS dept,
                  SUM(cd.amount)  AS amt,
                  COUNT(DISTINCT c.charge_id) AS cnt
             FROM charge_detail cd
             JOIN charge       c ON c.charge_id = cd.charge_id
             JOIN registration r ON r.reg_id    = c.reg_id
             JOIN department   d ON d.dept_id   = r.dept_id
            WHERE c.charge_status = '已收费'
              AND c.charge_time  >= DATE_SUB(CURDATE(), INTERVAL %s DAY)
            GROUP BY d.dept_name
            ORDER BY amt DESC""", (days,))
    rows = rev.get("rows") or []
    if not rows:
        raise SystemExit(f"✗ 近 {days} 天没有任何收费数据，题库的金额类期望值没法填。"
                         f"先重灌演示数据：cd hospital_db_project && python seed_demo_data.py")
    amts = [float(x["amt"]) for x in rows]
    names = [x["dept"] for x in rows]
    for i in range(5):
        t[f"TOP{i+1}_AMT"] = round(amts[i], 2)
    t["LAST_AMT"] = round(amts[-1], 2)
    t["LAST_DEPT"] = names[-1]
    t["N_DEPT_30"] = len(rows)

    t["INNER_CNT"] = int(next((x["cnt"] for x in rows if x["dept"] == "内科"), 0))

    lows = (core.run_select(
        """SELECT medicine_name AS name, stock, unit
             FROM medicine
            WHERE status = '在用'
            ORDER BY stock ASC LIMIT 1""").get("rows") or [{}])
    t["STOCK_MIN_NAME"] = (lows[0] or {}).get("name", "未知")
    t["STOCK_MIN_QTY"] = int((lows[0] or {}).get("stock", 0))

    highs = (core.run_select(
        """SELECT medicine_name AS name, stock, unit
             FROM medicine
            WHERE status = '在用'
            ORDER BY stock DESC LIMIT 1""").get("rows") or [{}])
    t["STOCK_MAX_NAME"] = (highs[0] or {}).get("name", "未知")
    t["STOCK_MAX_QTY"] = int((highs[0] or {}).get("stock", 0))

    t["LOW50_QTY"] = int(one(core,
        "SELECT stock FROM medicine WHERE status='在用' AND stock < 50 "
        "ORDER BY stock ASC LIMIT 1").get("stock", 0))

    t["IBU_QTY"] = int(one(core,
        "SELECT stock FROM medicine WHERE medicine_name='布洛芬缓释胶囊' "
        "AND status='在用'").get("stock", 0))

    t["N_TABLE"] = int(one(core,
        "SELECT COUNT(*) AS n FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = 'hospital_db'").get("n", 0))

    t["N_DEPT_60"] = int(one(core,
        """SELECT COUNT(DISTINCT d.dept_name) AS n
             FROM charge_detail cd
             JOIN charge c ON c.charge_id = cd.charge_id
             JOIN registration r ON r.reg_id = c.reg_id
             JOIN department d ON d.dept_id = r.dept_id
            WHERE c.charge_status = '已收费'
              AND c.charge_time >= DATE_SUB(CURDATE(), INTERVAL 60 DAY)""").get("n", 0))

    t["_DAYS"] = days
    return t


# ══════════════════════════════════════════════════════════════════════
# 三、题库装载与占位符填充
# ══════════════════════════════════════════════════════════════════════

def load_cases(truth, only=None, cat=None, limit=None):
    data = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    cases = data["cases"]

    def sub(x, depth=0):
        if depth > 4:
            return x
        if isinstance(x, str):
            m = re.fullmatch(r"\$\{(\w+)\}", x)
            if m:                                   # 整个字段就是一个占位符 → 保留类型
                key = m.group(1)
                if key not in truth:
                    raise SystemExit(f"✗ 题库引用了不存在的真值：{key}")
                return truth[key]
            return re.sub(r"\$\{(\w+)\}",
                          lambda g: str(truth[g.group(1)])
                          if g.group(1) in truth
                          else (_ for _ in ()).throw(
                              SystemExit(f"✗ 题库引用了不存在的真值：{g.group(1)}")),
                          x)
        if isinstance(x, list):
            return [sub(i, depth + 1) for i in x]
        if isinstance(x, dict):
            return {k: sub(v, depth + 1) for k, v in x.items()}
        return x

    cases = [sub(c) for c in cases]
    if only:
        want = {i.strip().upper() for i in only.split(",") if i.strip()}
        cases = [c for c in cases if c["id"].upper() in want]
        if not cases:
            raise SystemExit(f"✗ --only {only} 没匹配到任何题")
    if cat:
        cases = [c for c in cases if cat in c.get("cat", "")]
        if not cases:
            raise SystemExit(f"✗ --cat {cat} 没匹配到任何题")
    if limit:
        cases = cases[:limit]
    return cases, data.get("version", 0)


# ══════════════════════════════════════════════════════════════════════
# 四、判分
# ══════════════════════════════════════════════════════════════════════

NUM_RE = re.compile(r"\d+(?:\.\d+)?")

# 「我查不到」的表述。注意这几种语义要分开统计：
#   REFUSE   → 知识库里没有这个东西（D 类、F05/F06）
#   NO_DATA  → 查询结果为空，问题本身是有效的（B01、F01~F03）
# 两者都不是"错"，但混在一起统计就看不出系统是"不会编"还是"不肯说"。
#
# ⚠ 这里必须用正则而不是固定子串，原因是第一次跑分时踩到的：
#   · 模型写「文档里**没**找到」，子串「没有找到」匹配不上（少一个"有"）
#   · 模型写「没有库存低于 20 的药品」，子串「没有低于」匹配不上（中间插了"库存"）
#   中文表达中间很容易插入修饰语，固定子串判据在这种地方会大量误判。
REFUSE_RE = [
    r"(文档|资料|知识库|记录|系统|这里)[^。！？\n]{0,12}(没|未|无)",
    r"(我|这边|手头|目前|暂时)[^。！？\n]{0,12}(没有|不具备|无法)",
    r"(没有|不具备)[^。！？\n]{0,10}(工具|能力|权限|信息|数据)",
    r"没找到|没有找到|查不到|查不了|查不着",
    r"无法(回答|确认|提供|获取|查询|判断)",
    r"不知道|不清楚|不确定",
    r"没有(相关|这类|这方面)",
    # 「不在我的服务范围内」——实测 D02（WiFi 密码）就这么答的，判分没认出来。
    # 这种"划边界"式拒答比"我不知道"更常见，也必须算拒答。
    r"不在[^。！？\n]{0,10}(范围|职责|服务)",
    # 「没法直接给你」——A06 实测模型说工具做不到，这句其实已经是拒答了。
    # 不认出来，误拒率就会漏统计（看着 0 误拒，实际有一题）。
    r"没法(直接)?(给|提供|告诉|查|算)",
]
NO_DATA_RE = [
    r"没有[^。！？\n]{0,12}(低于|小于|少于|达到|符合|满足)",
    r"没有(任何|一条|需要|符合)",
    r"0\s*(种|条|个|款|项|例|笔)",
    r"零条|暂无",
    r"没有(数据|记录|收费|产生)",
    # 「近 7 天没有科室收入数据」——F01 实测原文。「没有」和「数据」中间
    # 隔了「科室收入」四个字，上面那条紧邻写法接不住。
    r"没有[^。！？\n]{0,10}(数据|记录|收费单|收入)",
    r"(清单|结果|列表|集合)?(是|为)空",
    r"(清单|结果|列表|集合)?(是|为)空",
    # 「一个都没有」这种口语化的空集说法。B01 实测原文就是
    # 「库存低于 20 的药品一个都没有」——量词把「没有」和「低于」拆开了，
    # 上面第一条的 12 字窗口够不着，只能单独认。
    r"(一个|一条|一款|一项|一例|一张|一单|一笔|一种)也?都没有",
]
CITE_PAT = ["readme.md", "数据库设计文档", ".md", "hospital_web", "hospital_db_project"]


def norm(s):
    """全角转半角、去 Markdown 标记、去千分位逗号、去所有空白、转小写。

    少这一步会误判大量题目，而且错得很难看：
      · 模型爱写 1,159.40 元和 1159.4，两者其实是一个数
      · 模型爱写 **9** 张，加粗标记会把"9"和"张"隔开，导致"答了 9"被
        判成"没答 9" —— 第一次跑分 A06 就是这么误判的
    """
    s = unicodedata.normalize("NFKC", str(s))
    s = re.sub(r"[*_`#>~\[\]()]", "", s)          # Markdown 标记
    s = s.replace(",", "").replace("，", "")
    s = re.sub(r"\s+", "", s)
    return s.lower()


def extract_nums(s):
    s = unicodedata.normalize("NFKC", str(s)).replace(",", "")
    return [float(x) for x in NUM_RE.findall(s)]


def has_any(text_norm, pats, regex=False):
    """regex=True 时按正则匹配（pats 已经是正则），否则按子串。"""
    if regex:
        return any(re.search(p, text_norm) for p in pats)
    return any(norm(p) in text_norm for p in pats)


def says_refuse(ans_norm):
    return has_any(ans_norm, REFUSE_RE, regex=True)


def says_no_data(ans_norm):
    return has_any(ans_norm, NO_DATA_RE, regex=True)


def grade(case, result):
    """返回 (是否通过, 人话理由, 附加标记)。"""
    ans = (result.get("answer") or "").strip()
    status = result.get("status")
    flags = {}

    if status != "ok":
        return False, f"没有正常收尾（status={status}），答案不可用", flags

    n_ans = norm(ans)
    nums = extract_nums(ans)

    if case.get("expect_refuse"):
        ok = says_refuse(n_ans)
        if not ok and not nums:
            return False, "没有拒答，而且给出的内容里连数字都没有——多半是泛泛而谈", flags
        return ok, ("正确拒答了" if ok else "应该拒答却给了答案"), flags

    if case.get("expect_no_data"):
        ok = says_no_data(n_ans)
        bad = [w for w in case.get("must_not", []) if norm(w) in n_ans]
        if bad:
            return False, f"空结果却提到了不该出现的「{bad[0]}」——大概率是编的", flags
        if not ok:
            return False, "应该回答「没有数据」但没说出来", flags
        if nums and not any(abs(n - v) < 0.011 for n in nums for v in
                            [float(x) for x in case.get("must_num", [])]):
            # 空结果题里出现数字不一定是错（可能提到库里最新日期），只做标记不判失败
            flags["mentions_number"] = True
        return True, "正确识别了空结果", flags

    missing = []
    for want in case.get("must_num", []):
        w = float(want)
        if not any(abs(n - w) < 0.011 for n in nums):
            missing.append(f"数字 {w:g}")
    for want in case.get("must_text", []):
        if norm(want) not in n_ans:
            missing.append(f"关键词「{want}」")
    any_list = case.get("must_any") or []
    if any_list and not has_any(n_ans, any_list):
        missing.append("同义表述（" + " / ".join(str(x) for x in any_list[:4]) + "）")
    # must_any_re：给中文的自由表述留的口子。
    # 「没有明确提到」「并没有做」这类说法，固定子串永远追不上——中间随便插两个字就断。
    any_re = case.get("must_any_re") or []
    if any_re and not has_any(n_ans, any_re, regex=True):
        missing.append("同义表述（正则未命中）")

    hit_not = [w for w in case.get("must_not", []) if norm(w) in n_ans]
    if hit_not:
        return False, f"出现了明确不该有的内容「{hit_not[0]}」", flags
    if missing:
        # 非关题答成拒答 → 这是「误拒」，比答错更伤用户
        if says_refuse(n_ans):
            flags["false_refusal"] = True
            return False, "误拒：这题本该能答，" + "；".join(missing), flags
        return False, "缺 " + "；".join(missing), flags

    return True, "全部断言命中", flags


# ══════════════════════════════════════════════════════════════════════
# 五、跑分主流程
# ══════════════════════════════════════════════════════════════════════

def run_one(core, case, retries=2, backoff=20):
    """跑一道题。请求层失败（限流/额度/网络）重试，不把基础设施故障当成模型答错。

    为什么要重试：实测有一轮 38 题跑到第 31 题时连续返回
    `HTTP 403 AccessDenied.Unpurchased`，6 题在 0.2 秒内 status=error。
    那些题根本没测到模型，却被算进分母，准确率从 97.4% 被拉到 81.6%——
    这个数字是假的，会诱导你去改根本没问题的代码。
    """
    t0 = time.time()
    buf = io.StringIO()
    last = None
    for attempt in range(retries + 1):
        try:
            with redirect_stdout(buf), redirect_stderr(buf):
                r = core.run_agent(case["q"], max_rounds=6, max_seconds=120, verbose=False)
            tools = list(dict.fromkeys(x["tool"] for x in (r.get("trace") or [])))
            return r, tools
        except Exception as e:                   # 单题崩了不能拖垮整轮
            last = f"{type(e).__name__}: {e}"
            if attempt < retries:
                wait = backoff * (attempt + 1)
                print(f" [重试 {attempt + 1}/{retries}，等 {wait}s]", end="", flush=True)
                time.sleep(wait)
    r = {"answer": "", "status": "error", "trace": [], "tokens": 0,
         "elapsed": round(time.time() - t0, 1), "error": last}
    return r, []


def evaluate(cases, core, tag="", retries=2, backoff=20):
    results = []
    total = len(cases)
    t_all = time.time()

    for i, case in enumerate(cases, 1):
        print(f"  [{i:>2}/{total}] {case['id']:<4} {case['q'][:34]:<36}", end="", flush=True)
        r, tools = run_one(core, case, retries=retries, backoff=backoff)
        ok, why, flags = grade(case, r)

        # 期望工具必须「全都调到」。早先用 set & set 判交集，多工具题只调一个也算命中，
        # 等于把 E02/E03 这种双工具题的开卷答案悄悄放过——是判分器自己的漏洞。
        want_tools = case.get("expect_tools") or []
        missing_tools = [t for t in want_tools if t not in tools]
        tool_hit = not missing_tools
        cited = has_any(norm(r.get("answer") or ""), CITE_PAT) if case["cat"] == "查文档" else None

        rec = {
            "id": case["id"], "cat": case["cat"], "q": case["q"],
            "pass": ok, "why": why, "flags": flags,
            "answer": (r.get("answer") or "")[:600],
            "status": r.get("status"), "tools": tools,
            # 异常详情必须留下来。实测有一轮 F03/F05/F06 连着 3 题 0.2 秒 status=error，
            # 报告只写"没有正常收尾"，看不出是限流还是代码崩了——基础设施故障和
            # 模型答错是两回事，前者不该算进准确率。
            "error": r.get("error"),
            "want_tools": want_tools, "tool_hit": tool_hit,
            "missing_tools": missing_tools,
            "elapsed": r.get("elapsed", 0), "tokens": r.get("tokens", 0),
            "rounds": r.get("rounds"),
            "expect_refuse": bool(case.get("expect_refuse")),
            "expect_no_data": bool(case.get("expect_no_data")),
            "cited": cited,
            "why_case": case.get("why", ""),
        }
        results.append(rec)
        # error 不画 ✗ —— 那会让人以为模型答错了，其实它根本没被叫起来。
        if r.get("status") == "error":
            mark, why = "⚠", "未测到（请求层失败，见 error 字段）"
        else:
            mark = "✓" if ok else "✗"
        print(f" {mark} {r.get('elapsed', 0):>5.1f}s  {why[:46]}")

    return results, round(time.time() - t_all, 1)


def summarize(results):
    # 「未测到」和「答错」是两件事，必须分开算。
    # status=error 意味着请求层就失败了（额度耗尽 / 限流 / 网络），模型压根没开口，
    # 拿它当"答错"会系统性低估准确率。这里把这类题从分母里剔出去，
    # 同时单独报出来，让人一眼看出这轮成绩可不可信。
    untested = [x for x in results if x["status"] == "error"]
    tested = [x for x in results if x["status"] != "error"]
    n = len(tested)
    passed = sum(1 for x in tested if x["pass"])
    s = {
        "n_total": len(results),
        "untested": len(untested),
        "untested_ids": [x["id"] for x in untested],
        "n": n,
        "passed": passed,
        "acc": round(passed / n * 100, 1) if n else 0.0,
        # n_total 口径的准确率也留着——如果未测到很多，这个数会很难看，
        # 正好提醒「这轮不算数」，不要让两者悄悄打架。
        "acc_raw": round(passed / len(results) * 100, 1) if results else 0.0,
        "trustworthy": len(untested) <= len(results) * 0.1,
        "tokens": sum(x["tokens"] or 0 for x in results),
        "elapsed": round(sum(x["elapsed"] or 0 for x in results), 1),
    }

    # 以下所有比例都只算「测到的题」。否则一遇到额度耗尽，
    # 拒答率、工具命中率、分类通过率会一起被拽下来，看上去像全面退化。
    # 非正常收尾：超轮次 / 超时 / 空回答。用户视角就是"等了半天没有答案"，
    # 比答错更严重——答错至少给了东西。这一项单独统计，别混进"答错"里。
    s["abnormal"] = sum(1 for x in tested if x["status"] != "ok")
    s["abnormal_ids"] = [x["id"] for x in tested if x["status"] != "ok"]

    # 拒答类：该拒的拒了 / 该拒总数
    ref = [x for x in tested if x["expect_refuse"]]
    s["refuse_n"] = len(ref)
    s["refuse_ok"] = sum(1 for x in ref if x["pass"])
    s["refuse_rate"] = round(s["refuse_ok"] / len(ref) * 100, 1) if ref else None

    # 误拒：本该正常回答却被判成拒答
    s["false_refusal"] = sum(1 for x in tested if x["flags"].get("false_refusal"))

    # 空结果
    nd = [x for x in tested if x["expect_no_data"]]
    s["nodata_n"] = len(nd)
    s["nodata_ok"] = sum(1 for x in nd if x["pass"])

    # 工具命中（只看有明确期望工具的题）
    tw = [x for x in tested if x["want_tools"]]
    s["tool_n"] = len(tw)
    s["tool_ok"] = sum(1 for x in tw if x["tool_hit"])
    s["tool_rate"] = round(s["tool_ok"] / len(tw) * 100, 1) if tw else None

    # 引用标注（只看查文档类）
    ci = [x for x in tested if x["cited"] is not None]
    s["cite_n"] = len(ci)
    s["cite_ok"] = sum(1 for x in ci if x["cited"])
    s["cite_rate"] = round(s["cite_ok"] / len(ci) * 100, 1) if ci else None

    # 分类
    by = {}
    for x in tested:
        c = x["cat"]
        d = by.setdefault(c, {"n": 0, "ok": 0})
        d["n"] += 1
        d["ok"] += 1 if x["pass"] else 0
    s["by_cat"] = by
    return s


# ══════════════════════════════════════════════════════════════════════
# 六、报告
# ══════════════════════════════════════════════════════════════════════

def fmt_report(s, results, truth, meta):
    L = []
    a = L.append
    a(f"# 端到端评测报告 · {meta['tag'] or '基线'}")
    a("")
    a(f"- 跑的时间：{meta['when']}")
    a(f"- 模型：`{meta['model']}`")
    a(f"- 题库版本：v{meta['version']}（{s['n_total']} 题）")
    if s["untested"]:
        a(f"- ⚠ **未测到 {s['untested']} 题**：{', '.join(s['untested_ids'])}"
          f"（请求层失败，模型没开口，不计入准确率分母）")
    a(f"- 知识库：{meta['chunks']} 块")
    a(f"- 总耗时：{round(meta['wall'], 1)} 秒   消耗 {s['tokens']} tokens")
    a("")
    a("> 判分只看最终那段回答，中间怎么走的不管——这就是「端到端」的意思。")
    a("> 所以它和 `tests/check_all.py` 那 87 项没有任何重叠：那边测「代码没坏」，这边测「答得对」。")
    a("")

    a("## 总览")
    a("")
    if not s.get("trustworthy", True):
        a(f"> 🔴 **本轮成绩不可信**：{s['untested']} 题因请求层失败没测到"
          f"（占 {round(s['untested'] / s['n_total'] * 100)}%）。"
          f"准确率按已测的 {s['n']} 题算，但样本已经不完整，别拿它跟别的轮次比。"
          f"先看错误详情确认是额度耗尽、限流还是代码崩了。")
        a("")
    a("| 指标 | 结果 | 说明 |")
    a("|---|---|---|")
    a(f"| **准确率** | **{s['passed']}/{s['n']} = {s['acc']}%** | 所有断言全中的题"
      + (f"（分母已剔除 {s['untested']} 题未测到）" if s["untested"] else "") + " |")
    a(f"| 非正常收尾 | {s.get('abnormal', 0)} 题"
      + (f"（{', '.join(s['abnormal_ids'])}）" if s.get("abnormal") else "")
      + " | 超轮次/超时——用户等半天没拿到答案，比答错更严重 |")
    if s["refuse_rate"] is not None:
        a(f"| 正确拒答率 | {s['refuse_ok']}/{s['refuse_n']} = {s['refuse_rate']}% | 该拒的有没有拒 |")
    a(f"| **误拒** | **{s['false_refusal']} 题** | 本该能答却答成「我不知道」，越低越好 |")
    if s["nodata_n"]:
        a(f"| 空结果识别 | {s['nodata_ok']}/{s['nodata_n']} | 没数据时有没有瞎编 |")
    if s["tool_rate"] is not None:
        a(f"| 工具命中率 | {s['tool_ok']}/{s['tool_n']} = {s['tool_rate']}% | 该调的工具调了没 |")
    if s["cite_rate"] is not None:
        a(f"| 引用标注率 | {s['cite_ok']}/{s['cite_n']} = {s['cite_rate']}% | 查文档题有没有标出处 |")
    a("")

    a("## 分类")
    a("")
    a("| 类别 | 题数 | 通过 | 通过率 |")
    a("|---|---|---|---|")
    for c, d in sorted(s["by_cat"].items(), key=lambda kv: (-kv[1]["n"], kv[0])):
        rate = round(d["ok"] / d["n"] * 100, 1) if d["n"] else 0
        bar = "█" * int(rate / 10) + "░" * (10 - int(rate / 10))
        a(f"| {c} | {d['n']} | {d['ok']} | {bar} {rate}% |")
    a("")

    untested = [x for x in results if x["status"] == "error"]
    fails = [x for x in results if not x["pass"] and x["status"] != "error"]
    a("## 怎么读这份报告")
    a("")
    a("- **先看误拒，再看准确率。** 误拒（本该能答却答成「我不知道」）最伤用户，")
    a("  而且通常是「阈值调太严」或「prompt 写过头」造成的，修起来最快、收益最大。")
    a("- **失败题先看判分理由。** 「缺数字」多半是真答错了；「缺关键词」可能只是表述不同，")
    a("  往下看回答原文自己判断——判分是机器，它不懂同义换说。")
    a("- **工具命中率比准确率更能暴露架构问题。** 模型没调工具、纯靠训练记忆答对了，")
    a("  准确率看不出来，工具命中率会掉。这种「蒙对」在生产里最危险，因为换个问题就崩。")
    a("- **两次跑分差 3% 以内别当回事。** 题库不到 40 道，一道题就占 2~3 个百分点，模型还有随机性。")
    a("  要看的是「哪几题翻了」，翻的那几题才是信息。")
    a("")

    a(f"## 失败明细（{len(fails)} 题）")
    a("")
    if not fails:
        a("全部通过。这种情况下更该怀疑题库是不是太松，而不是系统太好。")
    for x in fails:
        a(f"### {x['id']} · {x['q']}")
        a("")
        a(f"- **类别**：{x['cat']}")
        a(f"- **判分理由**：{x['why']}")
        if x["flags"].get("false_refusal"):
            a("- **标记**：⚠ 误拒（answered with refusal）")
        a(f"- **实际调用的工具**：{('、'.join(x['tools']) if x['tools'] else '（一个都没调）')}"
          f"　期望：{('、'.join(x['want_tools']) if x['want_tools'] else '不强制')}")
        a(f"- **耗时 / token**：{x['elapsed']} 秒 / {x['tokens']}")
        if x.get("error"):
            a(f"- **异常**：`{x['error']}`　（这种是链路/接口出错，不是模型答错，别算进模型能力）")
        if x.get("why_case"):
            a(f"- **出题意图**：{x['why_case']}")
        a("")
        a("回答原文（截断到 600 字）：")
        a("")
        a("```")
        a((x["answer"] or "").strip() or "（空）")
        a("```")
        a("")

    if untested:
        a(f"## 未测到的题（{len(untested)} 题）")
        a("")
        a("这些题在请求层就失败了，模型一个字都没输出。**它们既不算通过也不算失败**，")
        a("已经从上面的准确率分母里剔除。补跑用：")
        a("")
        a("```bash")
        a(f"& $PY run_eval.py --only {','.join(x['id'] for x in untested)} --tag 补跑")
        a("```")
        a("")
        a("| 题号 | 题目 | 耗时 | 错误 |")
        a("|---|---|---|---|")
        for x in untested:
            err = (x.get("error") or "").replace("|", "\\|")
            if "AccessDenied" in err or "403" in err:
                err = "403 额度/权限不足（AccessDenied.Unpurchased）"
            elif "429" in err:
                err = "429 限流"
            a(f"| {x['id']} | {x['q']} | {x['elapsed']}s | {err[:110]} |")
        a("")

    a("## 真值快照")
    a("")
    a("这些数字是跑分前从库里实时算出来的，替掉了题库里的 `${...}` 占位符。")
    a("留在这里是为了**可复现**：明天数据变了、准确率变了，能对照出是数据动了还是代码动了。")
    a("")
    a("| 占位符 | 值 |")
    a("|---|---|")
    for k, v in truth.items():
        if k.startswith("_"):
            continue
        a(f"| `${k}` | {v} |")
    a("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser(description="端到端评测：跑完整题库，看它到底答得对不对")
    ap.add_argument("--only", help="只跑指定题号，逗号分隔，如 A01,B02")
    ap.add_argument("--cat", help="只跑某一类，如 查文档 / 该拒答")
    ap.add_argument("--limit", type=int, help="只跑前 N 题（先试链路用）")
    ap.add_argument("--truth", action="store_true", help="只打印真值，不调模型、不花钱")
    ap.add_argument("--tag", default="", help="给这次跑分起个名字，用于对比改动前后")
    ap.add_argument("--out", help="报告写到哪（默认 eval/reports/eval-时间.md）")
    # 额度是有限的，跑一轮 38 题要十几万 token。中途被限流一次就白跑半轮，
    # 所以给单题做退避重试；重试几次还不行，那题标「未测到」，不算进准确率。
    ap.add_argument("--retries", type=int, default=2,
                    help="请求层失败时单题重试次数，默认 2（共最多 3 次尝试）")
    ap.add_argument("--backoff", type=int, default=20,
                    help="重试退避基数（秒），第 n 次等 n×backoff，默认 20")
    ap.add_argument("--rerender", metavar="JSON",
                    help="从已有的结果 .json 重新生成报告，不调模型、不花钱。"
                         "改报告格式时用它，省得重跑一遍")
    args = ap.parse_args()

    # 用**当前**判分逻辑把已有的答案重新量一遍，再重新排版。
    # 判分改动之后必须这样做：旧报告里的 pass 是旧尺子量出来的，直接换个
    # 排版会得到「summary 说 78% 但正文一条失败都没有」这种自相矛盾的文件。
    if args.rerender:
        p = Path(args.rerender)
        d = json.loads(p.read_text(encoding="utf-8"))
        cases, _ = load_cases(d["truth"])
        by = {c["id"]: c for c in cases}

        for r in d["results"]:
            c = by.get(r["id"])
            if not c:
                continue
            ok, why, flags = grade(c, {"answer": r["answer"], "status": r["status"]})
            r["pass"], r["why"], r["flags"] = ok, why, flags
            r["cat"] = c.get("cat", r["cat"])          # 题库里可能改过分类
        d["summary"] = summarize(d["results"])

        md = fmt_report(d["summary"], d["results"], d["truth"], d["meta"])
        out = Path(args.out) if args.out else p.with_suffix(".md")
        out.write_text(md, encoding="utf-8")
        d["meta"]["regraded"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  已用当前判分重新量过并生成：{out}")
        print(f"  准确率 {d['summary']['passed']}/{d['summary']['n']} = {d['summary']['acc']}%")
        return

    print()
    print("═" * 68)
    print("  端到端评测 —— 只看最终回答对不对，中间怎么走的不算")
    print("═" * 68)

    core = load_core()
    print(f"  模型：{core.MODEL}")
    print("  正在从库里取真值（独立于工具的 SQL，顺带交叉验证工具实现）…")
    truth = build_truth(core)
    print(f"  真值就位：近 {truth['_DAYS']} 天 {truth['N_DEPT_30']} 个科室有收入，"
          f"第一名 {truth['TOP1_AMT']} 元，末位 {truth['LAST_DEPT']} {truth['LAST_AMT']} 元")
    print("  正在初始化文档索引…")
    n_chunks = init_index(core)
    print(f"  知识库 {n_chunks} 块")

    if args.truth:
        print()
        print("  真值全量：")
        for k, v in truth.items():
            if not k.startswith("_"):
                print(f"    {k:<16} {v}")
        print()
        return

    cases, version = load_cases(truth, args.only, args.cat, args.limit)
    print(f"  题库 v{version}：本轮 {len(cases)} 题")
    print("─" * 68)
    print()

    t0 = time.time()
    results, _ = evaluate(cases, core, retries=args.retries, backoff=args.backoff)
    wall = time.time() - t0

    s = summarize(results)
    meta = {
        "when": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "model": core.MODEL, "version": version, "tag": args.tag,
        "chunks": n_chunks, "wall": wall,
    }

    print()
    print("─" * 68)
    print(f"  准确率 {s['passed']}/{s['n']} = {s['acc']}%   "
          f"误拒 {s['false_refusal']} 题   耗时 {round(wall, 1)} 秒   {s['tokens']} tokens")
    if s["untested"]:
        print(f"  ⚠ {s['untested']}/{s['n_total']} 题没测到"
              f"（{', '.join(s['untested_ids'])}）—— 请求层失败，已从分母剔除")
        if not s["trustworthy"]:
            print(f"  🔴 未测到占比 {round(s['untested'] / s['n_total'] * 100)}%，"
                  f"本轮数据不可信，别拿去跟别的轮次比")
    for c, d in sorted(s["by_cat"].items(), key=lambda kv: -kv[1]["n"]):
        print(f"    {c:<10} {d['ok']}/{d['n']}")
    print("─" * 68)

    md = fmt_report(s, results, truth, meta)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = Path(args.out) if args.out else \
        REPORT_DIR / f"eval-{datetime.now().strftime('%m%d-%H%M')}{('-' + args.tag) if args.tag else ''}.md"
    out.write_text(md, encoding="utf-8")

    raw = out.with_suffix(".json")
    raw.write_text(json.dumps({"summary": s, "results": results, "truth": truth,
                               "meta": meta}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    print(f"  报告：{out}")
    print(f"  原始：{raw}")
    print()

    # 退出码：低于 80% 视为不达标，方便接进 CI
    raise SystemExit(0 if s["acc"] >= 80 else 1)


if __name__ == "__main__":
    main()
