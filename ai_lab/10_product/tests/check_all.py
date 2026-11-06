# -*- coding: utf-8 -*-
"""
第 10 步 · 全部测试

跑法（不需要模型、不需要 MySQL、不花钱一条请求）：

    cd 10_product
    C:/Users/dell/.workbuddy/binaries/python/envs/default/Scripts/python.exe tests/check_all.py

──────────────────────────────────────────────────────────────────────
三组：

  ① store    持久化层。用**临时数据库**，跑完自动删，不碰你真实的聊天记录。
  ② engine   流式循环。把 requests.post 换成一个假流，能精确控制
             "第一轮要工具、第二轮才回答"，于是 retract / max_rounds
             这些平时很难触发的分支都能逼出来。
  ③ api      HTTP 层。用 Flask 的 test_client，不起服务、不占端口。

──────────────────────────────────────────────────────────────────────
为什么值得写这个文件：

第 9 步的验证方式是"打开浏览器点一遍"。这招在功能少的时候够用，
但这一步加了持久化之后，**很多错误是静默的** ——

  · 外键没开，删了会话消息还在 → 页面上看不出来，只是库越来越大
  · history_for_model 没过滤 aborted → 要等到某一轮模型开始胡说才发现
  · 上下文截断截出孤立的 assistant → 接口 400，而页面只显示"连接失败"

这些都不会在"点一遍"里暴露。所以得让机器每条都断一次。
"""

import json
import os
import shutil
import sys
import tempfile
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
sys.path.insert(0, BASE)

import store            # noqa: E402
import stream_engine    # noqa: E402

PASS, FAIL = [], []


def ok(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(("  ✅ " if cond else "  ❌ ") + name + (("  ← " + str(extra)) if extra and not cond else ""))


def section(t):
    print("\n" + "─" * 62)
    print(t)
    print("─" * 62)


# ══════════════════════════════════════════════════════════════════════
# 一、持久化层（临时库）
# ══════════════════════════════════════════════════════════════════════

def test_store():
    section("① 持久化层 store.py")

    tmp = tempfile.mkdtemp(prefix="ai_lab_test_")
    store.DATA_DIR = tmp
    store.DB_PATH = os.path.join(tmp, "t.db")
    store.init_db(verbose=False)

    try:
        # ── 会话基本操作 ──
        s = store.create_session()
        sid = s["id"]
        ok("新建会话返回 32 位 id", len(sid) == 32)
        ok("刚建时标题是「新对话」", s["title"] == "新对话")

        store.add_message(sid, "user", "近 30 天哪个科室收入最高？")
        store.touch_session(sid, "近 30 天哪个科室收入最高？")
        got = store.get_session(sid)
        ok("第一句话变成标题", got["title"] == "近 30 天哪个科室收入最高？",
           got["title"])

        # ── 工具记录能不能存回来 ──
        tools = [{"name": "dept_revenue_rank", "args": '{"days":30}',
                  "brief": "11 行｜第一名：内科 1159.4"}]
        store.add_message(sid, "assistant", "内科 1159.40 元。",
                          elapsed=5.4, rounds=2, tokens=312,
                          status="ok", tools=tools)
        msgs = store.get_messages(sid)
        ok("消息按 id 正序返回", [m["role"] for m in msgs] == ["user", "assistant"])
        ok("工具记录能反序列化回来",
           len(msgs[1]["tools"]) == 1 and msgs[1]["tools"][0]["name"] == "dept_revenue_rank")
        ok("中文没有被转义成 \\u",
           "内科" in json.dumps(msgs[1]["tools"], ensure_ascii=False))

        # ── 非法 id 一律返回 None / []，不抛异常 ──
        for bad in ["", "abc", "../../etc/passwd", "a" * 31, "Z" * 32, None]:
            ok(f"非法 id {bad!r} 被挡住", store.get_session(bad) is None
               and store.get_messages(bad) == [])

        # ── 上下文：要跳过非 ok 的那一对 ──
        sid2 = store.create_session()["id"]
        store.add_message(sid2, "user", "Q1")
        store.add_message(sid2, "assistant", "A1", status="ok")
        store.add_message(sid2, "user", "Q2 会超时的那句")
        store.add_message(sid2, "assistant", "⚠ 超时", status="timeout")
        store.add_message(sid2, "user", "Q3")
        store.add_message(sid2, "assistant", "A3", status="ok")

        h = store.history_for_model(sid2, keep=10)
        texts = [m["content"] for m in h]
        ok("超时那一对被剔除", "Q2 会超时的那句" not in texts, texts)
        ok("正常的两对保留", texts == ["Q1", "A1", "Q3", "A3"], texts)
        ok("第一条一定是 user", h[0]["role"] == "user")

        # 截断时也不能从中间切开
        h2 = store.history_for_model(sid2, keep=2)
        ok("keep=2 时第一条仍是 user", h2[0]["role"] == "user", h2)

        # ── 统计 ──
        # 库里现在有 4 条 assistant：A1(ok)、超时那条(timeout)、A3(ok)、内科(ok)
        # 所以成功率应该是 3/4 = 75%，不是 100%。
        st = store.stats()
        ok("统计：会话数", st["sessions"] == 2, st["sessions"])
        ok("统计：异常数只数非 ok 的", st["abnormal"] == 1, st["abnormal"])
        ok("统计：成功率 = 3/4 = 75%", st["ok_rate"] == 75.0, st["ok_rate"])
        ok("统计：工具调用计数", st["tool_total"] == 1, st["tool_total"])
        ok("统计：平均耗时", st["avg_elapsed"] == 5.4, st["avg_elapsed"])

        # ── 导出 ──
        md = store.export_markdown(sid)
        ok("导出含标题", md.startswith("# 近 30 天"))
        ok("导出含工具调用行", "dept_revenue_rank" in md)
        ok("导出含耗时", "5.4 秒" in md)

        # ── 级联删除（外键真的开了吗）──
        ok("删会话前消息在", len(store.get_messages(sid)) > 0)
        ok("删除会话成功", store.delete_session(sid) is True)
        ok("删会话后消息跟着没了（外键 CASCADE 生效）",
           len(store.get_messages(sid)) == 0)

        # 绕过 API 直接查表，确认是真的没了，不是 get_messages 骗人
        with store.connect() as conn:
            n = conn.execute("SELECT COUNT(*) c FROM chat_message WHERE session_id=?",
                             (sid,)).fetchone()["c"]
        ok("绕过 API 查表也确认消息已删", n == 0, n)

        # ── 改名 ──
        ok("改名成功", store.rename_session(sid2, "改过的名字") is True)
        ok("改名生效", store.get_session(sid2)["title"] == "改过的名字")
        ok("改空名字退回默认", (store.rename_session(sid2, "   ") and
                              store.get_session(sid2)["title"] == "新对话"))

        # ── 侧栏要过滤空会话 ──
        empty = store.create_session()["id"]
        ids_all = [x["id"] for x in store.list_sessions(include_empty=True)]
        ids_used = [x["id"] for x in store.list_sessions()]
        ok("include_empty=True 能列出空会话", empty in ids_all)
        ok("默认不列空会话", empty not in ids_used)

    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        store.DATA_DIR = os.path.join(BASE, "_data")
        store.DB_PATH = os.path.join(BASE, "_data", "app.db")


# ══════════════════════════════════════════════════════════════════════
# 二、流式引擎（假模型 + 假流）
# ══════════════════════════════════════════════════════════════════════

class FakeResp:
    """冒充 requests 的流式响应。"""

    def __init__(self, frames, status=200):
        self.status_code = status
        self._frames = frames
        self.text = "fake error body"

    def iter_lines(self, decode_unicode=True):
        for f in self._frames:
            yield f


class FakeCore:
    MODEL = "fake-model"
    API_URL = "http://fake.local/v1/chat"
    HEADERS = {}
    PROXIES = {}
    SYSTEM = "you are a fake"
    TOOLS = [{"type": "function", "function": {"name": "dept_revenue_rank"}}]
    DB_RO = {"user": "fake"}

    def dispatch(self, name, raw):
        return {"ok": True, "rows": [{"科室": "内科", "收入": 1159.4}]}


def frames_tool_then_answer():
    """一轮要工具、二轮回答。"""
    f1 = [
        'data: ' + json.dumps({"choices": [{"delta": {"content": "我先查一下。"}}]},
                              ensure_ascii=False),
        'data: ' + json.dumps({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_1",
             "function": {"name": "dept_revenue_rank", "arguments": '{"days":30}'}}]}}]},
            ensure_ascii=False),
        'data: ' + json.dumps({"choices": [{"finish_reason": "tool_calls", "delta": {}}]}),
        "data: [DONE]",
    ]
    f2 = [
        'data: ' + json.dumps({"choices": [{"delta": {"content": "内科"}}]},
                              ensure_ascii=False),
        'data: ' + json.dumps({"choices": [{"delta": {"content": " 1159.40 元。"}}]},
                              ensure_ascii=False),
        'data: ' + json.dumps({"choices": [{"finish_reason": "stop", "delta": {}}]}),
        "data: [DONE]",
    ]
    return [FakeResp(f1), FakeResp(f2)]


def collect(fakes):
    """把引擎吐的事件收成 [(事件名, 数据), ...]。"""
    it = iter(fakes)

    def fake_post(*a, **kw):
        return next(it)

    with mock.patch.object(stream_engine.requests, "post", side_effect=fake_post):
        return list(stream_engine.stream_agent("哪个科室收入最高？", []))


def test_engine():
    section("② 流式引擎 stream_engine.py")

    stream_engine.init(FakeCore())

    events = collect(frames_tool_then_answer())
    names = [e[0] for e in events]

    ok("事件里有 retract（先推了字才去调工具，必须撤回）", "retract" in names, names)
    ok("撤回发生在推字之后",
       names.index("delta") < names.index("retract") < names.index("status"), names)
    ok("工具卡片事件存在", "tool" in names)

    tool_ev = [e[1] for e in events if e[0] == "tool"][0]
    ok("工具摘要压缩成一行",
       "11" in tool_ev["brief"] or "1159" in tool_ev["brief"], tool_ev)

    done = [e[1] for e in events if e[0] == "done"][0]
    ok("done.status = ok", done["status"] == "ok", done["status"])
    ok("done 带着工具记录（第 10 步新增，写库靠它）",
       len(done["tools"]) == 1 and done["tools"][0]["name"] == "dept_revenue_rank",
       done.get("tools"))
    ok("done 的轮次是 2", done["rounds"] == 2, done["rounds"])

    text = "".join(e[1]["text"] for e in events if e[0] == "delta")
    ok("两轮的 delta 拼起来是完整回答", "内科 1159.40 元。" in text, text)

    # ── 空 choices 不能炸（第 9 步踩过的坑）──
    broken = [
        'data: ' + json.dumps({"choices": []}),          # 空数组
        'data: ' + json.dumps({"usage": {"total_tokens": 9}}),   # 没有 choices
        'data: ' + json.dumps({"choices": [{"delta": {"content": "OK"}}]}),
        "data: [DONE]",
    ]
    ev2 = collect([FakeResp(broken)])
    ok("空 choices 的 chunk 被跳过，不抛 IndexError",
       any(e[0] == "delta" for e in ev2) and ev2[-1][1]["status"] == "ok")

    # ── 模型一直要工具，必须被 max_rounds 叫停 ──
    loop_frames = [[
        'data: ' + json.dumps({"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "c", "function": {"name": "dept_revenue_rank",
                                                 "arguments": '{"days":1}'}}]}}]}),
        "data: [DONE]",
    ]] * 5
    engine = stream_engine.stream_agent("死循环", [], max_rounds=3)
    with mock.patch.object(stream_engine.requests, "post",
                           side_effect=lambda *a, **k: FakeResp(loop_frames[0])):
        ev3 = list(engine)
    d3 = [e[1] for e in ev3 if e[0] == "done"][0]
    ok("一直要工具会被 max_rounds 叫停", d3["status"] == "max_rounds", d3["status"])
    ok("叫停时只跑了 3 轮", d3["rounds"] == 3, d3["rounds"])

    # ── 接口 500 要变成 error，而不是静默断流 ──
    with mock.patch.object(stream_engine.requests, "post",
                           side_effect=lambda *a, **k: FakeResp([], status=500)):
        ev4 = list(stream_engine.stream_agent("炸了", []))
    ok("HTTP 500 变成 error 事件", any(e[0] == "error" for e in ev4))
    ok("500 之后仍然会发 done（前端才有机会收尾）",
       ev4[-1][0] == "done" and ev4[-1][1]["status"] == "error", ev4[-1][0])

    raw = '{"query":"密码","top_k":3}'
    ok("brief_of：search_docs 有命中", "命中" in stream_engine.brief_of(
        "search_docs", {"ok": True, "hits": [{"score": .66, "section": "密码存储"}]}))
    ok("brief_of：search_docs 空结果给 0 命中", "0 命中" in stream_engine.brief_of(
        "search_docs", {"ok": True, "hits": []}))
    ok("brief_of：失败要说失败", "失败" in stream_engine.brief_of(
        "x", {"ok": False, "reason": "boom"}))


# ══════════════════════════════════════════════════════════════════════
# 三、HTTP 层
# ══════════════════════════════════════════════════════════════════════

def test_api():
    section("③ HTTP 层 app.py（FakeCore，不连真库、不调真模型）")

    # 先把 CORE 换成假的，再 import app —— 免得它去连 MySQL、算向量
    import app as web

    real_core = web.CORE          # import 时加载的那个就是真的第 8 步 core，留着后面测
    web.CORE = FakeCore()
    web.CORE.IDX = type("Idx", (), {"chunks": [1, 2, 3]})()
    stream_engine.init(web.CORE)

    # 换一个临时库，别写到真实聊天记录里
    tmp = tempfile.mkdtemp(prefix="ai_lab_api_")
    store.DATA_DIR = tmp
    store.DB_PATH = os.path.join(tmp, "a.db")
    store.init_db(verbose=False)

    try:
        c = web.app.test_client()

        r = c.get("/")
        ok("GET / 会重定向", r.status_code == 302, r.status_code)

        r = c.get("/new")
        sid = r.headers["Location"].rsplit("/", 1)[-1]
        ok("GET /new 建会话并跳转", r.status_code == 302 and store.valid_id(sid), sid)

        r = c.get(f"/c/{sid}")
        ok("聊天页 200", r.status_code == 200)
        body = r.get_data(as_text=True)
        ok("页面里带着 sid", sid in body)
        ok("页面引用了自己的 CSS", "app.css" in body)

        r = c.get("/c/" + "0" * 32)
        ok("不存在的会话 404", r.status_code == 404)

        r = c.post("/api/ask", json={"q": "", "sid": sid})
        ok("空问题 400", r.status_code == 400, r.status_code)

        r = c.post("/api/ask", json={"q": "你好", "sid": "bad"})
        ok("非法 sid 404", r.status_code == 404, r.status_code)

        # 真正跑一次问答（假模型，两次 HTTP 都走 FakeCore 的流）
        with mock.patch.object(stream_engine.requests, "post",
                               side_effect=frames_tool_then_answer()):
            r = c.post("/api/ask", json={"q": "哪个科室收入最高？", "sid": sid})
            raw = r.get_data(as_text=True)
        ok("SSE 返回 200", r.status_code == 200)
        ok("SSE 是 data: 分帧", raw.count("data: ") >= 4, raw.count("data: "))
        ok("SSE 里有 done", '"t": "done"' in raw)

        msgs = store.get_messages(sid)
        ok("问答落库了（2 条）", len(msgs) == 2, len(msgs))
        if len(msgs) == 2:
            ok("user 消息内容正确", msgs[0]["content"] == "哪个科室收入最高？")
            ok("assistant 带工具记录", len(msgs[1]["tools"]) == 1)
            ok("assistant 记了耗时", msgs[1]["elapsed"] is not None, msgs[1]["elapsed"])
            ok("assistant 状态是 ok", msgs[1]["status"] == "ok")
        ok("标题被第一句话改了",
           store.get_session(sid)["title"] == "哪个科室收入最高？",
           store.get_session(sid)["title"])

        # ── 工具白名单：拿**真的**第 8 步 core 试，不是假的 ──
        r_bad = real_core.dispatch("drop_table", "{}")
        ok("真 dispatch 拒绝没注册的工具",
           r_bad.get("ok") is False and "没有这个工具" in r_bad.get("reason", ""),
           r_bad)
        r_bad2 = real_core.dispatch("dept_revenue_rank", "这不是 JSON")
        ok("参数不是 JSON 时返回失败而不是抛异常",
           r_bad2.get("ok") is False, r_bad2)

        # ── 空壳回复：模型不调工具、一个字也不写 ──
        # A02 实测踩过：status=ok、2.4 秒、0 次工具、答案空白。统计上看不出任何异常，
        # 用户看到的是"发出去没反应"。所以这条要用假模型锁死，别再悄悄退回去。
        orig_model = real_core.real_model
        try:
            blank = {"calls": 0}

            def fake_blank(messages, tools=None, timeout=120):
                blank["calls"] += 1
                return {"content": "   ", "tool_calls": None}, {"total_tokens": 1}, "stop"

            real_core.real_model = fake_blank
            rr = real_core.run_agent("随便问点什么", verbose=False)
            ok("空壳回复不会被当成成功", rr["status"] == "empty", rr.get("status"))
            ok("空壳回复只追问一次，不无限重试", blank["calls"] == 2, blank["calls"])
        finally:
            real_core.real_model = orig_model

        try:
            seq = {"n": 0}

            def fake_blank_then_ok(messages, tools=None, timeout=120):
                seq["n"] += 1
                if seq["n"] == 1:
                    return {"content": "", "tool_calls": None}, {"total_tokens": 1}, "stop"
                return {"content": "补上的答案", "tool_calls": None}, {"total_tokens": 1}, "stop"

            real_core.real_model = fake_blank_then_ok
            rr2 = real_core.run_agent("随便问点什么", verbose=False)
            ok("空壳后追问一次能救回来",
               rr2["status"] == "ok" and "补上的答案" in rr2["answer"], rr2.get("status"))
        finally:
            real_core.real_model = orig_model

        # ── 改名 / 导出 / 删除 ──
        r = c.patch(f"/api/session/{sid}", json={"title": "改个名字"})
        ok("PATCH 改名 200", r.status_code == 200 and r.get_json()["ok"] is True)
        ok("改名真的写进去了", store.get_session(sid)["title"] == "改个名字")

        r = c.patch(f"/api/session/{sid}", json={"title": ""})
        ok("空标题 400", r.status_code == 400)

        r = c.get(f"/api/session/{sid}/export")
        ok("导出 200", r.status_code == 200)
        ok("导出是 markdown", "markdown" in r.headers["Content-Type"])
        ok("导出带附件名", "attachment" in r.headers["Content-Disposition"])
        ok("导出内容含问答", "哪个科室收入最高？" in r.get_data(as_text=True))
        ok("Content-Type 里的 charset 不重复",
           r.headers["Content-Type"].count("charset") == 1,
           r.headers["Content-Type"])

        r = c.get("/api/session/" + "0" * 32 + "/export")
        ok("导出不存在的会话 404", r.status_code == 404)

        r = c.delete(f"/api/session/{sid}")
        ok("DELETE 200", r.status_code == 200)
        ok("删除后会话没了", store.get_session(sid) is None)
        r = c.delete(f"/api/session/{sid}")
        ok("重复删除返回 404", r.status_code == 404)

        r = c.get("/api/health")
        h = r.get_json()
        ok("health 返回模型名", h["model"] == "fake-model", h.get("model"))
        # 这里注入的是假 core，只能验证"如实透出"，不能拿它数工具个数
        ok("health 返回工具清单（白名单可见）", "dept_revenue_rank" in h["tools"], h["tools"])
        # 真 core 必须注册 4 个工具。medicine_stock 是为「库存最少是哪个」这类
        # 排序题加的——被误删或忘了进 TOOLS，这里立刻报警（纯内存，不连库）
        real_names = {t["function"]["name"] for t in real_core.TOOLS}
        want = {"dept_revenue_rank", "low_stock_medicines", "medicine_stock", "search_docs"}
        ok("真 core 注册了全部 4 个工具", want <= real_names, sorted(real_names))

        r = c.get("/history")
        ok("记录页 200", r.status_code == 200)

    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        store.DATA_DIR = os.path.join(BASE, "_data")
        store.DB_PATH = os.path.join(BASE, "_data", "app.db")


# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 62)
    print("  第 10 步 · 全部测试")
    print("=" * 62)

    test_store()
    test_engine()
    test_api()

    print("\n" + "=" * 62)
    total = len(PASS) + len(FAIL)
    if FAIL:
        print(f"  ✗ {len(FAIL)} 项失败 / 共 {total} 项")
        for f in FAIL:
            print("    - " + f)
        print("=" * 62)
        sys.exit(1)
    print(f"  ✓ 全部通过（{total} 项）")
    print("=" * 62)
