# -*- coding: utf-8 -*-
"""专门验证 retract（撤回）这条分支。

为什么需要这个测试：
    实测 5 个真实问题，retract 一次都没触发 —— 因为模型习惯"先调工具，
    拿到结果再开口"，不会先寒暄一句。但这条分支一旦写错，用户会看到
    "我先查一下"这句话永远留在屏幕上，下面再接一个答案，很怪。
    所以用**假的流式响应**把它逼出来。这比等模型哪天刚好先说话可靠得多。

跑法（在 ai_lab 目录下）：python tests/check_retract.py
"""
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # ai_lab/
sys.path.insert(0, ROOT)              # 让 08 里的 `from db_ro import ...` 能找到

spec = importlib.util.spec_from_file_location("web9", os.path.join(ROOT, "09_web_demo.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def d(obj):
    return "data: " + json.dumps(obj, ensure_ascii=False)


ROUND1 = [
    d({"choices": [{"delta": {"content": "我先去查一下库存。"}, "finish_reason": None}]}),
    d({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                              "function": {"name": "low_stock_medicines",
                                                           "arguments": ""}}]},
                    "finish_reason": None}]}),
    d({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"threshold"'}}]},
                    "finish_reason": None}]}),
    d({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": ': 15}'}}]},
                    "finish_reason": None}]}),
    # 这个空 choices 的 chunk 是真实平台会发的，必须不崩
    d({"choices": [], "usage": None}),
    d({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
    "data: [DONE]",
]

ROUND2 = [
    d({"choices": [{"delta": {"content": "有"}, "finish_reason": None}]}),
    d({"choices": [{"delta": {"content": " 3 种"}, "finish_reason": None}]}),
    d({"choices": [{"delta": {"content": "低于 15。"}, "finish_reason": None}]}),
    d({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
    "data: [DONE]",
]


class FakeResp:
    def __init__(self, lines):
        self.lines = lines
        self.status_code = 200
        self.text = ""

    def iter_lines(self, decode_unicode=True):
        for line in self.lines:
            yield line


calls = {"n": 0}


def fake_post(url, **kw):
    calls["n"] += 1
    return FakeResp(ROUND1 if calls["n"] == 1 else ROUND2)


m.requests.post = fake_post

events = list(m.stream_agent("随便问点什么", []))

print("事件序列：")
for ev, payload in events:
    brief = json.dumps(payload, ensure_ascii=False)
    print(f"  {ev:<8} {brief[:90]}")

seq = [ev for ev, _ in events]
print()
print("retract 是否出现：", "retract" in seq)
print("工具是否执行：", any(ev == "tool" for ev in seq))
print("是否正常收尾：", seq[-1] == "done")

# 断言：先推文字、再撤回、再调工具
# 注意不能拿"所有 delta"的下标去比 —— 第 2 轮的 delta 本来就在 tool 之后。
# 要比的是 **第 1 个** delta、retract、**第 1 个** tool 三者的先后。
first_delta = seq.index("delta") if "delta" in seq else -1
retract_i = seq.index("retract") if "retract" in seq else -1
tool_i = seq.index("tool") if "tool" in seq else -1
ok = -1 < first_delta < retract_i < tool_i
print(f"第一个 delta={first_delta}  retract={retract_i}  第一个 tool={tool_i}")
print("顺序正确（推字 -> 撤回 -> 调工具）：", ok)
print()
print("结论：", "✓ 这条分支可用" if ok else "✗ 逻辑有问题")
