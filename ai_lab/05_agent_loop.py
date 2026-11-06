# -*- coding: utf-8 -*-
"""
第 5 步：Agent 循环 —— 从"能调一次工具"到"能自己干完一件事"。

第 4 步的 ask() 只循环一次：模型要一次数据 -> 查 -> 回答。
真实任务经常要**好几步**：
    "今天哪个科室号最紧张？那个科室常用药还够吗？"
    第 1 轮：模型先要知道各科室情况     -> 调 list_all_departments
    第 2 轮：拿到结果，知道是骨科最紧张 -> 调 get_dept_drugs("骨科")
    第 3 轮：两次结果都有了             -> 组织自然语言回答

所以第 4 步的那段代码外面再套一个 while，就是 Agent 了。
但套 while 会立刻冒出三个新问题，本脚本就是逐个解决它们：

    ① 什么时候停？        -> 靠 finish_reason，不靠数轮数
    ② 死循环怎么办？      -> repeat_limit 拦重复 + max_blocks 判卡死（实验 C 会真的演给你看）
    ③ 跑飞了怎么办？      -> max_rounds / max_seconds / max_tokens 三重上限

四个实验：
    A  完整多轮循环：一个问题自动跑 2 轮工具 + 1 轮收尾
    B  轮次上限被人为卡到 1：看它怎么优雅收尾而不是崩
    C  模拟一个失控的模型（不发 API）：看刹车怎么在 3 轮内拉住它
    D  把 trace 打出来：每一轮调了什么、花了多少 token、多久

跑法（在 ai_lab 目录下）：
    C:/Users/dell/.workbuddy/binaries/python/envs/default/Scripts/python.exe 05_agent_loop.py
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
import sys
import time

import requests

# 这里原本有一行 sys.stdout = io.TextIOWrapper(...)，已删除。原因：每 import
# 一次就新建一层包装，包第二次时前一层被回收、析构时把底层 buffer 关掉，
# 之后所有 print 都报 I/O operation on closed file。上面那段 reconfigure 已够用。

try:
    from mykey import API_KEY, API_URL, MODEL
except ImportError:
    sys.exit("✗ 没找到 mykey.py。先把 mykey.py.example 复制成 mykey.py，再填值。")

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


# ══════════════════════════════════════════════════════════
# 一、数据与工具（假数据，第 6 步换成真 SQL）
# ══════════════════════════════════════════════════════════
SCHEDULE_DB = {
    "内科": [("张建国", "主任医师", 30, 18), ("李慧敏", "副主任医师", 25, 25)],
    "外科": [("王海涛", "主任医师", 20, 6)],
    "儿科": [("陈晓燕", "主治医师", 40, 40)],
    "骨科": [("刘志强", "副主任医师", 25, 24)],
    "眼科": [("赵敏", "主治医师", 30, 9)],
}

DEPT_DRUGS = {
    "内科": ["奥美拉唑肠溶胶囊", "阿莫西林胶囊"],
    "外科": ["阿莫西林胶囊"],
    "儿科": ["布洛芬缓释胶囊"],
    "骨科": ["布洛芬缓释胶囊", "奥美拉唑肠溶胶囊"],
    "眼科": ["阿莫西林胶囊"],
}

STOCK_DB = {
    "布洛芬缓释胶囊": {"stock": 0, "unit": "盒", "status": "停用"},
    "奥美拉唑肠溶胶囊": {"stock": 128, "unit": "盒", "status": "在用"},
    "阿莫西林胶囊": {"stock": 6, "unit": "盒", "status": "在用"},
}


def list_all_departments():
    """所有科室今天的号源紧张程度，按剩余率从低到高排。"""
    out = []
    for dept, rows in SCHEDULE_DB.items():
        total = sum(r[2] for r in rows)
        booked = sum(r[3] for r in rows)
        left = total - booked
        out.append({
            "department": dept,
            "total": total,
            "booked": booked,
            "left": left,
            "left_rate": round(left / total, 3) if total else 0.0,
        })
    out.sort(key=lambda x: x["left_rate"])
    return {"ok": True, "departments": out}


def get_schedule_slots(department):
    rows = SCHEDULE_DB.get(department)
    if not rows:
        return {"ok": False, "reason": f"没有找到科室「{department}」",
                "known_departments": list(SCHEDULE_DB)}
    return {"ok": True, "department": department, "doctors": [
        {"doctor": n, "title": t, "total": tot, "booked": b, "left": tot - b}
        for n, t, tot, b in rows]}


def get_dept_drugs(department):
    """查某科室常用药及其库存。"""
    names = DEPT_DRUGS.get(department)
    if names is None:
        return {"ok": False, "reason": f"没有找到科室「{department}」",
                "known_departments": list(DEPT_DRUGS)}
    return {"ok": True, "department": department, "drugs": [
        {"medicine": n, **STOCK_DB.get(n, {"stock": None, "status": "目录无此药"})}
        for n in names]}


TOOLS = [
    {"type": "function", "function": {
        "name": "list_all_departments",
        "description": "列出所有科室今天的号源情况（总数/已挂/剩余/剩余率），"
                       "已按剩余率从低到高排序。当用户问哪个科室最紧张、整体号源情况时先调这个。",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    }},
    {"type": "function", "function": {
        "name": "get_schedule_slots",
        "description": "查询**某一个指定科室**今天各医生的号源明细。用户已经点名科室时用这个。",
        "parameters": {
            "type": "object",
            "properties": {"department": {"type": "string", "description": "科室名称，如 内科 / 骨科"}},
            "required": ["department"], "additionalProperties": False,
        },
    }},
    {"type": "function", "function": {
        "name": "get_dept_drugs",
        "description": "查询**某一个指定科室**的常用药清单及各药当前库存和状态。",
        "parameters": {
            "type": "object",
            "properties": {"department": {"type": "string", "description": "科室名称，如 内科 / 骨科"}},
            "required": ["department"], "additionalProperties": False,
        },
    }},
]

REGISTRY = {
    "list_all_departments": list_all_departments,
    "get_schedule_slots": get_schedule_slots,
    "get_dept_drugs": get_dept_drugs,
}


def dispatch(name, arguments_json):
    func = REGISTRY.get(name)
    if func is None:
        return {"ok": False, "reason": f"没有这个工具：{name}"}
    try:
        args = json.loads(arguments_json or "{}")
    except json.JSONDecodeError as e:
        return {"ok": False, "reason": f"参数不是合法 JSON：{e}"}
    try:
        return func(**args)
    except Exception as e:
        return {"ok": False, "reason": f"工具执行出错：{type(e).__name__}: {e}"}


# ══════════════════════════════════════════════════════════
# 二、模型调用（返回统一结构，方便实验 C 换成"假模型"）
# ══════════════════════════════════════════════════════════
def real_model(messages, tools=None, timeout=120):
    """返回 {"message":..., "finish_reason":..., "usage":..., "elapsed":...}"""
    body = {"model": MODEL, "messages": messages}
    if tools:
        body["tools"] = tools
    t0 = time.time()
    resp = requests.post(
        API_URL,
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        json=body, timeout=timeout, proxies=build_proxies(),
    )
    elapsed = time.time() - t0
    if resp.status_code != 200:
        print(f"  ✗ HTTP {resp.status_code}: {resp.text[:300]}")
        return None
    ch = resp.json()["choices"][0]
    return {
        "message": ch["message"],
        "finish_reason": ch.get("finish_reason"),
        "usage": resp.json().get("usage") or {},
        "elapsed": elapsed,
    }


SYSTEM = ("你是一家医院信息系统的助手。涉及号源、库存这类实时数据必须调用工具查询，"
          "不允许凭印象猜测。多步问题就分步查，查完再回答。回答简短、口语化、直接给结论。")


# ══════════════════════════════════════════════════════════
# 三、★ 核心：Agent 循环 ★
# ══════════════════════════════════════════════════════════
def run_agent(question, max_rounds=8, max_seconds=180, max_tokens=40000,
              repeat_limit=2, max_blocks=3, model_fn=None, verbose=True):
    """一个带刹车的 Agent 循环。

    参数都是刹车：
        max_rounds   最多问模型几轮（防止它没完没了要工具）
        max_seconds  总耗时上限
        max_tokens   累计 token 上限（钱）
        repeat_limit 同一个工具 + 同样的参数，最多允许真的执行几次
        max_blocks   被拦截累计超过这个次数，直接判定卡死退出（防止空转烧钱）
    """
    model_fn = model_fn or real_model

    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": question},
    ]
    trace = []          # 每一步都记下来，出问题好复盘
    seen = {}           # (工具名, 参数) -> 执行次数
    blocked_total = 0
    t_start = time.time()
    tokens_total = 0

    def finish(answer, status, reason=""):
        if verbose:
            print(f"\n  ★ 最终回答（{status}）：{answer}")
            if reason:
                print(f"    （{reason}）")
        return {"answer": answer, "status": status, "trace": trace,
                "elapsed": round(time.time() - t_start, 1), "tokens": tokens_total}

    for round_no in range(1, max_rounds + 1):
        # ── 刹车 1：耗时 ──
        if time.time() - t_start > max_seconds:
            return finish("任务超时，已中止。", "timeout",
                          f"超过 {max_seconds} 秒")

        # ── 刹车 2：token 预算 ──
        if tokens_total > max_tokens:
            return finish("已超出 token 预算，中止。", "budget",
                          f"累计 {tokens_total} tokens")

        out = model_fn(messages, TOOLS)
        if out is None:
            return finish("模型调用失败。", "error", "见上方 HTTP 报错")

        msg = out["message"]
        fr = out["finish_reason"]
        usage = out.get("usage") or {}
        tokens_total += usage.get("total_tokens") or 0

        if verbose:
            print(f"\n  ── 第 {round_no} 轮 ── finish_reason={fr} "
                  f"({out.get('elapsed', 0):.1f}s, {usage.get('total_tokens', '?')} tokens)")

        # ── ★ 停止条件：它不再要工具，就说明它说完了 ★ ──
        #    注意：绝不能用"轮数到了"当停止条件，那只是兜底
        if fr != "tool_calls":
            answer = msg.get("content") or ""
            trace.append({"round": round_no, "action": "answer",
                          "tokens": usage.get("total_tokens")})
            return finish(answer, "ok")

        # 模型要调工具：先把它的申请原样记进历史
        messages.append({
            "role": "assistant",
            "content": msg.get("content") or "",
            "tool_calls": msg["tool_calls"],
        })

        for tc in msg["tool_calls"]:
            name = tc["function"]["name"]
            args_str = tc["function"]["arguments"] or "{}"

            # 参数规范化，用来判断"是不是在重复做同一件事"
            try:
                key = (name, json.dumps(json.loads(args_str), sort_keys=True, ensure_ascii=False))
            except json.JSONDecodeError:
                key = (name, args_str)
            seen[key] = seen.get(key, 0) + 1

            # ── 刹车 3：重复调用 ──
            is_blocked = seen[key] > repeat_limit
            if is_blocked:
                blocked_total += 1
                result = {"ok": False,
                          "reason": f"这个调用已经重复 {seen[key]} 次了，结果不会变。"
                                    f"请换一个思路，或用现有信息直接回答用户。"}
                if verbose:
                    print(f"    ⛔ 拦截重复调用（第 {blocked_total} 次拦截）：{name}({args_str})")
            else:
                result = dispatch(name, args_str)
                if verbose:
                    print(f"    → {name}({args_str})")
                    print(f"      {(json.dumps(result, ensure_ascii=False))[:180]}")

            trace.append({"round": round_no, "tool": name, "args": args_str,
                          "ok": result.get("ok"), "blocked": is_blocked,
                          "tokens": usage.get("total_tokens")})

            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": json.dumps(result, ensure_ascii=False),
            })

        # ── 刹车 4：卡死判定 ──
        #    光拦不执行还不够 —— 它会被拦、被劝、还是继续要同一个工具，
        #    那就等于白转轮次、白烧 token。累计拦够次数就直接收手。
        if blocked_total >= max_blocks:
            return finish("这个问题我反复查同一份数据也没法得出结果，先停下。",
                          "stuck",
                          f"同一调用被拦截 {blocked_total} 次，判定卡死")

    # 兜底：轮数用完了还没停
    return finish("已经查了很多轮还没得出答案，我先停下。", "max_rounds",
                  f"达到 {max_rounds} 轮上限")


# ══════════════════════════════════════════════════════════
# 实验 A：真实多轮
# ══════════════════════════════════════════════════════════
def exp_a():
    print("=" * 68)
    print("实验 A：一个必须分步查的问题")
    print("=" * 68)
    q = "今天哪个科室的号最紧张？那个科室的常用药还够吗？"
    print(f"\n用户问：{q}")
    r = run_agent(q)
    calls = [t for t in r["trace"] if t.get("tool")]
    print(f"\n（跑了 {len(calls)} 次工具，共 {r['elapsed']} 秒，{r['tokens']} tokens）")

    if len(calls) >= 2:
        try:
            dept = json.loads(calls[1]["args"]).get("department", "?")
        except json.JSONDecodeError:
            dept = "?"
        print("\n↑ 看这个先后顺序：")
        print(f"  第 1 轮它不知道哪个科室最紧张，只能先查全量 -> {calls[0]['tool']}")
        print(f"  拿到结果后，它自己决定去查「{dept}」的药 -> {calls[1]['tool']}")
        print("  **这两步的先后是模型自己排的，你没在代码里写死。** 这才是 Agent 和脚本的区别。")
    print()
    return r


# ══════════════════════════════════════════════════════════
# 实验 B：人为把轮次上限卡到 1
# ══════════════════════════════════════════════════════════
def exp_b():
    print("=" * 68)
    print("实验 B：把 max_rounds 卡到 1，看它怎么收尾")
    print("=" * 68)
    q = "今天哪个科室的号最紧张？那个科室的常用药还够吗？"
    print(f"\n用户问：{q}（max_rounds=1）")
    r = run_agent(q, max_rounds=1)
    print(f"\n返回的 status = {r['status']!r}  ← 程序能区分“答完了”和“被叫停”")
    print("\n↑ 上限被卡住时，不能崩、也不能假装答完，必须给一个明确的 status，")
    print("  上层代码才能决定是提示用户、还是重试、还是降级。\n")


# ══════════════════════════════════════════════════════════
# 实验 C：模拟一个失控的模型（不发 API 请求，纯本地）
# ══════════════════════════════════════════════════════════
def runaway_model(messages, tools=None, timeout=0):
    """一个"疯了"的模型：不管你说什么，永远要求调同一个工具、同样参数。

    这不是瞎编——真实世界里模型陷入循环是常见故障
    （工具报错信息不明确、或者工具总返回空，它就一遍遍重试）。
    这里用假模型把这个场景稳定复现出来，不浪费你的 token。
    """
    n = sum(1 for m in messages if m.get("role") == "tool")
    time.sleep(0.05)                      # 假装有网络耗时
    return {
        "message": {
            "role": "assistant", "content": "",
            "tool_calls": [{
                "id": f"call_mock_{n}",
                "type": "function",
                "function": {"name": "get_schedule_slots",
                             "arguments": '{"department": "内科"}'},
            }],
        },
        "finish_reason": "tool_calls",
        "usage": {"total_tokens": 100},
        "elapsed": 0.05,
    }


def exp_c():
    print("=" * 68)
    print("实验 C：模型陷入死循环，看刹车怎么拉住它（本地模拟，不花 token）")
    print("=" * 68)
    print("\n场景：模型反复要求查同一个科室，永远不给出答案。")
    r = run_agent("随便问点什么", model_fn=runaway_model,
                  max_rounds=10, repeat_limit=2, max_blocks=3)
    blocked = [t for t in r["trace"] if t.get("blocked")]
    executed = [t for t in r["trace"] if t.get("tool") and not t.get("blocked")]
    print(f"\n工具调用共 {len(r['trace'])} 次：真的执行 {len(executed)} 次，"
          f"被拦截 {len(blocked)} 次")
    print(f"返回值 status = {r['status']!r}，总 token = {r['tokens']}，"
          f"只用了 {r['elapsed']} 秒")
    print("\n↑ 四层刹车依次生效：")
    print("   第 1、2 轮真的执行了 —— 允许重试，因为可能真是临时故障")
    print("   第 3 轮起被 repeat_limit 拦下，并把'别再重复了，用现有信息直接答'告诉模型")
    print("   它不听话，累计拦够 max_blocks=3 次 -> 判定卡死，立刻退出")
    print(f"   —— 于是 5 轮（2 次执行 + 3 次拦截）就收手了。")
    print(f"   如果只有 max_rounds 兜底，会一路转到 {10} 轮，多烧一倍 token，还白等。")


# ══════════════════════════════════════════════════════════
# 实验 D：把 trace 打成人能看的表
# ══════════════════════════════════════════════════════════
def exp_d(result):
    print("=" * 68)
    print("实验 D：trace 复盘表（线上出问题时，靠这个定位）")
    print("=" * 68)
    print(f"\n{'轮':<4}{'动作':<26}{'结果':<10}{'token':<8}")
    print("-" * 60)
    for t in result["trace"]:
        if t.get("action") == "answer":
            print(f"{t['round']:<4}{'回答':<26}{'—':<10}{t.get('tokens') or '':<8}")
        else:
            act = f"{t['tool']}({t['args']})"
            act = act if len(act) <= 24 else act[:23] + "…"
            flag = "被拦截" if t.get("blocked") else ("成功" if t.get("ok") else "失败")
            print(f"{t['round']:<4}{act:<26}{flag:<10}{t.get('tokens') or '':<8}")
    print("-" * 60)
    print(f"总计：{result['elapsed']} 秒，{result['tokens']} tokens，"
          f"status={result['status']}")


if __name__ == "__main__":
    print(f"模型：{MODEL}\n")
    r = exp_a()
    exp_b()
    exp_c()
    exp_d(r)
    print("\n" + "=" * 68)
    print("第 5 步完成。Agent 循环的全部秘密就这几行：")
    print()
    print("    while 轮数没用完:")
    print("        让模型看一眼历史")
    print("        if 它不要工具了: 返回它的回答; break")
    print("        它要的工具 -> 你执行 -> 结果塞回历史")
    print()
    print("  加上四层刹车（轮次/耗时/重复/卡死），再加一份 trace 日志。")
    print("  剩下的所谓“Agent 框架”，都是在包这几十行。")
    print("=" * 68)
