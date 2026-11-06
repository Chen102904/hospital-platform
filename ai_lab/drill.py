# -*- coding: utf-8 -*-
"""
闯关器 —— 把十步一条条跑一遍，跑完自动判「过没过」。

为什么需要它
──────────────────────────────────────────────────────────────────────
这些脚本的输出很长，而且中间夹着**故意失败**的实验：

  · 第 3 步实验 A 就是要让 json.loads 报错（证明"提示词要 JSON"靠不住）
  · 第 4 步实验 D 就是要让参数抽取失败（证明模型不会自己重试）
  · 第 5 步实验 C 就是要让模型死循环（证明刹车有用）
  · 第 6 步实验 D 就是要让写操作被拒（证明只读账号有用）

所以"输出里有 ✗"完全正常，"输出里全是 ✓"反而可能说明你没跑对分支。

人眼从头看到尾，很容易做两件错事：
  ① 把"故意失败"当成"我环境坏了"
  ② 把"崩在中途"当成"跑完了"（尤其第 7 步，前半段输出一大堆）

判据只能是一个：**只有跑到最后一行才会打印的那句话**。
每个脚本末尾都有一句收尾总结（"第 3 步完成""六句话收尾"），跑不到就没有。
这就是本文件里 must 标记的来源 —— 它不是"关键行"，是"终点线"。

怎么用
──────────────────────────────────────────────────────────────────────
PowerShell（Windows 默认终端，变量必须有 $）：
    $PY = "C:/Users/dell/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
    & $PY drill.py --check

Git Bash（变量不能有 $）：
    PY="C:/Users/dell/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
    $PY drill.py --check

参数：
    --check      体检：密钥、数据库、文档、端口，不花钱
    1 / 3 4      跑指定关，跑完自动判分
    all          从头跑到尾（会很慢，第 7、8 关各要一两分钟）
    9 --keep     起服务不关，自己点浏览器
    serve        起第 10 步的产品页（5002）

每次运行的完整输出都存在 drill_logs/stepNN-时分.txt，判不过就打开看。
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
import os
import socket
import subprocess
import sys
import textwrap
import time

BASE = os.path.dirname(os.path.abspath(__file__))
LOGDIR = os.path.join(BASE, "drill_logs")

PY = sys.executable          # 用"当前这个解释器"跑子进程，不猜路径


# ══════════════════════════════════════════════════════════════════════
# 一、十关的定义
# ══════════════════════════════════════════════════════════════════════
#
# must   : 必须出现（终点线）
# forbid : 出现即判不过（真正的异常，不是"实验里的故意失败"）
# need   : 这一关依赖什么前置条件（交给 --check 体检）
# hint   : 没过时最可能的原因
#
class Step:
    def __init__(self, n, name, script, goal, must, forbid=None,
                 need=None, cost="", hint="", stdin=None, kind="script",
                 cwd=None, port=None):
        self.n = n
        self.name = name
        self.script = script
        self.goal = goal
        self.must = must
        self.forbid = forbid or ["Traceback (most recent call last)"]
        self.need = need or []
        self.cost = cost
        self.hint = hint
        self.stdin = stdin
        self.kind = kind            # script / server / tests
        self.cwd = cwd or BASE
        self.port = port


STEPS = {
    1: Step(
        1, "把话说明白",
        "01_hello.py",
        "URL / Key / 模型名三样填对，能收到一句回答",
        must=["── 模型回答 ──", "耗时："],
        forbid=["Traceback (most recent call last)", "✗ HTTP", "✗ 连不上", "✗ 代理"],
        need=["key"],
        cost="1 次请求 · 十几秒",
        hint=textwrap.dedent("""\
            401 → Key 不对，或者粘贴时末尾带了空格（脚本会提示并自动 strip）
            404 → URL 少了 /chat/completions 或者模型名拼错
            405 → 你填的是 OSS 地址（带 oss-），那是存文件的，不是模型接口
            ProxyError → 本机代理软件没开。在 mykey.py 里写 PROXY = "none" 试直连"""),
    ),
    2: Step(
        2, "模型没有记忆",
        "02_multi_turn.py",
        "亲眼看见：独立两次调用它不记得；自己维护 messages 它才记得",
        must=["实验 A：两次独立调用", "实验 B：自己维护 messages", "退出。注意最后几轮"],
        need=["key"],
        cost="约 8 次请求 · 半分钟",
        # 实验 C 是交互式的，闯关器替你说两句话，证明它真的记住了上一句
        stdin="我叫陈嘉岩，正在做医院信息管理系统的项目\n我刚才说我叫什么？\nexit\n",
        hint="卡在「我：」不动 = 输入被吞了，直接跑 python 02_multi_turn.py 手动敲",
    ),
    3: Step(
        3, "让模型回代码能读的东西",
        "03_structured_output.py",
        "「请输出 JSON」是请求不是保证；response_format 才是保证",
        must=["实验 A：只在提示词里写", "实验 B：response_format", "第 3 步完成"],
        # 这一关的 ✗ 是教学内容，不能当失败
        forbid=["Traceback (most recent call last)"],
        need=["key"],
        cost="约 10 次请求 · 一分钟",
        hint="实验 A 打印 JSONDecodeError 是**正常且必需的** —— 那正是这一关要证明的事",
    ),
    4: Step(
        4, "模型只出主意，执行永远是代码",
        "04_tool_calling.py",
        "工具调用返回的不是答案，是「调用申请」；参数会抽错，得在工具侧兜",
        must=["实验 B：给了工具", "实验 D：它能不能自己选对工具", "第 4 步完成"],
        forbid=["Traceback (most recent call last)"],
        need=["key"],
        cost="约 10 次请求 · 一分钟",
        hint="实验 D 里「布洛芬缓释胶囊」被抽成「布洛芬」是**真实现象**，不是 bug；"
             "实验 E 才修好它",
    ),
    5: Step(
        5, "循环 + 刹车",
        "05_agent_loop.py",
        "while 循环靠 finish_reason 停，不靠轮数；四层刹车防跑飞",
        must=["实验 A：一个必须分步查的问题", "实验 C：模型陷入死循环", "第 5 步完成"],
        forbid=["Traceback (most recent call last)"],
        need=["key"],
        cost="约 6 次请求 · 一分钟",
        hint="实验 C 是本地模拟，不花 token；运行十几秒才能看到「被拦住」",
    ),
    6: Step(
        6, "接自己的真库",
        "06_his_agent.py",
        "只读账号 + SQL 写死 + 参数化；实测写操作被数据库拒绝",
        must=["实验 D", "第 6 步完成"],
        forbid=["Traceback (most recent call last)"],
        need=["key", "db"],
        cost="约 8 次请求 + 5 次查库 · 一分半",
        hint=textwrap.dedent("""\
            db_ro.py 不存在 → 从 db_ro.py.example 复制并填密码
            1045 拒绝登录 → 密码错
            1142 → 这个反而是**对的**，说明你用的就是只读账号"""),
    ),
    7: Step(
        7, "文档塞不进 prompt",
        "07_rag_basics.py",
        "先检索再喂；检索好坏要量化，**金标准标法决定结论**",
        must=["六句话收尾"],
        forbid=["Traceback (most recent call last)", "一块都没切出来"],
        need=["key", "kb"],
        cost="约 20 次请求 + 63 段向量（首次）· 一两分钟",
        hint=textwrap.dedent("""\
            _cache/ 里有缓存就直接读，没缓存要调 63 次向量接口，会慢
            实验 F 的命中数在不同数据下会浮动，看的是「向量 vs 关键词」的相对关系，
            不是绝对数字"""),
    ),
    8: Step(
        8, "两个工具合成一个助手",
        "08_rag_agent.py",
        "它自己判断该查库还是查文档；阈值挡不住的那题，靠模型判断力兜住",
        must=["七句话收尾"],
        forbid=["Traceback (most recent call last)"],
        need=["key", "db", "kb"],
        cost="约 15 次请求 · 一两分钟",
        hint="这一关最慢，中间会有一段静默（在跑 Agent 循环），别以为卡死了",
    ),
    9: Step(
        9, "做成能点开的网页",
        "09_web_demo.py",
        "流式输出 + 服务端会话；工具调用时先推后撤（retract）",
        must=["打开浏览器"],
        kind="server",
        port=5001,
        need=["key", "db", "kb"],
        cost="起服务 · 不花钱（只有你提问才花）",
        hint="端口被占 → 先关掉上次没关的那个窗口，或者改脚本里的 PORT",
    ),
    10: Step(
        10, "从演示页变成产品",
        "tests/check_all.py",
        "持久化 / 流式引擎 / HTTP 三层，87 项断言全绿",
        must=["✓ 全部通过"],
        forbid=["Traceback (most recent call last)", "项失败"],
        kind="tests",
        cwd=os.path.join(BASE, "10_product"),
        need=[],
        cost="不花一分钱、不连数据库、不调模型 · 三十秒",
        hint=textwrap.dedent("""\
            有一条 ❌ → 看它写的那行中文，那就是失败原因
            报 imports 错 → 你用的不是装了 flask 的那个 python（看 --check 第一条）"""),
    ),
}

ORDER = list(range(1, 11))


# ══════════════════════════════════════════════════════════════════════
# 二、体检（不花钱，不需要模型）
# ══════════════════════════════════════════════════════════════════════

def _port_free(port):
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) != 0


def check(verbose=True):
    """返回 (是否全部通过, 结果列表)。每项 (级别, 说明)。"""
    rows = []

    def add(level, msg):
        rows.append((level, msg))

    # ① 解释器与依赖
    v = sys.version.split()[0]
    add("OK", f"解释器  Python {v}")
    add("OK", f"路径    {sys.executable}")
    for mod in ("requests", "flask", "pymysql"):
        try:
            __import__(mod)
            add("OK", f"依赖    {mod} 已装")
        except ImportError:
            add("FAIL", f"依赖    {mod} **没装** —— 你用的不是项目那个 python")

    # ② 密钥
    key_ok = False
    kp = os.path.join(BASE, "mykey.py")
    if not os.path.exists(kp):
        add("FAIL", "密钥    mykey.py 不存在（从 mykey.py.example 复制）")
    else:
        try:
            import mykey
            url = getattr(mykey, "API_URL", "")
            model = getattr(mykey, "MODEL", "")
            key = getattr(mykey, "KEY", "") or getattr(mykey, "API_KEY", "")
            if not url or not model or not key:
                add("FAIL", "密钥    三样里缺东西（API_URL / MODEL / KEY）")
            elif key != key.strip():
                add("WARN", "密钥    KEY 末尾有空白字符 —— 脚本会自动 strip，但建议手动清掉")
                key_ok = True
            else:
                add("OK", f"密钥    {model} @ {url.split('//')[-1].split('/')[0]}")
                key_ok = True
        except Exception as e:
            add("FAIL", f"密钥    mykey.py 读不了：{e}")

    # ③ 数据库（只读账号）
    dbfile = os.path.join(BASE, "db_ro.py")
    if not os.path.exists(dbfile):
        add("WARN", "数据库  db_ro.py 不存在 —— 第 6、8 关会用不了")
    else:
        try:
            import db_ro
            import pymysql
            cfg = dict(
                host=db_ro.DB_RO.get("host", "localhost"),
                user=db_ro.DB_RO.get("user"),
                password=db_ro.DB_RO.get("password"),
                database=db_ro.DB_RO.get("database"),
                connect_timeout=4,
            )
            conn = pymysql.connect(**cfg)
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM information_schema.tables "
                            "WHERE table_schema=%s", (cfg["database"],))
                n = cur.fetchone()[0]
            conn.close()
            add("OK", f"数据库  {cfg['user']}@{cfg['host']}/{cfg['database']} 连上了，{n} 张表")
        except Exception as e:
            add("WARN", f"数据库  连不上（第 6、8 关会失败）：{str(e)[:90]}")

    # ④ 知识库文档
    kb = [
        ("hospital_web/README.md", "医院管理系统说明"),
        ("hospital_db_project/数据库设计文档.md", "数据库设计"),
        ("hospital_db_project/README.md", "库项目说明"),
    ]
    miss = []
    total = 0
    for rel, _ in kb:
        p = os.path.join(os.path.dirname(BASE), rel)
        if os.path.exists(p):
            total += os.path.getsize(p)
        else:
            miss.append(rel)
    if miss:
        add("FAIL", f"文档    缺 {len(miss)} 份：{', '.join(miss)}（第 7、8 关要用）")
    else:
        add("OK", f"文档    3 份齐，共 {total:,} 字节")

    # ⑤ 向量缓存
    cd = os.path.join(BASE, "_cache")
    files = [f for f in os.listdir(cd)] if os.path.isdir(cd) else []
    if files:
        add("OK", f"缓存    {len(files)} 个向量缓存（第 7、8 关不用重新算）")
    else:
        add("WARN", "缓存    没有 —— 第 7 关会先调 63 次向量接口，慢一点但没问题")

    # ⑥ 端口
    for port, who in ((5001, "第 9 关"), (5002, "第 10 关产品页")):
        if _port_free(port):
            add("OK", f"端口    {port} 空闲（{who}）")
        else:
            add("WARN", f"端口    {port} 已被占用（{who} 起不来，先关掉旧的）")

    if verbose:
        print("=" * 66)
        print("  体检（不花钱，只查环境和文件）")
        print("=" * 66)
        icon = {"OK": "✅", "WARN": "⚠️ ", "FAIL": "❌"}
        for level, msg in rows:
            print(f"  {icon[level]} {msg}")
        bad = [r for r in rows if r[0] == "FAIL"]
        print("─" * 66)
        if bad:
            print(f"  {len(bad)} 项硬伤，先修掉再往下走。")
        else:
            print("  没有硬伤。可以开跑了。")
        print()

    return (not any(r[0] == "FAIL" for r in rows)), rows


def _need_ok(step):
    """按这一关的 need 做最小体检，不通过就拒绝开跑。"""
    need = set(step.need)
    if "key" in need:
        kp = os.path.join(BASE, "mykey.py")
        if not os.path.exists(kp):
            return False, "缺 mykey.py（从 mykey.py.example 复制并填好）"
    if "db" in need:
        if not os.path.exists(os.path.join(BASE, "db_ro.py")):
            return False, "缺 db_ro.py（从 db_ro.py.example 复制并填好）"
    if "kb" in need:
        for rel in ("hospital_web/README.md", "hospital_db_project/数据库设计文档.md",
                    "hospital_db_project/README.md"):
            if not os.path.exists(os.path.join(os.path.dirname(BASE), rel)):
                return False, f"缺知识库文档 {rel}"
    if step.port and not _port_free(step.port):
        return False, f"端口 {step.port} 被占用"
    return True, ""


# ══════════════════════════════════════════════════════════════════════
# 三、跑一关
# ══════════════════════════════════════════════════════════════════════

def _banner(step, total):
    print()
    print("═" * 66)
    print(f"  第 {step.n} 关 / 共 {total} 关 · {step.name}")
    print(f"  {step.goal}")
    print("─" * 66)
    print(f"  花费： {step.cost}")
    print("  自己跑一遍（PowerShell）：")
    print(f'    cd "{step.cwd}"')
    print(f'    & "{PY}" {step.script}')
    print("═" * 66)
    print()


def _log_path(step):
    os.makedirs(LOGDIR, exist_ok=True)
    return os.path.join(LOGDIR, f"step{step.n:02d}-{time.strftime('%H%M%S')}.txt")


def run_script(step, total):
    """普通脚本：流式打印 + 落日志 + 按标记判分。"""
    _banner(step, total)
    logp = _log_path(step)
    lines = []
    t0 = time.time()

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"

    p = subprocess.Popen(
        [PY, step.script],
        cwd=step.cwd, env=env,
        stdin=subprocess.PIPE if step.stdin else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    try:
        if step.stdin:
            p.stdin.write(step.stdin)
            p.stdin.close()
        for line in p.stdout:
            print(line, end="")
            lines.append(line)
    except KeyboardInterrupt:
        p.kill()
        print("\n  （你按了 Ctrl+C，已中断）")
        return False, "被你中断"
    finally:
        p.wait()

    text = "".join(lines)
    elapsed = time.time() - t0
    with open(logp, "w", encoding="utf-8") as f:
        f.write(text)

    return grade(step, text, p.returncode, elapsed, logp)


def grade(step, text, code, elapsed, logp):
    missing = [m for m in step.must if m not in text]
    bad = [f for f in step.forbid if f in text]

    print()
    print("─" * 66)
    if not missing and not bad and code == 0:
        print(f"  ✅ 第 {step.n} 关通过（{elapsed:.1f} 秒，退出码 {code}）")
        print(f"  日志： {os.path.relpath(logp, BASE)}")
        print()
        print("  合上屏幕，用一句自己的话说清楚这一关在证明什么。说不出来就再来一遍。")
        print("─" * 66)
        return True, ""

    print(f"  ❌ 第 {step.n} 关没过（{elapsed:.1f} 秒，退出码 {code}）")
    if missing:
        print(f"     没到终点线，缺这些标记：")
        for m in missing:
            print(f"       · {m}")
    if bad:
        print(f"     出现真正的异常：")
        for f in bad:
            print(f"       · {f}")
    print(f"     完整日志： {os.path.relpath(logp, BASE)}")
    if step.hint:
        print("     最可能的原因：")
        for ln in step.hint.splitlines():
            print(f"       {ln}")
    print("─" * 66)
    return False, "; ".join(missing + bad)


def run_server(step, total, keep=False):
    """起服务：等到端口通了就判定成功，默认随后关掉。"""
    _banner(step, total)
    logp = _log_path(step)
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"

    logf = open(logp, "w", encoding="utf-8")
    p = subprocess.Popen([PY, step.script], cwd=step.cwd, env=env,
                         stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT)

    ok, waited = False, 0.0
    while waited < 45:
        time.sleep(0.6)
        waited += 0.6
        if p.poll() is not None:
            break
        if not _port_free(step.port):
            ok = True
            break

    time.sleep(1.2)     # 让 boot 的日志写完
    logf.close()
    text = open(logp, encoding="utf-8", errors="replace").read()
    print(text)

    if ok:
        import urllib.request
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{step.port}/api/health", timeout=5) as r:
                body = r.read().decode("utf-8", "replace")
            print(f"  健康检查 GET /api/health → {r.status}  {body[:120]}")
        except Exception as e:
            print(f"  健康检查失败：{str(e)[:100]}")

    print()
    print("─" * 66)
    if ok:
        print(f"  ✅ 第 {step.n} 关通过（{waited:.1f} 秒起来）")
        print(f"  现在打开： http://127.0.0.1:{step.port}")
        if keep:
            print(f"  服务留在前台（PID {p.pid}），Ctrl+C 结束。")
            print("─" * 66)
            try:
                p.wait()
            except KeyboardInterrupt:
                p.kill()
            return True, ""
        p.kill()
        print("  闯关器已经把它关掉了。要自己点： cd ai_lab && "
              f"{os.path.basename(PY)} {step.script}")
        print("─" * 66)
        return True, ""
    if p.poll() is None:
        p.kill()
    print(f"  ❌ 第 {step.n} 关没过（等了 {waited:.0f} 秒端口还没起来）")
    print(f"     日志： {os.path.relpath(logp, BASE)}")
    if step.hint:
        print("     最可能的原因：")
        for ln in step.hint.splitlines():
            print(f"       {ln}")
    print("─" * 66)
    return False, "端口没起来"


def run_tests(step, total):
    return run_script(step, total)


# ══════════════════════════════════════════════════════════════════════
# 四、入口
# ══════════════════════════════════════════════════════════════════════

def run_steps(nums, keep=False):
    total = len(ORDER)
    results = []
    for i, n in enumerate(nums):
        step = STEPS[n]
        ready, why = _need_ok(step)
        if not ready:
            print(f"\n  ⏭  第 {n} 关暂不能跑：{why}")
            results.append((n, step.name, False, "前置条件不满足：" + why))
            continue
        if step.kind == "server":
            okr, why = run_server(step, total, keep=keep)
        else:
            okr, why = run_script(step, total)
        results.append((n, step.name, okr, why))
        if not okr and i < len(nums) - 1:
            print(f"\n  第 {n} 关没过。**先把它修好再往下走** —— 后面的关都建在它上面。")
            break

    # 汇总
    print()
    print("═" * 66)
    print("  本次结果")
    print("═" * 66)
    for n, name, okr, why in results:
        print(f"  {'✅' if okr else '❌'}  第 {n:>2} 关  {name}" + (f"   ← {why[:46]}" if why else ""))
    passed = sum(1 for r in results if r[2])
    print("─" * 66)
    print(f"  {passed} / {len(results)} 关通过")
    print("═" * 66)
    return passed == len(results)


def serve_product():
    """第 10 步的产品页，前台跑。"""
    d = os.path.join(BASE, "10_product")
    print("═" * 66)
    print("  第 10 步 · 产品页")
    print(f"  打开： http://127.0.0.1:5002")
    print("  停止： Ctrl+C")
    print("═" * 66)
    print()
    return subprocess.call([PY, "app.py"], cwd=d)


def run_eval_cli(extra):
    """端到端评测。它不属于"闯关"，是另一件事：
    闯关测「按流程能不能跑通」，评测测「答得对不对」。
    但入口放在一起，省得你记两条路径。
    """
    script = os.path.join(BASE, "10_product", "eval", "run_eval.py")
    if not os.path.exists(script):
        print("✗ 没找到 10_product/eval/run_eval.py")
        return 2
    if not extra:
        print("═" * 66)
        print("  端到端评测 · 38 题，看它到底答得对不对")
        print("═" * 66)
        print()
        print("  常用参数：")
        print("    --truth        只打印真值，不调模型、不花钱")
        print("    --limit 2      先跑两题确认链路")
        print("    --cat 查文档    只跑某一类")
        print("    --only A01,F01 指定题号")
        print("    --tag 名字      给这次跑分打标签，便于 compare 对比")
        print()
        print("  跑完看 reports/ 下的 .md；对比多轮用 eval/compare.py")
        print()
        return 0
    return subprocess.call([PY, script] + list(extra))


def main():
    # eval 分支必须在 argparse 之前处理：它后面跟的是评测脚本自己的参数
    # （--truth / --limit / --cat …），drill 的解析器不认识，先解析就会报错。
    if len(sys.argv) > 1 and sys.argv[1] == "eval":
        return run_eval_cli(sys.argv[2:])

    ap = argparse.ArgumentParser(
        description="ai_lab 十关闯关器：跑一关、判一关",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            例：
              python drill.py --check      先体检（不花钱）
              python drill.py 1            只跑第 1 关
              python drill.py 3 4          跑第 3、4 关
              python drill.py all          从第 1 关跑到第 10 关
              python drill.py 9 --keep     起网页但不关
              python drill.py serve        起第 10 步的产品页
              python drill.py eval --limit 2   端到端评测（答得对不对，不是跑不跑得通）
            """))
    ap.add_argument("target", nargs="?", help="关号、多个关号，或 all / serve / eval")
    ap.add_argument("more", nargs="*", help="更多关号；eval 后面直接跟评测参数")
    ap.add_argument("--check", action="store_true", help="只体检，不跑脚本")
    ap.add_argument("--keep", action="store_true", help="服务类关卡不自动关闭")
    ap.add_argument("--force", action="store_true", help="体检有硬伤也照跑")
    a = ap.parse_args()

    print()
    print("  ai_lab 闯关器")
    print(f"  工作目录 {BASE}")
    print(f"  解释器   {PY}")
    print()

    if a.check or not a.target:
        okall, _ = check()
        if not a.check:
            print("  下一步： python drill.py 1")
        return 0 if okall else 1

    if a.target == "serve":
        return serve_product()

    if a.target == "all":
        okall, _ = check()
        if not okall and not a.force:
            print("  体检有硬伤，先修。确实要硬跑加 --force。")
            return 1
        nums = ORDER
    else:
        raw = [a.target] + a.more
        nums = []
        for x in raw:
            if not x.isdigit() or int(x) not in STEPS:
                print(f"  没有第 {x} 关。可选：1-10 / all / serve / eval")
                return 2
            nums.append(int(x))

    return 0 if run_steps(nums, keep=a.keep) else 1


if __name__ == "__main__":
    sys.exit(main())
