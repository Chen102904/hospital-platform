# -*- coding: utf-8 -*-
"""
第 1 步：最小调用 —— 让你的 Python 代码跟大模型说上话。

跑法（在 ai_lab 目录下）：
    C:/Users/dell/.workbuddy/binaries/python/envs/default/Scripts/python.exe 01_hello.py

前提：已经把 mykey.py.example 复制成 mykey.py，填好了 API_URL / API_KEY / MODEL。
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

# Windows 终端输出中文的保险：强制用 utf-8，避免乱码报错
# 这里原本有一行 sys.stdout = io.TextIOWrapper(...)，已删除。原因：每 import
# 一次就新建一层包装，包第二次时前一层被回收、析构时把底层 buffer 关掉，
# 之后所有 print 都报 I/O operation on closed file。上面那段 reconfigure 已够用。

try:
    from mykey import API_KEY, API_URL, MODEL
except ImportError:
    sys.exit("✗ 没找到 mykey.py。先把 mykey.py.example 复制成 mykey.py，再填三个值。")

# PROXY 是后来才加的可选项，老版本 mykey.py 里没有这个变量，所以单独兜一下
try:
    from mykey import PROXY
except ImportError:
    PROXY = ""


def build_proxies():
    """把 mykey.PROXY 翻译成 requests 认识的 proxies 参数。

    requests 的规则很绕，这里帮你固定成三种情况，你只要在 mykey.py 里选一个：
        不填 / ""            -> 返回 None，requests 自己去读环境变量（默认行为）
        "none"               -> 显式告诉 requests"别走代理"，优先级高于环境变量
        "http://127.0.0.1:7897" -> 走这个代理（Clash 这类软件开着的时候用）
    """
    p = (PROXY or "").strip()
    if not p:
        return None
    if p.lower() == "none":
        # 关键：值给 None 而不是删掉 key，requests 才不会用环境变量补回来
        return {"http": None, "https": None}
    return {"http": p, "https": p}


def describe_proxy():
    """给用户看一眼当前走不走代理，出问题时不用猜。"""
    p = (PROXY or "").strip()
    if not p:
        return "跟随系统环境变量（如果 system 里设了 http_proxy，就会走它）"
    if p.lower() == "none":
        return "强制不走代理"
    return f"走指定代理 {p}"

missing = [n for n, v in [("API_URL", API_URL), ("API_KEY", API_KEY), ("MODEL", MODEL)]
           if not (v or "").strip()]
if missing:
    sys.exit(f"✗ mykey.py 里这几项还没填：{', '.join(missing)}")

# 复制粘贴最容易带进来首尾空格/换行，Authorization 头里多一个空格就是 401
_raw = {"API_URL": API_URL, "API_KEY": API_KEY, "MODEL": MODEL}
API_URL, API_KEY, MODEL = (v.strip() for v in (API_URL, API_KEY, MODEL))
_dirty = [k for k, v in _raw.items() if v != v.strip()]
if _dirty:
    print(f"（已自动去掉 {', '.join(_dirty)} 首尾的空白字符 —— 粘贴时带进来的）\n")

HINT = {
    401: "Key 错了或没带上。检查有没有多余空格、Bearer 有没有写重复。",
    402: "余额不足，去平台充值，或者换一个免费模型。",
    403: "没有权限，可能这个模型没开通。",
    404: "地址写错了。确认 URL 以 /chat/completions 结尾，不要多也不要少。",
    405: "这个地址不接受 POST —— 你填的很可能是文件下载地址或网页地址，不是模型接口。"
         "看响应里的 HostId：带 oss- 的是对象存储，绝对不对。回官方文档的「调用示例」里复制地址。",
    429: "请求太快被限流，等几十秒再跑。",
}


def chat(user_text, timeout=60):
    """最核心的 20 行：拼请求 → 发出去 → 解析结果。"""

    # 请求体：告诉模型用哪个模型、说什么
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": user_text}],
    }

    print("── 发出去的请求体 ──")
    print(json.dumps(body, ensure_ascii=False, indent=2))
    print()

    t0 = time.time()
    try:
        resp = requests.post(
            API_URL,
            headers={
                "Authorization": f"Bearer {API_KEY}",   # 钥匙：证明你是谁、花谁的钱
                "Content-Type": "application/json",     # 告诉对方"我发的是 JSON"
            },
            json=body,
            timeout=timeout,
            proxies=build_proxies(),
        )
    except requests.Timeout:
        print(f"✗ 超过 {timeout} 秒没响应。网络慢，或者这家服务当前不稳定。")
        return None
    except requests.exceptions.ProxyError as e:
        # 这个必须排在 ConnectionError 前面，因为 ProxyError 是它的子类
        print("✗ 代理连不上了（ProxyError）。")
        print("  意思：你机器上设了代理，但代理软件没开或者端口不对。")
        print("  两个办法，任选一个：")
        print('    ① 在 mykey.py 里写  PROXY = "none"         → 直连，不用代理')
        print('    ② 把代理软件打开，让它监听你填的那个端口')
        print("  原始错误：", str(e)[:200])
        return None
    except requests.ConnectionError as e:
        print("✗ 连不上服务器。常见原因：URL 写错、域名解析不了、或者这家需要代理。")
        print("  原始错误：", str(e)[:200])
        return None
    cost = time.time() - t0

    # 出错时把它翻译成人话
    if resp.status_code != 200:
        print(f"✗ HTTP {resp.status_code}")
        print(HINT.get(resp.status_code, "未收录的错误码，看下面的原始响应。"))
        print("原始响应：", resp.text[:500])
        return None

    data = resp.json()

    # 返回结构：choices 是数组，第一个元素的 message.content 就是回答
    answer = data["choices"][0]["message"]["content"]
    usage = data.get("usage") or {}

    print("── 模型回答 ──")
    print(answer)
    print()
    print(f"耗时：{cost:.1f} 秒")
    if usage:
        print(f"token 用量：prompt={usage.get('prompt_tokens')} "
              f"completion={usage.get('completion_tokens')} "
              f"total={usage.get('total_tokens')}")
        print("（prompt=你发过去的字数，completion=它回的字数，都是要计费的）")
    return answer


if __name__ == "__main__":
    print(f"目标地址：{API_URL}")
    print(f"使用模型：{MODEL}")
    print(f"网络代理：{describe_proxy()}\n")

    # 地址自检：提前挡住最常见的三种填错
    if "oss-" in API_URL:
        print("⚠️ 强烈提醒：地址里带 oss- ，这是阿里云对象存储（存文件的），绝对不是模型接口。")
        print("   OSS 只接受 GET 下载，你 POST 过去必然 405。\n")
    elif "chat/completions" not in API_URL:
        print("⚠️ 提醒：你填的地址里没有 chat/completions，很可能少了最后一段。")
        if API_URL.rstrip("/").endswith("/v1"):
            print(f'   看起来你填的是 Base URL。试试在后面补上：{API_URL.rstrip("/")}/chat/completions')
        print("   正确地址去官方文档的「调用示例」里复制，别从浏览器地址栏复制。\n")

    chat("用一句话解释什么是数据库索引。")
