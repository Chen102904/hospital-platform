# -*- coding: utf-8 -*-
"""
第 10 步 · 流式 Agent 引擎

这个文件是从第 9 步的 `09_web_demo.py` 里**原样搬出来**的那个循环，
只改了三处。搬出来本身就是为了改：

  第 9 步是单文件，Flask 路由、SSE 编码、流式循环、HTML 全挤在一起。
  790 行。想加一个"存进数据库"的功能，得在大段字符串模板中间找代码。
  这一步拆成三层：

      stream_engine.py   会边跑边吐事件的循环     ← 你正在看的
      app.py             路由、SSE 编码、落库
      store.py           数据库
      templates/ static/ 页面

  拆完的好处不是"看起来专业"，而是**每一层都能单独测**。
  下面 tests/check_engine.py 就是在没有 Flask、没有浏览器的情况下
  把整个循环跑一遍的。

──────────────────────────────────────────────────────────────────────
相比第 9 步，这里改的三处：

  ① done 事件多带一个 `tools` 列表。
     第 9 步的工具调用只往**前**推（显示成卡片就完事），没留底。
     后果是：刷新页面，那张卡片就没了，库里的历史也只有文字。
     现在把每轮的 name / args / brief 攒进一个列表，跟着 done 一起交出去，
     由 app.py 写进 chat_message.tools —— 于是历史会话能"回放"过程。

  ② 加了 status 的细分：ok / max_rounds / timeout / error。
     第 9 步就有，但没地方存。现在它是数据库里的一列，
     管理页能看到"三次里有几次是被叫停的"。

  ③ 把 CORE 从"模块级全局"改成 init() 注入。
     因为现在有两个地方要跑这个循环（Web 和测试脚本），
     谁先 import 谁负责初始化，不能再靠 import 副作用。
"""

import json
import time

import requests

CORE = None      # 08_rag_agent 模块，由 init() 注入


def init(core_module):
    """把第 8 步的业务逻辑交给引擎。

    不在这里 import，是为了让 test 脚本能换成假的 core —— 依赖注入的
    唯一好处就是这个：**测试时可以不用真的连数据库、不用真的花钱调模型**。
    """
    global CORE
    CORE = core_module


def brief_of(name, result):
    """把工具结果压成一行摘要，给前端显示、也存进库。

    直接把 json 塞给前端会很难看（尤其 search_docs 返回整段正文）。
    """
    if not result.get("ok"):
        return "失败：" + str(result.get("reason", ""))[:80]

    if name == "search_docs":
        hits = result.get("hits") or []
        if not hits:
            return "0 命中（低于阈值）"
        top = hits[0]
        return f"命中 {len(hits)} 块｜最高 {top['score']:.2f}｜{top['section'][:28]}"

    if name == "dept_revenue_rank":
        rows = result.get("rows") or []
        if not rows:
            return "0 行"
        return f"{len(rows)} 行｜第一名：{rows[0].get('科室')} {rows[0].get('收入')}"

    if name == "low_stock_medicines":
        rows = result.get("rows") or []
        if not rows:
            return "没有告急药品"
        first = rows[0]
        return (f"{len(rows)} 种低于阈值｜最紧：{first.get('药品')} "
                f"剩 {first.get('库存')} {first.get('单位') or ''}")

    if name == "medicine_stock":
        rows = result.get("rows") or []
        if not rows:
            return "0 条"
        first = rows[0]
        arrow = "最少" if result.get("order") != "desc" else "最多"
        return (f"{len(rows)} 条｜{arrow}：{first.get('药品')} "
                f"剩 {first.get('库存')} {first.get('单位') or ''}")

    return json.dumps(result, ensure_ascii=False, default=str)[:70]


def stream_agent(question, history, max_rounds=6, max_seconds=150):
    """一次完整的问答，边跑边 yield (事件名, 数据)。

    事件类型（前端按 t 字段分支）：
        round   第几轮            {"n": 1}
        status  状态文案          {"text": "正在查数据库…"}
        delta   一段新文字        {"text": "内科"}
        retract 撤回本轮已推文字   {}
        tool    调用了工具        {"name","args","brief"}
        done    结束              {"status","tokens","elapsed","rounds","tools"}
        error   出错              {"message"}
    """
    if CORE is None:
        raise RuntimeError("stream_engine 没初始化：先调用 init(core)")

    messages = (
        [{"role": "system", "content": CORE.SYSTEM}]
        + list(history)
        + [{"role": "user", "content": question}]
    )

    t0 = time.time()
    chars = 0                # 累计输出字符数，最后用来估 token
    seen = {}                # 防重复调用：(工具名, 参数) -> 次数
    tool_log = []            # ★ 新增：本轮从头到尾调过哪些工具
    retried_empty = False    # 空壳回复只追问一次，不能无限重试烧钱

    for rnd in range(1, max_rounds + 1):
        if time.time() - t0 > max_seconds:
            yield ("done", {"status": "timeout", "tokens": 0,
                            "elapsed": round(time.time() - t0, 1),
                            "rounds": rnd, "tools": tool_log})
            return

        yield ("round", {"n": rnd})

        body = {
            "model": CORE.MODEL,
            "messages": messages,
            "tools": CORE.TOOLS,
            "stream": True,
        }
        r = requests.post(
            CORE.API_URL, headers=CORE.HEADERS, json=body,
            stream=True, timeout=(10, 120), proxies=CORE.PROXIES,
        )
        if r.status_code != 200:
            yield ("error", {"message": f"接口 HTTP {r.status_code}：{r.text[:200]}"})
            yield ("done", {"status": "error", "tokens": int(chars / 1.5),
                            "elapsed": round(time.time() - t0, 1),
                            "rounds": rnd, "tools": tool_log})
            return

        # ── 边收边解析 ──
        content_parts = []
        tool_slots = {}          # index -> {id, name, arguments}
        pushed = 0               # 本轮已经推给前端多少字

        for line in r.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            payload = line[6:].strip()
            if payload == "[DONE]":
                break
            try:
                d = json.loads(payload)
            except json.JSONDecodeError:
                continue

            # ★ 坑：流式响应里会出现 choices 为空数组的 chunk（比如只带 usage
            #   或心跳），直接取 [0] 会 IndexError，整条流就断了。
            choices = d.get("choices") or []
            if not choices:
                continue
            ch = choices[0]
            delta = ch.get("delta") or {}

            text = delta.get("content")
            if text:
                content_parts.append(text)
                chars += len(text)
                pushed += len(text)
                yield ("delta", {"text": text})

            # 工具调用的参数是一段一段吐出来的，要按 index 攒起来
            for tc in (delta.get("tool_calls") or []):
                i = tc.get("index", 0)
                slot = tool_slots.setdefault(i, {"id": "", "name": "", "arguments": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]

        # ── 这一轮结束了：答完了，还是要调工具？ ──
        if not tool_slots:
            got = "".join(content_parts).strip()
            # 空壳回复：没调工具、一个字也没写。流式下用户就是盯着屏幕等，最后啥也没有。
            # 记成 ok 会让"沉默"看起来像成功，统计和评测都发现不了。追问一次。
            if not got and not retried_empty:
                retried_empty = True
                yield ("status", {"text": "模型没给出内容，重试一次…"})
                messages.append({"role": "user",
                                 "content": "你上一条回复是空的。请直接回答上面的问题，别留空。"})
                continue
            yield ("done", {
                "status": "ok" if got else "empty",
                "tokens": int(chars / 1.5),   # 平台流式不给 usage，只能估
                "elapsed": round(time.time() - t0, 1),
                "rounds": rnd,
                "estimated": True,
                "tools": tool_log,
            })
            return

        # 它要调工具 —— 但刚才流出去的字只是"我先查一下"这类话术，
        # 留着会让用户以为那就是答案。撤回。
        if pushed:
            yield ("retract", {})

        # 把这一轮的 assistant 消息（含 tool_calls）放回历史，这是协议要求
        messages.append({
            "role": "assistant",
            "content": "".join(content_parts) or None,
            "tool_calls": [
                {
                    "id": s["id"] or f"call_{i}",
                    "type": "function",
                    "function": {"name": s["name"], "arguments": s["arguments"]},
                }
                for i, s in sorted(tool_slots.items())
            ],
        })

        for i, s in sorted(tool_slots.items()):
            name, raw = s["name"], s["arguments"]
            yield ("status", {"text": f"正在调用 {name}…"})

            key = (name, raw)
            seen[key] = seen.get(key, 0) + 1
            if seen[key] > 2:
                result = {"ok": False,
                          "reason": "这个调用重复了，结果不会变。请换思路或直接用现有信息回答。"}
            else:
                result = CORE.dispatch(name, raw)

            brief = brief_of(name, result)
            tool_log.append({"name": name, "args": raw[:200], "brief": brief})
            yield ("tool", {"name": name, "args": raw[:160], "brief": brief})

            messages.append({
                "role": "tool",
                "tool_call_id": s["id"] or f"call_{i}",
                "content": json.dumps(result, ensure_ascii=False, default=str),
            })

    yield ("done", {"status": "max_rounds", "tokens": int(chars / 1.5),
                    "elapsed": round(time.time() - t0, 1),
                    "rounds": max_rounds, "estimated": True, "tools": tool_log})
