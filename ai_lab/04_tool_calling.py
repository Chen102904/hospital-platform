# -*- coding: utf-8 -*-
"""
第 4 步：工具调用（function calling）—— Agent 最反直觉的一步。

═══════════════════════════════════════════════════════════════
先记住这句，本步所有代码都是为了证明它：
    模型**从来没有**执行过任何函数。
    它只是输出了一段文本，说："我想调用 get_schedule_slots，参数是 {department:'内科'}"。
    真正去查数据库的，是下面那个 dispatch() 里的 Python 代码。
═══════════════════════════════════════════════════════════════

它解决什么问题：
    模型不知道今天内科还剩几个号，也不知道你库里有几个患者。
    硬问它，它只能瞎编（实验 A 会演给你看）。
    所以流程变成：模型说"我要查" -> **你的代码去查** -> 结果喂回给它 -> 它用人话回答。

四个实验：
    A  不给工具，直接问 -> 它编 / 它推脱
    B  给工具，看它返回什么 -> content 是空的，意图在 tool_calls 里
    C  完整跑一轮：模型要数据 -> 代码查 -> 数据喂回 -> 模型回答
    D  换一个工具、换个问法，看它能不能自己选对工具、抽对参数

跑法（在 ai_lab 目录下）：
    C:/Users/dell/.workbuddy/binaries/python/envs/default/Scripts/python.exe 04_tool_calling.py
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
# 一、先写"真活"——你项目里本来就有的查询
#    ⚠️ 这里用假数据，是为了不依赖数据库就能跑。
#       第 6 步会把这些函数换成真的 SQL。
# ══════════════════════════════════════════════════════════
SCHEDULE_DB = {
    "内科": [("张建国", "主任医师", 30, 18), ("李慧敏", "副主任医师", 25, 25)],
    "外科": [("王海涛", "主任医师", 20, 6)],
    "儿科": [("陈晓燕", "主治医师", 40, 40)],
    "骨科": [("刘志强", "副主任医师", 25, 3)],
}

STOCK_DB = {
    "布洛芬缓释胶囊": {"stock": 0, "unit": "盒", "status": "停用"},
    "奥美拉唑肠溶胶囊": {"stock": 128, "unit": "盒", "status": "在用"},
    "阿莫西林胶囊": {"stock": 6, "unit": "盒", "status": "在用"},
}

# 实验 D 关掉（严格匹配，会失败）；实验 E 打开（模糊匹配，能救回来）
FUZZY = False


def get_schedule_slots(department):
    """查某科室今天各医生的剩余号源。"""
    rows = SCHEDULE_DB.get(department)
    if not rows:
        return {"ok": False, "reason": f"没有找到科室「{department}」",
                "known_departments": list(SCHEDULE_DB)}
    return {
        "ok": True,
        "department": department,
        "doctors": [
            {"doctor": n, "title": t, "total": total, "booked": booked,
             "left": total - booked}
            for n, t, total, booked in rows
        ],
    }


def get_medicine_stock(medicine_name):
    """查某个药品的库存。

    ⚠️ 实验 D 会暴露一个真实问题：模型会把"布洛芬缓释胶囊"抽成"布洛芬"，
    把"阿莫西林胶囊"抽成"阿莫西林"——它觉得核心词就够了。
    严格匹配就查不到，然后它不会自己重试，而是跑来问你"是不是想问XX"。

    FUZZY 开关用来对比「严格匹配」和「模糊匹配」的差别（实验 E）。
    """
    name = (medicine_name or "").strip()
    info = STOCK_DB.get(name)
    matched_by = "精确匹配"

    if info is None and FUZZY:
        # 工具侧的容错：短名能匹配到长名，长名也能匹配到短名
        hits = [k for k in STOCK_DB if name and (name in k or k in name)]
        if len(hits) == 1:
            name, info, matched_by = hits[0], STOCK_DB[hits[0]], "模糊匹配"
        elif len(hits) > 1:
            return {"ok": False, "reason": f"「{medicine_name}」匹配到多个药品，请指定全名",
                    "candidates": hits}

    if info is None:
        return {"ok": False, "reason": f"药品目录里没有「{medicine_name}」",
                "known_medicines": list(STOCK_DB),
                "hint": "请使用药品全名，例如 布洛芬缓释胶囊"}
    return {"ok": True, "medicine": name, "matched_by": matched_by, **info}


# ══════════════════════════════════════════════════════════
# 二、把"函数"翻译成"模型能读的说明书"（tools 参数）
#    ⚠️ description 写得好不好，直接决定它能不能选对、能不能抽对参数。
#       这不是注释，这是给模型的接口文档。
# ══════════════════════════════════════════════════════════
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_schedule_slots",
            "description": "查询某个科室今天各医生的号源总数、已挂数量和剩余数量。"
                           "当用户询问某科室今天有没有号、能不能挂上、某医生还有没有号时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "department": {
                        "type": "string",
                        "description": "科室名称，只要科室名，例如 内科 / 外科 / 儿科 / 骨科",
                    }
                },
                "required": ["department"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_medicine_stock",
            "description": "查询某个药品的当前库存数量、单位和状态（在用/停用）。"
                           "当用户询问药还有多少、会不会断货、某个药是否还能开时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "medicine_name": {
                        "type": "string",
                        "description": "药品名称，例如 布洛芬缓释胶囊 / 奥美拉唑肠溶胶囊",
                    }
                },
                "required": ["medicine_name"],
                "additionalProperties": False,
            },
        },
    },
]


# ══════════════════════════════════════════════════════════
# 三、路由表：模型说"调 X"，代码去查表找 X 对应的函数
#    ⚠️ 关键安全点：模型只能从你注册过的名字里选，选不到表里没有的东西。
#       这就是为什么"别让模型直接生成函数名"是不成立的——它只能选。
# ══════════════════════════════════════════════════════════
REGISTRY = {
    "get_schedule_slots": get_schedule_slots,
    "get_medicine_stock": get_medicine_stock,
}


def dispatch(name, arguments_json):
    """模型给的是函数名 + 一段 JSON 字符串，这里负责真的执行。"""
    func = REGISTRY.get(name)
    if func is None:
        return {"ok": False, "reason": f"没有这个工具：{name}"}
    try:
        args = json.loads(arguments_json or "{}")
    except json.JSONDecodeError as e:
        # 模型偶尔会吐坏 JSON，这里必须兜住，不能让它带崩整个程序
        return {"ok": False, "reason": f"参数不是合法 JSON：{e}"}
    try:
        return func(**args)
    except TypeError as e:
        return {"ok": False, "reason": f"参数不匹配：{e}"}
    except Exception as e:
        return {"ok": False, "reason": f"工具执行出错：{type(e).__name__}: {e}"}


# ══════════════════════════════════════════════════════════
# 四、调用层
# ══════════════════════════════════════════════════════════
def post(messages, tools=None, timeout=120):
    body = {"model": MODEL, "messages": messages}
    if tools:
        body["tools"] = tools
    resp = requests.post(
        API_URL,
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        json=body,
        timeout=timeout,
        proxies=build_proxies(),
    )
    if resp.status_code != 200:
        print(f"  ✗ HTTP {resp.status_code}: {resp.text[:400]}")
        return None
    return resp.json()["choices"][0]


SYSTEM = ("你是一家医院信息系统的助手。涉及号源、库存这类实时数据，"
          "必须调用工具查询，不允许凭印象猜测。回答简短、口语化。")


# ══════════════════════════════════════════════════════════
# 实验 A：不给工具 —— 它只能编或者推脱
# ══════════════════════════════════════════════════════════
def exp_a():
    print("=" * 68)
    print("实验 A：不给工具，直接问实时数据")
    print("=" * 68)
    ch = post([{"role": "system", "content": SYSTEM},
               {"role": "user", "content": "内科今天还有号吗？"}])
    if not ch:
        return
    print(f"\nfinish_reason: {ch.get('finish_reason')}")
    print("模型回答：", (ch["message"].get("content") or "(空)")[:300])
    print("\n↑ 它要么编一个数字，要么说'我查不到'。两种都没用。\n")


# ══════════════════════════════════════════════════════════
# 实验 B：给工具，看它到底返回什么
# ══════════════════════════════════════════════════════════
def exp_b():
    print("=" * 68)
    print("实验 B：给了工具，它返回的不是答案，是“调用申请”")
    print("=" * 68)
    ch = post([{"role": "system", "content": SYSTEM},
               {"role": "user", "content": "内科今天还有号吗？"}], tools=TOOLS)
    if not ch:
        return
    msg = ch["message"]
    print(f"\nfinish_reason: {ch.get('finish_reason')}   ← 注意这个值不再是 stop")
    print("content     :", repr(msg.get("content")), "  ← 空的！它什么都没回答")
    print("tool_calls  :")
    print(json.dumps(msg.get("tool_calls"), ensure_ascii=False, indent=2))

    print("\n" + "-" * 68)
    print("三个必须看清的细节：")
    print("  1. content 是空字符串 —— 它没回答问题，只是提了个申请")
    print("  2. arguments 是**字符串**，不是 dict —— 得自己 json.loads")
    print("  3. 有个 id（call_xxx）—— 待会儿把结果还回去时必须带这个 id，")
    print("     模型靠它知道“这条结果对应我刚才哪个申请”")
    print("-" * 68 + "\n")


# ══════════════════════════════════════════════════════════
# 实验 C：完整跑一轮 —— 这是 Agent 的雏形
# ══════════════════════════════════════════════════════════
def ask(question, verbose=True):
    """一轮完整的工具调用。注意：这里只循环一次，真正的 Agent 循环在第 5 步。"""
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": question},
    ]

    ch = post(messages, tools=TOOLS)
    if not ch:
        return None
    msg = ch["message"]

    # 情况 1：模型觉得不用查，直接回答了
    if ch.get("finish_reason") != "tool_calls":
        if verbose:
            print("（模型没调用工具，直接回答）")
            print(msg.get("content"))
        return msg.get("content")

    # 情况 2：模型要调工具 —— 先把它的"申请"原样记进历史
    messages.append({
        "role": "assistant",
        "content": msg.get("content") or "",
        "tool_calls": msg["tool_calls"],          # 必须带上，否则下一轮对不上
    })

    # 逐个执行，把结果以 role="tool" 塞回去
    for tc in msg["tool_calls"]:
        name = tc["function"]["name"]
        args_str = tc["function"]["arguments"]
        if verbose:
            print(f"  → 模型申请调用：{name}({args_str})")

        result = dispatch(name, args_str)
        if verbose:
            print(f"  ← 你的代码返回：{json.dumps(result, ensure_ascii=False)}")

        messages.append({
            "role": "tool",
            "tool_call_id": tc["id"],             # 这一行少写就会 400
            "content": json.dumps(result, ensure_ascii=False),
        })

    # 第二次请求：这次模型手里有真实数据了
    ch2 = post(messages, tools=TOOLS)
    if not ch2:
        return None
    final = ch2["message"].get("content") or ""
    if verbose:
        print(f"  ★ 模型最终回答：{final}")
    return final


def exp_c():
    print("=" * 68)
    print("实验 C：完整跑一轮（模型要 → 代码查 → 数据喂回 → 模型回答）")
    print("=" * 68)
    print("\n用户问：内科今天还有号吗？")
    ask("内科今天还有号吗？")
    print()


# ══════════════════════════════════════════════════════════
# 实验 D：换个工具、换个问法 —— 看它能不能自己选对
# ══════════════════════════════════════════════════════════
def exp_d():
    print("=" * 68)
    print("实验 D：它能不能自己选对工具、抽对参数")
    print("=" * 68)
    for q in [
        "骨科的号还剩多少？",                       # 只有 3 个，看它会不会算出"快满了"
        "布洛芬还有货吗？",                         # 库存 0 且已停用，看它会不会说"开不了"
        "阿莫西林库存紧张吗？",                     # 6 盒，看它判断
        "今天天气怎么样？",                         # 完全无关，看它会不会乱调工具
    ]:
        print(f"\n用户问：{q}")
        ask(q)

    print("\n" + "-" * 68)
    print("↑ 注意上面两次药品查询**都失败了**：")
    print("   模型把「布洛芬缓释胶囊」抽成「布洛芬」")
    print("   把「阿莫西林胶囊」抽成「阿莫西林」")
    print("   —— 它觉得核心词就够了，剂型后缀随手扔掉。")
    print("   而且它不会自己改口重试，反而回头问你“是不是想问XX”——")
    print("   对话能继续，但用户多绕了一圈。这在真实系统里就是一次失败的查询。")
    print("-" * 68 + "\n")


# ══════════════════════════════════════════════════════════
# 实验 E：工具侧加容错，同一个问题就通了
# ══════════════════════════════════════════════════════════
def exp_e():
    global FUZZY
    FUZZY = True
    print("=" * 68)
    print("实验 E：同样的问题，工具侧加上模糊匹配（FUZZY = True）")
    print("=" * 68)
    for q in ["布洛芬还有货吗？", "阿莫西林库存紧张吗？"]:
        print(f"\n用户问：{q}")
        ask(q)
    FUZZY = False

    print("\n" + "-" * 68)
    print("结论（这条在工程上非常值钱）：")
    print("  **模型抽参数不准，是常态，不是 bug。**")
    print("  你改不动模型，但可以把自己的工具写皮实：")
    print("    · 字符串做模糊匹配 / 别名表（布洛芬 -> 布洛芬缓释胶囊）")
    print("    · 科室名做同义词映射（心内科 / 心血管内科 -> 内科）")
    print("    · 查不到时把**候选清单**一起返回，模型就知道该怎么改口")
    print("  凡是让模型去猜的环节，都要假设它会猜错。")
    print("-" * 68 + "\n")


if __name__ == "__main__":
    print(f"模型：{MODEL}\n")
    exp_a()
    exp_b()
    exp_c()
    exp_d()
    exp_e()
    print("=" * 68)
    print("第 4 步完成。一句话总结：")
    print("  模型只负责“决定查什么、用什么参数”，执行永远在你的代码里。")
    print("  所以：工具清单是你给的（它选不出你没注册的东西），")
    print("        参数要校验，结果要兜错，权限卡在你手里。")
    print("=" * 68)
