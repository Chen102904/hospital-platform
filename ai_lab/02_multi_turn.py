# -*- coding: utf-8 -*-
"""
第 2 步：多轮对话 —— 搞懂 messages 列表怎么长。

核心认知（这一段看懂了，后面 tool calling 就顺了）：
    HTTP 是无状态的，模型**不记得**你上一句说了什么。
    "多轮对话"是假象：每一轮你都要把**完整的聊天记录**重新发一遍。
    所以越长越贵，也所以有"上下文窗口"这个上限。

跑法（在 ai_lab 目录下）：
    C:/Users/dell/.workbuddy/binaries/python/envs/default/Scripts/python.exe 02_multi_turn.py
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


def call(messages, timeout=90):
    """和 01 完全一样的调用，只是 messages 不再写死一条，而是外面传进来。

    返回 (回答文本, usage 字典)，失败返回 (None, None)。
    """
    resp = requests.post(
        API_URL,
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        json={"model": MODEL, "messages": messages},
        timeout=timeout,
        proxies=build_proxies(),
    )
    if resp.status_code != 200:
        print(f"  ✗ HTTP {resp.status_code}: {resp.text[:300]}")
        return None, None
    data = resp.json()
    return data["choices"][0]["message"]["content"], data.get("usage") or {}


# ─────────────────────────────────────────────────────────
# 实验 A：为什么说模型没有记忆
# ─────────────────────────────────────────────────────────
def experiment_a():
    print("=" * 60)
    print("实验 A：两次独立调用，模型记不住")
    print("=" * 60)

    print('\n[第 1 次调用] 我说："我叫陈嘉岩，我在准备医疗信息化的面试。"')
    call([{"role": "user", "content": "我叫陈嘉岩，我在准备医疗信息化的面试。"}])

    print('\n[第 2 次调用] 我问："我叫什么名字？"')
    answer, _ = call([{"role": "user", "content": "我叫什么名字？"}])
    print("模型回答：", answer)
    print("\n↑ 它答不出来。因为第 2 次请求里**只有这一句**，前面那句根本没发过去。\n")


# ─────────────────────────────────────────────────────────
# 实验 B：把历史带上，就有记忆了
# ─────────────────────────────────────────────────────────
def experiment_b():
    print("=" * 60)
    print("实验 B：自己维护 messages 列表，就有记忆了")
    print("=" * 60)

    # 这就是"记忆"的全部实现 —— 一个不断 append 的 list
    messages = [
        {"role": "system", "content": "你是面试辅导助手，回答简短，不超过两句话。"},
        {"role": "user", "content": "我叫陈嘉岩，杭州医学院医学信息工程，明年毕业。"},
    ]

    for q in ["我叫什么名字？", "我刚才说我是哪个学校、什么专业的？"]:
        messages.append({"role": "user", "content": q})
        answer, usage = call(messages)
        if answer is None:
            return
        # 关键一步：把模型的回答**也塞回去**，否则下一轮它不知道自己刚说了什么
        messages.append({"role": "assistant", "content": answer})

        print(f"\n我：{q}")
        print(f"它：{answer}")
        print(f"   [本轮 token: prompt={usage.get('prompt_tokens')} "
              f"completion={usage.get('completion_tokens')}]")

    print("\n" + "-" * 60)
    print("此刻 messages 里一共有", len(messages), "条：")
    print(json.dumps(messages, ensure_ascii=False, indent=2))
    print("\n↑ 下一次请求会把**这全部**发过去。历史越长，prompt_tokens 越大，越贵。\n")


# ─────────────────────────────────────────────────────────
# 实验 C：自己跟它聊，观察 token 怎么涨
# ─────────────────────────────────────────────────────────
def experiment_c():
    print("=" * 60)
    print("实验 C：自由对话（输入 exit 退出，输入 /show 看 messages）")
    print("=" * 60)

    messages = [{"role": "system", "content": "你是一个简洁的助手，每次回答不超过三句话。"}]
    total_prompt = 0

    while True:
        try:
            text = input("\n我：").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        if text.lower() in ("exit", "quit", "q"):
            break
        if text == "/show":
            print(json.dumps(messages, ensure_ascii=False, indent=2))
            continue

        messages.append({"role": "user", "content": text})
        answer, usage = call(messages)
        if answer is None:
            messages.pop()          # 失败了把这条撤回，别污染历史
            continue
        messages.append({"role": "assistant", "content": answer})

        p = usage.get("prompt_tokens") or 0
        total_prompt += p
        print(f"它：{answer}")
        print(f"   [本轮 prompt={p} tokens，历史已累计 {len(messages)} 条，累计输入 {total_prompt} tokens]")

    print("\n退出。注意最后几轮的 prompt_tokens 比第一轮大得多 —— 那就是历史记录在重复计费。")


if __name__ == "__main__":
    print(f"模型：{MODEL}\n")
    experiment_a()
    experiment_b()
    experiment_c()
