# -*- coding: utf-8 -*-
"""
第 9 步：把它变成一个能当场演示的网页

前面八步都是命令行。命令行有个致命问题：**面试时你没法让人"体验"它。**
这一步把第 8 步那个 Agent 搬到浏览器里，一个输入框，问完就答。

运行：
    C:/Users/dell/.workbuddy/binaries/python/envs/default/Scripts/python.exe 09_web_demo.py
然后浏览器打开 http://127.0.0.1:5001

──────────────────────────────────────────────────────────────────────
这一步真正要解决的是四个「Web 才有」的新问题，前面八步一个都没碰到：

  ① 流式输出
     不流式的话，用户要盯着空白屏幕等 8 秒才看到第一句话。
     流式 = 一个字一个字吐出来，体感完全不同。
     ★ 但流式和工具调用天然打架：流式是"边想边说"，工具调用要求
       "先想清楚再动手"。本脚本用「先推、发现要调工具就撤回」解决。

  ② 多轮会话在 Web 上是断的
     命令行里 messages 是个变量，活得比对话长。
     HTTP 是无状态的 —— 浏览器每发一次请求都是新连接，
     模型更是"每次调用都失忆"。
     所以服务端必须自己存一个 dict：谁（cookie）→ 聊过什么。
     ★ 所谓"它会记住"，全是这个 dict 在撑着。

  ③ 上下文会无限膨胀
     存着不管，第 20 轮时 prompt 就是两万 token。
     所以每次只带最近几轮进去（TRIM_KEEP），更早的丢掉。

  ④ 前端得看得见"它在干什么"
     8 秒的等待如果只有转圈，用户会以为死了。
     所以要把每次工具调用实时推给前端，显示成卡片。

──────────────────────────────────────────────────────────────────────
分层说明（这一点对写简历有用）：

    08_rag_agent.py   = 业务逻辑（工具、循环、提示词）
    09_web_demo.py    = 展示层（Flask 路由 + 一个页面）

本脚本不复制 08 的任何工具代码，而是 import 它。
意思是：**明天换成 FastAPI、换成微信机器人、换成命令行，核心那 40 行循环不用改。**
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
import os
import sys
import time
import uuid
import importlib.util

import requests
from flask import Flask, Response, request, jsonify

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PORT = 5001          # 避开 hospital_web 的 5000，两个可以同时开


# ══════════════════════════════════════════════════════════════════════
# 一、复用第 8 步的 Agent 核心
# ══════════════════════════════════════════════════════════════════════

def load_core():
    """把 08_rag_agent.py 当模块加载进来。

    为什么不用 import？文件名以数字开头，不是合法标识符。
    所以用 importlib 按路径加载 —— 这是标准做法。
    """
    path = os.path.join(HERE, "08_rag_agent.py")
    if not os.path.exists(path):
        sys.exit("✗ 找不到 08_rag_agent.py，第 8 步还没做？")

    spec = importlib.util.spec_from_file_location("agent_core", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["agent_core"] = mod
    spec.loader.exec_module(mod)
    return mod


CORE = load_core()


def boot():
    """启动时把知识库装进内存。

    63 块的向量如果每次都重算，每个请求要多等 2 秒。
    所以放在进程启动时做一次 —— 这也是「服务」和「脚本」的区别。
    """
    chunks = CORE.build_chunks()
    CORE.IDX = CORE.DocIndex(chunks)
    print(f"知识库已加载：{len(chunks)} 块", flush=True)
    print(f"模型：{CORE.MODEL}", flush=True)
    print(f"数据库：只读账号 {CORE.DB_RO.get('user')}", flush=True)


# ══════════════════════════════════════════════════════════════════════
# 二、会话存储（这就是「记忆」的全部真相）
# ══════════════════════════════════════════════════════════════════════

SESSIONS = {}        # sid -> [ {"role":"user",...}, {"role":"assistant",...} ]
TRIM_KEEP = 6        # 每次只把最近 6 条（3 轮）发给模型
MAX_SESSIONS = 200   # 防止演示久了内存里堆一堆僵尸会话


def trim(history):
    """只保留最近几轮，并且保证第一条是 user。

    不截断的后果：第 20 轮时 prompt 两万 token，又慢又贵，
    而且模型反而容易被早期无关内容带偏。
    """
    h = history[-TRIM_KEEP:]
    while h and h[0]["role"] != "user":
        h = h[1:]
    return h


def sse(obj):
    """把一条事件编码成 SSE 格式。

    注意格式：`data: ` 前缀 + 两个换行结尾，一个都不能少。
    少一个换行，浏览器就把两条事件当成一条。
    """
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


# ══════════════════════════════════════════════════════════════════════
# 三、流式 Agent 循环（第 5 步的循环 + 流式改造）
# ══════════════════════════════════════════════════════════════════════

def brief_of(name, result):
    """把工具结果压成一行摘要，给前端显示。

    直接把 json 塞给前端会很难看（尤其是 search_docs 返回的整段正文）。
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
        return f"{len(rows)} 种低于阈值｜最紧：{first.get('药品')} 剩 {first.get('库存')} {first.get('单位') or ''}"

    if name == "medicine_stock":
        rows = result.get("rows") or []
        if not rows:
            return "0 条"
        first = rows[0]
        arrow = "最少" if result.get("order") != "desc" else "最多"
        return (f"{len(rows)} 条｜{arrow}：{first.get('药品')} "
                f"剩 {first.get('库存')} {first.get('单位') or ''}")

    return json.dumps(result, ensure_ascii=False, default=str)[:70]


def stream_agent(question, history):
    """一次完整的问答，边跑边 yield 事件。

    事件类型（前端按 t 字段分支）：
        round   第几轮        {"n": 1}
        status  状态文案      {"text": "正在查数据库…"}
        delta   一段新文字    {"text": "内科"}
        retract 撤回本轮文字   {}
        tool    调用了工具    {"name": ..., "args": ..., "brief": ...}
        done    结束          {"status":..., "tokens":..., "elapsed":..., "rounds":...}
        error   出错          {"message": ...}
    """
    messages = (
        [{"role": "system", "content": CORE.SYSTEM}]
        + trim(history)
        + [{"role": "user", "content": question}]
    )

    t0 = time.time()
    chars = 0                # 累计输出字符数，最后用来估 token
    seen = {}                # 防重复调用：(工具名, 参数) -> 次数
    max_rounds = 6
    max_seconds = 150

    for rnd in range(1, max_rounds + 1):
        if time.time() - t0 > max_seconds:
            yield ("done", {"status": "timeout", "tokens": 0,
                            "elapsed": round(time.time() - t0, 1), "rounds": rnd})
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
            return

        # ── 边收边解析 ──
        content_parts = []
        tool_slots = {}          # index -> {id, name, arguments}
        finish = None
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

            # ★ 踩过的坑：流式响应里可能出现 choices 为空数组的 chunk，
            #   直接取 [0] 会 IndexError。必须判空。
            choices = d.get("choices") or []
            if not choices:
                continue
            ch = choices[0]

            if ch.get("finish_reason"):
                finish = ch["finish_reason"]

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

        # ── 这一轮结束了，判断：它是答完了，还是要调工具？ ──
        if not tool_slots:
            # 正常收尾
            yield ("done", {
                "status": "ok",
                "tokens": int(chars / 1.5),   # 平台流式不给 usage，只能估
                "elapsed": round(time.time() - t0, 1),
                "rounds": rnd,
                "estimated": True,
            })
            return

        # 它要调工具 —— 但刚才流出去的那些字只是"我先查一下"这类话术，
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

            yield ("tool", {"name": name, "args": raw[:160],
                            "brief": brief_of(name, result)})

            messages.append({
                "role": "tool",
                "tool_call_id": s["id"] or f"call_{i}",
                "content": json.dumps(result, ensure_ascii=False, default=str),
            })

    yield ("done", {"status": "max_rounds", "tokens": int(chars / 1.5),
                    "elapsed": round(time.time() - t0, 1),
                    "rounds": max_rounds, "estimated": True})


# ══════════════════════════════════════════════════════════════════════
# 四、Flask 路由
# ══════════════════════════════════════════════════════════════════════

app = Flask(__name__)


@app.get("/")
def index():
    return Response(
        PAGE.replace("__MODEL__", CORE.MODEL)
            .replace("__BLOCKS__", str(len(CORE.IDX.chunks)))
            .replace("__DBUSER__", str(CORE.DB_RO.get("user"))),
        mimetype="text/html; charset=utf-8",
    )


@app.post("/api/ask")
def api_ask():
    data = request.get_json(silent=True) or {}
    question = (data.get("q") or "").strip()
    if not question:
        return jsonify({"error": "问题不能为空"}), 400

    # 会话 id 存在 cookie 里。第一次来就发一个。
    sid = request.cookies.get("sid") or uuid.uuid4().hex
    history = SESSIONS.get(sid, [])

    def gen():
        collected = []
        status = "ok"
        try:
            for ev, payload in stream_agent(question, history):
                if ev == "delta":
                    collected.append(payload["text"])
                elif ev == "done":
                    status = payload["status"]
                yield sse({"t": ev, **payload})
        except Exception as e:
            # 生成器里抛异常，SSE 会直接断掉，前端只会看到"连接中断"。
            # 必须自己兜住，把错误当成一条正常事件发出去。
            yield sse({"t": "error", "message": f"{type(e).__name__}: {e}"})
            status = "error"
        finally:
            # 只有正常答完才写进历史。超时/报错的不写，
            # 否则下一轮会把半截答案当成"上一轮说过的"。
            if status == "ok" and collected:
                history.append({"role": "user", "content": question})
                history.append({"role": "assistant", "content": "".join(collected)})
                SESSIONS[sid] = history
                if len(SESSIONS) > MAX_SESSIONS:
                    SESSIONS.pop(next(iter(SESSIONS)))

    resp = Response(gen(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"      # 让 nginx 别缓冲（本地用不上，上服务器能救命）
    resp.set_cookie("sid", sid, httponly=True, samesite="Lax")
    return resp


@app.post("/api/reset")
def api_reset():
    SESSIONS.pop(request.cookies.get("sid") or "", None)
    return jsonify({"ok": True})


@app.get("/api/health")
def api_health():
    return jsonify({
        "ok": True,
        "model": CORE.MODEL,
        "blocks": len(CORE.IDX.chunks),
        "sessions": len(SESSIONS),
        "tools": [t["function"]["name"] for t in CORE.TOOLS],
    })


# ══════════════════════════════════════════════════════════════════════
# 五、页面（单文件，不依赖任何 CDN —— 面试现场网络不好也能开）
# ══════════════════════════════════════════════════════════════════════

PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>HIS 智能助手 · 演示</title>
<style>
  :root{
    --ink:#16202a; --muted:#6b7a89; --line:#e3e8ee; --bg:#f4f7f9;
    --card:#fff; --brand:#0f766e; --soft:#e6f4f1; --warn:#b45309; --bad:#b91c1c;
  }
  *{box-sizing:border-box}
  html,body{height:100%}
  body{
    margin:0; background:var(--bg); color:var(--ink);
    font:14px/1.75 -apple-system,"Segoe UI","Microsoft YaHei",system-ui,sans-serif;
    -webkit-font-smoothing:antialiased;
  }
  .wrap{max-width:880px; margin:0 auto; height:100vh; display:flex; flex-direction:column}

  header{
    padding:16px 22px 14px; background:var(--card); border-bottom:1px solid var(--line);
    display:flex; align-items:center; gap:14px; flex-wrap:wrap;
  }
  .logo{
    width:38px;height:38px;border-radius:11px;flex:0 0 auto;
    background:linear-gradient(135deg,#0f766e,#14b8a6);
    color:#fff;display:grid;place-items:center;font-weight:700;font-size:17px;
    box-shadow:0 2px 8px rgba(15,118,110,.28);
  }
  .title h1{margin:0;font-size:15px;font-weight:650;letter-spacing:.2px}
  .title p{margin:1px 0 0;font-size:12px;color:var(--muted)}
  .meta{
    margin-left:auto; display:flex; gap:6px; flex-wrap:wrap; align-items:center;
  }
  .tag{
    font-size:11.5px; padding:3px 9px; border-radius:999px;
    background:var(--soft); color:var(--brand); border:1px solid #cfe8e3; white-space:nowrap;
  }
  .tag.gray{background:#f1f4f7;color:var(--muted);border-color:var(--line)}
  .btn-ghost{
    font-size:12px;padding:4px 11px;border-radius:8px;border:1px solid var(--line);
    background:#fff;color:var(--muted);cursor:pointer;transition:.15s;
  }
  .btn-ghost:hover{border-color:#c6d2dd;color:var(--ink)}

  main{flex:1;overflow-y:auto;padding:6px 22px 24px;scroll-behavior:smooth}
  main::-webkit-scrollbar{width:9px}
  main::-webkit-scrollbar-thumb{background:#d3dce4;border-radius:9px}

  .msg{display:flex;gap:10px;margin:16px 0;align-items:flex-start;flex-wrap:wrap}
  .msg.user{flex-direction:row-reverse}
  .avatar{
    width:28px;height:28px;border-radius:9px;flex:0 0 auto;display:grid;place-items:center;
    font-size:12px;font-weight:600;
  }
  .bot .avatar{background:var(--soft);color:var(--brand)}
  .user .avatar{background:#e8edf2;color:#54677a}
  .bubble{
    max-width:78%;padding:10px 14px;border-radius:13px;
    white-space:pre-wrap;word-break:break-word;font-size:14px;
  }
  .bot .bubble{background:var(--card);border:1px solid var(--line);border-bottom-left-radius:4px;
               box-shadow:0 1px 2px rgba(16,32,42,.04)}
  .user .bubble{background:var(--brand);color:#fff;border-bottom-right-radius:4px;
                box-shadow:0 2px 8px rgba(15,118,110,.22)}
  .bubble:empty{display:none}
  .bubble strong{font-weight:650;color:#0b3f3a}
  .bubble code{
    font-family:ui-monospace,Consolas,monospace;font-size:12.5px;
    background:#f0f5f6;color:#0f5c55;padding:1px 5px;border-radius:4px;
  }
  .bubble .hr{display:block;height:1px;background:var(--line);margin:9px 0}
  .bubble.streaming::after{
    content:"";display:inline-block;width:7px;height:15px;margin-left:2px;
    background:var(--brand);border-radius:1px;vertical-align:-2px;
    animation:blink 1s steps(2,start) infinite;
  }
  @keyframes blink{50%{opacity:0}}

  .tool{
    margin:9px 0 9px 38px;padding:8px 12px;border-radius:0 9px 9px 0;
    border-left:3px solid var(--brand);background:#f4fbf9;
    font-size:12.5px;color:#54677a;animation:fade .25s ease-out;
    width:calc(100% - 38px);
  }
  .tool .row1{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
  .tool .nm{color:#0b6159;font-weight:700;font-family:ui-monospace,Consolas,monospace;font-size:12px}
  .tool .ar{font-family:ui-monospace,Consolas,monospace;font-size:11.5px;color:#5d6f7e;
            background:#e9f3f1;padding:1px 6px;border-radius:5px;max-width:100%;
            overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .tool .br{margin-top:3px;color:#364b5c}
  @keyframes fade{from{opacity:0;transform:translateY(-3px)}to{opacity:1;transform:none}}

  .status{margin:10px 0 10px 38px;font-size:12.5px;color:var(--muted);
          display:flex;align-items:center;gap:8px;width:calc(100% - 38px)}
  .spin{width:12px;height:12px;border:2px solid #cfe0dd;border-top-color:var(--brand);
        border-radius:50%;animation:sp .7s linear infinite}
  @keyframes sp{to{transform:rotate(360deg)}}

  .stat{margin:5px 0 0 38px;font-size:11.5px;color:#8b9aa8;flex-basis:100%}
  .err{color:var(--bad)}

  .chips{display:flex;gap:8px;padding:0 22px 10px;flex-wrap:wrap}
  .chip{
    font-size:12.5px;padding:6px 12px;border-radius:999px;cursor:pointer;
    border:1px solid var(--line);background:#fff;color:#465a6b;transition:.15s;
  }
  .chip:hover{border-color:var(--brand);color:var(--brand);background:var(--soft)}

  footer{
    padding:12px 22px 18px;background:var(--card);border-top:1px solid var(--line);
    display:flex;gap:10px;align-items:flex-end;
  }
  textarea{
    flex:1;resize:none;border:1px solid var(--line);border-radius:12px;
    padding:11px 14px;font:inherit;max-height:140px;outline:none;transition:.15s;
    background:#fbfcfd;
  }
  textarea:focus{border-color:var(--brand);background:#fff;box-shadow:0 0 0 3px rgba(15,118,110,.09)}
  #send{
    padding:11px 20px;border:0;border-radius:12px;cursor:pointer;
    background:var(--brand);color:#fff;font:inherit;font-weight:600;transition:.15s;
  }
  #send:hover{background:#0d6159}
  #send:disabled{background:#b6c6c4;cursor:not-allowed}
  .hint{padding:0 22px 8px;font-size:11.5px;color:#a3b0bc}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="logo">H</div>
    <div class="title">
      <h1>医院信息系统 · 智能助手</h1>
      <p>能查数据库里的真实数字，也能查你自己的设计文档</p>
    </div>
    <div class="meta">
      <span class="tag">__MODEL__</span>
      <span class="tag gray">知识库 __BLOCKS__ 块</span>
      <span class="tag gray">只读账号 __DBUSER__</span>
      <button class="btn-ghost" id="reset">清空对话</button>
    </div>
  </header>

  <main id="chat"></main>

  <div class="chips" id="chips">
    <button class="chip">近 30 天哪个科室收入最高？</button>
    <button class="chip">库存紧张的药有哪些？</button>
    <button class="chip">这个系统的密码是怎么存的？</button>
    <button class="chip">收入最高的科室，他们是怎么收费的？</button>
    <button class="chip">机房那台 UPS 的电池多久换一次？</button>
  </div>

  <div class="hint">Enter 发送，Shift+Enter 换行。回答基于你的真实数据和文档，查不到它会直说。</div>

  <footer>
    <textarea id="q" rows="1" placeholder="问点什么……"></textarea>
    <button id="send">发送</button>
  </footer>
</div>

<script>
const chat = document.getElementById('chat');
const box  = document.getElementById('q');
const btn  = document.getElementById('send');
let busy = false;

/* ── 渲染小工具 ── */
function el(cls, text){
  const d = document.createElement('div');
  d.className = cls;
  if (text !== undefined) d.textContent = text;
  return d;
}
function scrollDown(){
  chat.scrollTop = chat.scrollHeight;
}

/* ── 极简 Markdown ──
   模型是拿 Markdown 回答的，直接 textContent 会把 ** 和 ` 原样露出来。
   但这里有个坑：**必须先转义再替换**。
   模型输出属于不可信内容 —— 它完全可能吐出 <img src=x onerror=alert(1)>，
   直接塞 innerHTML 就等于在页面上开了个 XSS 口子。 */
function escHtml(s){
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function renderMd(text){
  let h = escHtml(text);
  h = h.replace(/^#{1,6}\s+(.+)$/gm, '<strong>$1</strong>');
  h = h.replace(/^\s*-{3,}\s*$/gm, '<span class="hr"></span>');
  h = h.replace(/\*\*([^*\n]+?)\*\*/g, '<strong>$1</strong>');
  h = h.replace(/`([^`\n]+?)`/g, '<code>$1</code>');
  return h;
}
function addUser(text){
  const m = el('msg user');
  m.appendChild(el('avatar', '我'));
  m.appendChild(el('bubble', text));
  chat.appendChild(m);
  scrollDown();
}
function addBot(){
  const m = el('msg bot');
  m.appendChild(el('avatar', 'AI'));
  const b = el('bubble streaming');
  m.appendChild(b);
  chat.appendChild(m);
  scrollDown();
  return { msg: m, bubble: b, stat: null };
}
function addTool(ev){
  const box = el('tool');
  const r1 = el('row1');
  r1.appendChild(el('nm', ev.name));
  r1.appendChild(el('ar', ev.args || '{}'));
  box.appendChild(r1);
  box.appendChild(el('br', '→ ' + ev.brief));
  const bot = chat.querySelector('.msg.bot:last-of-type');
  chat.insertBefore(box, bot);
  scrollDown();
}
function addStatus(text){
  const s = el('status');
  s.appendChild(el('spin'));
  s.appendChild(el('span', text));
  s.dataset.tmp = '1';
  const bot = chat.querySelector('.msg.bot:last-of-type');
  chat.insertBefore(s, bot);
  scrollDown();
  return s;
}
function clearStatus(){
  chat.querySelectorAll('.status[data-tmp]').forEach(n => n.remove());
}

/* ── 一条事件的处理器 ── */
function handle(ev, ui){
  switch (ev.t) {
    case 'round':
      clearStatus();
      if (ev.n > 1) addStatus('第 ' + ev.n + ' 轮：再想想…');
      break;
    case 'status':
      clearStatus();
      addStatus(ev.text);
      break;
    case 'delta':
      ui.bubble.textContent += ev.text;
      scrollDown();
      break;
    case 'retract':
      /* 它刚才说的只是"我先查一下"这类话术，撤掉 */
      ui.bubble.textContent = '';
      break;
    case 'tool':
      clearStatus();
      addTool(ev);
      break;
    case 'done': {
      clearStatus();
      ui.bubble.classList.remove('streaming');
      /* 流式时只能当纯文本追加（半截 ** 没法渲染），收完了再整体格式化一次 */
      ui.bubble.innerHTML = renderMd(ui.bubble.textContent);
      const parts = [];
      if (ev.status !== 'ok') {
        const map = {timeout:'超时', max_rounds:'达到轮次上限'};
        parts.push('⚠ ' + (map[ev.status] || ev.status));
      }
      parts.push(ev.elapsed + ' 秒');
      parts.push(ev.rounds + ' 轮');
      parts.push((ev.estimated ? '约 ' : '') + ev.tokens + ' tokens' + (ev.estimated ? '（估算）' : ''));
      ui.stat = el('stat', parts.join(' · '));
      ui.msg.appendChild(ui.stat);
      scrollDown();
      break;
    }
    case 'error': {
      clearStatus();
      ui.bubble.classList.remove('streaming');
      ui.bubble.classList.add('err');
      ui.bubble.textContent = '出错了：' + ev.message;
      break;
    }
  }
}

/* ── 主流程 ── */
async function ask(text){
  if (busy) return;
  busy = true; btn.disabled = true;
  addUser(text);
  const ui = addBot();

  try {
    const res = await fetch('/api/ask', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({q: text})
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);

    const reader = res.body.getReader();
    const dec = new TextDecoder('utf-8');
    let buf = '';

    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      buf += dec.decode(value, {stream: true});

      /* SSE 以空行分帧。一条事件可能被拆在两次 read 里，
         所以要缓冲到凑齐 "\n\n" 才处理 —— 这是手写 SSE 最容易错的地方。 */
      let i;
      while ((i = buf.indexOf('\n\n')) >= 0) {
        const frame = buf.slice(0, i);
        buf = buf.slice(i + 2);
        for (const line of frame.split('\n')) {
          if (!line.startsWith('data: ')) continue;
          let ev;
          try { ev = JSON.parse(line.slice(6)); } catch (e) { continue; }
          handle(ev, ui);
        }
      }
    }
  } catch (e) {
    ui.bubble.classList.remove('streaming');
    ui.bubble.classList.add('err');
    ui.bubble.textContent = '连接失败：' + e.message;
  } finally {
    ui.bubble.classList.remove('streaming');
    clearStatus();
    busy = false; btn.disabled = false; box.focus();
  }
}

function submit(){
  const t = box.value.trim();
  if (!t || busy) return;
  box.value = '';
  box.style.height = 'auto';
  ask(t);
}

btn.addEventListener('click', submit);
box.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit(); }
});
box.addEventListener('input', () => {
  box.style.height = 'auto';
  box.style.height = Math.min(box.scrollHeight, 140) + 'px';
});
document.querySelectorAll('.chip').forEach(c => {
  c.addEventListener('click', () => { box.value = c.textContent; submit(); });
});
document.getElementById('reset').addEventListener('click', async () => {
  await fetch('/api/reset', {method: 'POST'});
  chat.innerHTML = '';
  greeting();
});

/* ── 开场白 ── */
function greeting(){
  const m = el('msg bot');
  m.appendChild(el('avatar', 'AI'));
  const b = el('bubble');
  b.textContent =
    '你好。我手上有四个工具：查科室收入的、查库存告警清单的、按库存点查或排名的、查项目设计文档的。\n' +
    '数字我会去库里取，设计问题我会去文档里找，找不到我会直说。\n\n' +
    '下面几个问题可以直接点，试试看。';
  m.appendChild(b);
  chat.appendChild(m);
}
greeting();
box.focus();
</script>
</body>
</html>
"""


# ══════════════════════════════════════════════════════════════════════
# 六、启动
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    boot()
    print("\n" + "═" * 62)
    print(f"  打开浏览器： http://127.0.0.1:{PORT}")
    print("  健康检查：   curl http://127.0.0.1:%d/api/health" % PORT)
    print("  停止：       Ctrl+C")
    print("═" * 62 + "\n", flush=True)
    # threaded=True 必须开：SSE 是长连接，单线程会让第二个请求排队干等
    app.run(host="127.0.0.1", port=PORT, threaded=True, debug=False)
