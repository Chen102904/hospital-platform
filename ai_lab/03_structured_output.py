# -*- coding: utf-8 -*-
"""
第 3 步：结构化输出 —— 让模型回的话，代码能直接吃。

为什么这一步是 Agent 的分水岭：
    第 2 步模型回的是"给人看的散文"，你能读，程序读不了。
    Agent 要干活，就必须让模型回"给程序看的数据"。
    后面第 4 步的 function calling，底层就是"让模型输出一段特定格式的 JSON"。

本脚本四个实验（都在你机器上实跑过，输出见 README）：
    A  只在提示词里写"请输出 JSON"        -> 它会给你包一层 ```json 围栏，json.loads 直接报错
    B  用 response_format=json_object     -> 干净 JSON，能解析
    C  用 response_format=json_schema     -> 干净 JSON，而且类型被强制（age 是 int 不是 "35岁"）
    D  真实场景：一段门诊病历 -> 结构化字段 -> 拼 SQL 的安全写法

跑法（在 ai_lab 目录下）：
    C:/Users/dell/.workbuddy/binaries/python/envs/default/Scripts/python.exe 03_structured_output.py
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
import re
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
# 一、调用层
# ══════════════════════════════════════════════════════════
def call(messages, response_format=None, timeout=120):
    """在 02 的基础上多一个 response_format 参数。

    注意：这个平台的返回里有两个字段
        message.content           -> 真正的答案
        message.reasoning_content -> 它的思考过程（推理模型才有，别拿这个去解析）
    """
    body = {"model": MODEL, "messages": messages}
    if response_format:
        body["response_format"] = response_format

    resp = requests.post(
        API_URL,
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        json=body,
        timeout=timeout,
        proxies=build_proxies(),
    )
    if resp.status_code != 200:
        print(f"  ✗ HTTP {resp.status_code}: {resp.text[:300]}")
        return None
    return resp.json()["choices"][0]["message"].get("content") or ""


# ══════════════════════════════════════════════════════════
# 二、解析层（生产代码里必须有的兜底）
# ══════════════════════════════════════════════════════════
def safe_json_parse(text):
    """从模型回复里抠出 JSON。返回 (对象, 错误说明)。

    为什么要这么麻烦：即使你要求了 JSON，真实世界里还是可能拿到
        ```json
        {...}
        ```
    或者前后带一句"好的，结果如下："。这类脏活必须在这一层挡掉。
    """
    if not text:
        return None, "回复是空的"
    s = text.strip()

    # 情况 1：整段就是合法 JSON
    try:
        return json.loads(s), None
    except json.JSONDecodeError:
        pass

    # 情况 2：被 markdown 代码块包住了
    m = re.search(r"```(?:json)?\s*(.+?)\s*```", s, re.S)
    if m:
        try:
            return json.loads(m.group(1)), None
        except json.JSONDecodeError as e:
            return None, f"代码块里的内容不是合法 JSON：{e}"

    # 情况 3：前后有废话，抠第一个 { 到最后一个 }
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j > i:
        try:
            return json.loads(s[i:j + 1]), None
        except json.JSONDecodeError as e:
            return None, f"截取大括号区间后仍不是合法 JSON：{e}"

    return None, "整段里找不到 JSON 结构"


# ══════════════════════════════════════════════════════════
# 实验 A：只靠提示词要求 JSON —— 会翻车
# ══════════════════════════════════════════════════════════
def exp_a():
    print("=" * 68)
    print("实验 A：只在提示词里写“请输出 JSON”")
    print("=" * 68)

    text = call([{"role": "user",
                  "content": '把这句话抽成 JSON：张三，男，35岁，主诉头痛三天。'
                             '字段用 name/gender/age/chief_complaint'}])

    print("\n模型原样返回（注意看 repr 里的 \\n，这是真实字符）：")
    print(repr(text)[:400])

    try:
        json.loads(text)
        print("\n→ 侥幸解析成功了。但这不是保证，换个模型/换个问法就会翻车。")
    except json.JSONDecodeError as e:
        print(f"\n✗ json.loads 报错：{e}")
        print("  原因：它给你包了一层 ```json 围栏，那三个反引号不是 JSON 的一部分。")
        print("  这是最常见的一种翻车，联网搜『LLM 返回 markdown 代码块』一大片。")

    obj, err = safe_json_parse(text)
    print("\n用 safe_json_parse 兜一下：", "成功 ->" if obj else "仍失败 ->", obj or err)
    print("\n结论：① 一定要加 response_format；② 解析层仍要有兜底。\n")


# ══════════════════════════════════════════════════════════
# 实验 B：json_object —— 保证"是 JSON"
# ══════════════════════════════════════════════════════════
def exp_b():
    print("=" * 68)
    print("实验 B：response_format = json_object")
    print("=" * 68)

    text = call(
        [{"role": "user",
          "content": '把这句话抽成 JSON：张三，男，35岁，主诉头痛三天。'
                     '字段用 name/gender/age/chief_complaint'}],
        response_format={"type": "json_object"},
    )
    print("\n模型原样返回：")
    print(repr(text)[:400])

    obj, err = safe_json_parse(text)
    if obj:
        print("\n✓ 解析成功：", obj)
        print(f"  age 的值是 {obj.get('age')!r}，类型 {type(obj.get('age')).__name__}")
    else:
        print("\n✗ 失败：", err)
    print("\n注意：json_object 只保证“这是一段合法 JSON”，不保证字段对不对、类型对不对。\n")


# ══════════════════════════════════════════════════════════
# 实验 C：json_schema —— 连字段和类型都强制
# ══════════════════════════════════════════════════════════
PATIENT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "gender": {"type": "string", "enum": ["男", "女"]},
        "age": {"type": "integer"},
        "chief_complaint": {"type": "string"},
        "diagnosis": {"type": "string"},
        "medicines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "dose": {"type": "string"},
                    "frequency": {"type": "string"},
                    "days": {"type": "integer"},
                },
                "required": ["name", "dose", "frequency", "days"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["name", "gender", "age", "chief_complaint", "diagnosis", "medicines"],
    "additionalProperties": False,
}


def exp_c(text=None, verbose=True):
    """返回解析好的 dict，供实验 D 直接用。"""
    if verbose:
        print("=" * 68)
        print("实验 C：response_format = json_schema（带字段清单和类型）")
        print("=" * 68)

    case = text or "张三，男，35岁，主诉头痛三天，初步诊断偏头痛，开布洛芬缓释胶囊 0.3g 每日两次 共3天"
    raw = call(
        [{"role": "system", "content": "你是病历结构化助手。严格按给定 schema 输出，不要额外解释。"},
         {"role": "user", "content": f"抽取以下门诊记录的信息：\n{case}"}],
        response_format={"type": "json_schema",
                         "json_schema": {"name": "patient_record",
                                         "strict": True,
                                         "schema": PATIENT_SCHEMA}},
    )
    obj, err = safe_json_parse(raw)
    if verbose:
        if obj:
            print("\n✓ 结构化结果：")
            print(json.dumps(obj, ensure_ascii=False, indent=2))
            print(f"\n  age = {obj['age']!r} (int)  ← 就算原文写“35岁”，schema 也逼它输整数")
            print("  medicines 是数组       ← 是一张表，不是一个字符串，可以直接 for 循环入库")
        else:
            print("\n✗ 失败：", err, "\n原始返回：", repr(raw)[:300])
        print()
    return obj


# ══════════════════════════════════════════════════════════
# 实验 D：拿它干真活 —— 病历 -> 结构化 -> SQL 的安全写法
# ══════════════════════════════════════════════════════════
def exp_d():
    print("=" * 68)
    print("实验 D：真实场景（对应你自己的 HIS 项目）")
    print("=" * 68)

    record = (
        "患者李建国，男，52岁。主诉反复上腹隐痛两周，餐后加重。"
        "查体上腹轻压痛。初步诊断：慢性胃炎。"
        "处方：奥美拉唑肠溶胶囊 20mg 每日一次 共14天；铝碳酸镁片 0.5g 每日三次 共14天。"
    )
    print("\n【输入】一段自由文本病历：")
    print("  " + record)

    obj = exp_c(record, verbose=False)
    if not obj:
        return

    print("\n【模型输出】结构化后：")
    print(json.dumps(obj, ensure_ascii=False, indent=2))

    # ── 第一道闸：字段白名单校验 ──
    # 模型可能凭空多给你一个字段（比如 patient_id、hospital 之类），
    # 如果代码直接拿它的 key 去拼 SQL，就等于让模型控制 SQL 结构了。
    ALLOWED = set(PATIENT_SCHEMA["properties"].keys())
    extra = set(obj.keys()) - ALLOWED
    if extra:
        print(f"\n⚠️ 模型多给了这些字段，已丢弃：{extra}")
        obj = {k: v for k, v in obj.items() if k in ALLOWED}

    # ── 第二道闸：值一律走参数化，不拼字符串 ──
    sql = ("INSERT INTO patient (patient_name, gender, age) "
           "VALUES (%s, %s, %s)")
    values = [obj["name"], obj["gender"], obj["age"]]

    print("\n【生成 SQL】（只打印，不执行）")
    print("  " + sql)
    print(f"  参数：{values}")
    print("\n也可以拼成人能读的样子，方便你核对：")
    shown = sql
    for v in values:
        shown = shown.replace("%s", f"'{v}'", 1)
    print("  " + shown)

    print("\n" + "-" * 68)
    print("⚠️ 三条红线：")
    print("  1. 绝不把模型输出的**字段名**直接当 SQL 列名 —— 只能从白名单里取")
    print("  2. 绝不把模型输出的**值**直接拼进 SQL 字符串 —— 一律参数化（%s 占位）")
    print("  3. 模型给的数字要自己复核（type 是不是 int、age 会不会是 350）")
    print("  模型输出属于**不可信输入**，跟用户提交的表单是同一个信任级别。")
    print("-" * 68)


if __name__ == "__main__":
    print(f"模型：{MODEL}\n")
    exp_a()
    exp_b()
    exp_c()
    exp_d()
    print("\n" + "=" * 68)
    print("第 3 步完成。一句话总结：")
    print("  “请输出 JSON”是请求，不是保证。要保证就用 response_format；")
    print("  而且在解析层和入库前，各留一道兜底。")
    print("=" * 68)
