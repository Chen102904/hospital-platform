# -*- coding: utf-8 -*-
"""
第 10 步 · 从"演示页"到"小产品"

第 9 步结束时它已经能用了：打一个问题，8 秒后带工具卡片答出来。
但一刷新页面，刚才那一整段就没了 —— 因为"记性"是个 Python 字典。
面试官问"能看看你之前问过什么吗"，你只能当场再问一遍。

这一步补三件产品该有、演示页不该有的东西：

  ① 持久化   会话和消息进 SQLite，重启不丢（store.py）
  ② 会话管理 侧栏列出历史、能改名、能删、能导出 Markdown
  ③ 复盘     每条回答都记下耗时 / 轮次 / token / 调了哪些工具 —— 于是
             打开一条旧会话，能看到"当时它是先查库再查文档的，花了 21 秒"

顺手做的事：把第 9 步那个 790 行的单文件拆成四层。
    store.py          存
    stream_engine.py  算
    app.py            路由 + 这一层的活儿（SSE、落库、鉴权边界）  ← 你在看的
    templates/ static/ 看

──────────────────────────────────────────────────────────────────────
这一层唯一新增的、值得记一笔的技术点：**落库的时机**。

写早写晚都是坑：

  写早了 → 跑到一半崩了，库里躺着一条"答到一半"的回答，
           下一轮模型看到它，会顺着半截往下编。
  写晚了 → 用户关了页面，整个生成器被 GC，这条记录直接消失。
           数据库里只剩一个孤零零的问题，永远没有答案。

所以时机是：
    · 用户消息：收到请求立刻写。他问了就是问了，哪怕答案没跑出来。
    · 回答消息：在生成器的 **finally** 里写。
      finally 一定会执行 —— 正常结束执行、抛异常执行、
      客户端断开（GeneratorExit）也执行。这是"一定会跑到"的唯一位置。

另外这里有个第 9 步没有的状态：**aborted**。
客户端中途关页面，生成器收到 GeneratorExit。这时候不能假装一切正常，
要把它记成 aborted，并且下一轮喂模型时跳过这一对（见 store.history_for_model）。

运行：
    C:/Users/dell/.workbuddy/binaries/python/envs/default/Scripts/python.exe app.py
然后浏览器打开 http://127.0.0.1:5002
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

import importlib.util
import json
import os
import sys
import time

from flask import (Flask, Response, abort, jsonify, redirect, render_template,
                   request, url_for)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # ai_lab/

# 08_rag_agent.py 里有 `import db_ro` 这类同级导入，所以 ROOT 得在 sys.path 上
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import store                          # noqa: E402
import stream_engine                  # noqa: E402

PORT = 5002                # 5000 给 hospital_web，5001 给第 9 步，这里用 5002
TRIM_KEEP = 8              # 每次喂给模型最近 8 条（4 组问答）
NAV_LIMIT = 40             # 侧栏最多列多少条会话


# ══════════════════════════════════════════════════════════════════════
# 一、加载第 8 步的 Agent 核心
# ══════════════════════════════════════════════════════════════════════

def load_core():
    path = os.path.join(ROOT, "08_rag_agent.py")
    if not os.path.exists(path):
        sys.exit("✗ 找不到 08_rag_agent.py，第 8 步还没做？")
    spec = importlib.util.spec_from_file_location("agent_core", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["agent_core"] = mod
    spec.loader.exec_module(mod)
    return mod


CORE = load_core()
stream_engine.init(CORE)


def boot():
    """进程启动时做一次昂贵的事：把 63 块文档向量化进内存。

    每请求重算要 2 秒，服务化之后这笔钱只能花一次。
    """
    chunks = CORE.build_chunks()
    CORE.IDX = CORE.DocIndex(chunks)
    store.init_db()
    print(f"知识库：{len(chunks)} 块")
    print(f"模型：{CORE.MODEL}")
    print(f"只读库账号：{CORE.DB_RO.get('user')}")


# ══════════════════════════════════════════════════════════════════════
# 二、Flask 应用
# ══════════════════════════════════════════════════════════════════════

app = Flask(__name__)
# Flask 2.3 之后 JSON_AS_ASCII 这个配置项作废了，得改 app.json。
# 不改的话接口返回的 {"title": "\u6536\u5165..."} 全是转义 —— 浏览器
# 能正确解析，但你 curl 一眼看过去以为坏了。
app.json.ensure_ascii = False


def sse(obj):
    """一条事件 = `data: ` + JSON + 两个换行。少一个换行浏览器就串帧。"""
    return "data: " + json.dumps(obj, ensure_ascii=False) + "\n\n"


@app.context_processor
def inject_nav():
    """每个页面都要的那份侧栏数据。

    只取有内容的会话 —— 否则"新对话"按钮点五次就多五个空条目。
    当前正在看的那个（哪怕还是空的）由 active_sid 单独补在最前面。
    """
    items = store.list_sessions(limit=NAV_LIMIT)
    return {
        "nav_sessions": items,
        "model_name": CORE.MODEL,
        "block_count": len(CORE.IDX.chunks) if CORE.IDX else 0,
        "db_user": CORE.DB_RO.get("user"),
    }


@app.get("/")
def home():
    """进来先看上次聊到哪儿了。没有就新建一个。"""
    sid = request.cookies.get("last_sid")
    if sid and store.get_session(sid):
        return redirect(url_for("chat_page", sid=sid))
    return redirect(url_for("new_chat"))


@app.get("/new")
def new_chat():
    s = store.create_session()
    return redirect(url_for("chat_page", sid=s["id"]))


@app.get("/c/<sid>")
def chat_page(sid):
    s = store.get_session(sid)
    if not s:
        abort(404)
    msgs = store.get_messages(sid)
    resp = app.make_response(
        render_template("chat.html", s=s, msgs=msgs, active_sid=sid, page="chat"))
    resp.set_cookie("last_sid", sid, max_age=90 * 86400, httponly=True, samesite="Lax")
    return resp


@app.get("/history")
def history_page():
    rows = store.list_sessions(limit=200, include_empty=True)
    st = store.stats()
    daily = store.daily_counts(7)
    peak = max([d["n"] for d in daily] + [1])
    return render_template("history.html", rows=rows, st=st, daily=daily,
                           peak=peak, page="history", active_sid=None)


# ══════════════════════════════════════════════════════════════════════
# 三、问答（SSE + 落库）
# ══════════════════════════════════════════════════════════════════════

@app.post("/api/ask")
def api_ask():
    data = request.get_json(silent=True) or {}
    q = (data.get("q") or "").strip()[:4000]
    sid = (data.get("sid") or "").strip()

    if not q:
        return jsonify({"error": "问题不能为空"}), 400
    if not store.get_session(sid):
        return jsonify({"error": "会话不存在，刷新页面重试"}), 404

    # ★ 顺序很重要：先取历史，再写当前这句。
    #   反过来的话，当前问题会同时出现在 history 和 question 里，
    #   模型会看到自己问了自己一遍。
    history = store.history_for_model(sid, keep=TRIM_KEEP)
    store.add_message(sid, "user", q)
    store.touch_session(sid, q)          # 顺便把标题按第一句话定下来

    def gen():
        parts = []               # 本轮累计的输出文字
        tool_events = []         # 本轮调过的工具（要写进库）
        status = "error"
        meta = {}
        t_start = time.time()    # ★ 客户端断开时 meta 是空的，得自己算

        try:
            for ev, payload in stream_engine.stream_agent(q, history):
                if ev == "delta":
                    parts.append(payload["text"])
                elif ev == "retract":
                    # 它刚才说的"我先查一下"被撤回了，这几个字不该进库，
                    # 否则历史记录里会留一句莫名其妙的前言
                    parts.clear()
                elif ev == "done":
                    status = payload.get("status") or "ok"
                    meta = payload
                    tool_events = payload.get("tools") or []
                yield sse({"t": ev, **payload})

        except GeneratorExit:
            # 客户端中途关页面。不能假装无事发生 —— 记成 aborted，
            # 下一轮 store.history_for_model 会自动跳过这一对。
            status = "aborted"
            raise

        except Exception as e:
            # 生成器里抛异常，SSE 会直接断掉，前端只看到"连接中断"。
            # 必须自己兜住，把错误当成正常事件发出去。
            status = "error"
            yield sse({"t": "error", "message": f"{type(e).__name__}: {e}"})
            yield sse({"t": "done", "status": "error",
                       "elapsed": meta.get("elapsed", 0), "rounds": 0,
                       "tokens": 0, "tools": tool_events})

        finally:
            # ★ 落库放在 finally —— 这是"一定会跑到"的唯一位置。
            #   正常结束、抛异常、客户端断开，三种情况都会走到这里。
            content = "".join(parts).strip()
            if content or status != "ok":
                try:
                    store.add_message(
                        sid, "assistant",
                        content or "（这次没有产生回答）",
                        elapsed=meta.get("elapsed") or round(time.time() - t_start, 1),
                        rounds=meta.get("rounds"),
                        tokens=meta.get("tokens"),
                        status=status,
                        tools=tool_events,
                    )
                except Exception as e:      # 库写不进去也不能砸了已经答完的内容
                    print(f"[warn] 落库失败：{type(e).__name__}: {e}", flush=True)

    resp = Response(gen(), mimetype="text/event-stream")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"   # 本地没用，上 nginx 能救命
    return resp


# ══════════════════════════════════════════════════════════════════════
# 四、会话管理 API
# ══════════════════════════════════════════════════════════════════════

@app.post("/api/session")
def api_new_session():
    s = store.create_session()
    return jsonify({"ok": True, "id": s["id"]}), 201


@app.patch("/api/session/<sid>")
def api_rename(sid):
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    if not title:
        return jsonify({"error": "标题不能为空"}), 400
    if not store.rename_session(sid, title):
        return jsonify({"error": "会话不存在"}), 404
    return jsonify({"ok": True, "title": title[:60]})


@app.delete("/api/session/<sid>")
def api_delete(sid):
    if not store.delete_session(sid):
        return jsonify({"error": "会话不存在"}), 404
    resp = jsonify({"ok": True})
    if request.cookies.get("last_sid") == sid:
        resp.delete_cookie("last_sid")
    return resp


@app.get("/api/session/<sid>/export")
def api_export(sid):
    md = store.export_markdown(sid)
    if md is None:
        return jsonify({"error": "会话不存在"}), 404
    fname = f"chat-{sid[:8]}.md"
    return Response(
        md, mimetype="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/api/session/<sid>/messages")
def api_messages(sid):
    if not store.get_session(sid):
        return jsonify({"error": "会话不存在"}), 404
    return jsonify({"ok": True, "messages": store.get_messages(sid)})


@app.get("/api/sessions")
def api_sessions():
    return jsonify({"ok": True, "sessions": store.list_sessions(limit=200)})


@app.get("/api/stats")
def api_stats():
    return jsonify({"ok": True, "stats": store.stats(), "daily": store.daily_counts(7)})


@app.get("/api/health")
def api_health():
    return jsonify({
        "ok": True,
        "model": CORE.MODEL,
        "blocks": len(CORE.IDX.chunks) if CORE.IDX else 0,
        "db": store.DB_PATH,
        "sessions": store.count_sessions(),
        "messages": store.count_messages(),
        "tools": [t["function"]["name"] for t in CORE.TOOLS],
    })


@app.errorhandler(404)
def on_404(e):
    return render_template("404.html", active_sid=None, page="404"), 404


# ══════════════════════════════════════════════════════════════════════
# 五、启动
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    boot()
    print("\n" + "═" * 62)
    print(f"  对话：  http://127.0.0.1:{PORT}")
    print(f"  记录：  http://127.0.0.1:{PORT}/history")
    print(f"  体检：  http://127.0.0.1:{PORT}/api/health")
    print("═" * 62 + "\n", flush=True)
    # threaded=True 必须开：SSE 是长连接，单线程会让第二个请求排队干等
    app.run(host="127.0.0.1", port=PORT, threaded=True, debug=False)
