# -*- coding: utf-8 -*-
"""
医院运营异常归因 Agent。

和「智能问数」的区别（面试要讲的核心）：
  智能问数：你问「7 月挂号多少」，它查一个数给你 —— 路径固定，写死 SQL 就行。
  本 Agent：你问「7 月为什么掉量」，它自己决定查几步、查哪些维度，
            且第 N 步查什么取决于第 N-1 步查出了什么。

为什么必须多步（举例）：
  「总量降了 12%」这个现象本身没有信息量，必须拆到科室才知道是谁拖的；
  拆到科室后，还要再拆「是没人出诊、还是人不来、还是来了又退」这三种完全不同的原因；
  而查哪一种，取决于前面观察到的指标异常方向。

决策层做成可替换的 Brain：
  LLMBrain      —— 有 API 额度时，由模型决定调哪个工具
  HeuristicBrain—— 无额度时的内置归因策略，按「观察到什么→决定下一步」分支
两者共用同一套 tool schema 和同一套执行循环，切换只换决策来源。
"""
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

import pymysql

DB = dict(host="127.0.0.1", user="root", password="his_dev_only",
          charset="utf8mb4", database="hospital_ops")

CANCEL = ("已取消", "已退费")


def conn():
    return pymysql.connect(**DB)


def q(sql, args=None):
    c = conn()
    try:
        cur = c.cursor(pymysql.cursors.DictCursor)
        cur.execute(sql, args)
        return cur.fetchall()
    finally:
        c.close()


# ============================================================ 工具层
# 全部只读。每个工具的 description 写清楚「能答什么、答不了什么」，
# 因为模型选错工具最常见的原因是描述含糊。

TOOLS = [
    {"type": "function", "function": {
        "name": "monthly_summary",
        "description": ("查某个月的运营总览：有效挂号量、退号率、号源使用率、在岗医生数。"
                        "用于确认「现象是否真实存在」，以及和上一个月对比。"),
        "parameters": {"type": "object", "properties": {
            "month": {"type": "string", "description": "月份，格式 YYYY-MM，如 2026-07"},
        }, "required": ["month"], "additionalProperties": False},
    }},
    {"type": "function", "function": {
        "name": "dept_breakdown",
        "description": ("查某个月按科室拆开的情况，一次只返回一种指标。三个指标含义不同："
                        "volume=有效挂号量（谁的人少了）；"
                        "cancel_rate=退号率（人来了又退，量可能正常）；"
                        "quota_usage=号源使用率（号放出去了但没人挂，说明号源浪费）。"
                        "总量正常但怀疑有问题时，要换成 cancel_rate 或 quota_usage 再看一遍。"),
        "parameters": {"type": "object", "properties": {
            "month": {"type": "string", "description": "月份 YYYY-MM"},
            "metric": {"type": "string", "enum": ["volume", "cancel_rate", "quota_usage"]},
        }, "required": ["month", "metric"], "additionalProperties": False},
    }},
    {"type": "function", "function": {
        "name": "dept_month_detail",
        "description": ("查某个科室某个月的明细：在岗医生名单、每人排班天数、每人号源使用率、"
                        "退号率、与上月对比。用于在锁定可疑科室后，判断到底是"
                        "「医生少了」「医生出诊少了」还是「号放了没人挂」。"),
        "parameters": {"type": "object", "properties": {
            "dept_name": {"type": "string", "description": "科室名，如 内科"},
            "month": {"type": "string", "description": "月份 YYYY-MM"},
        }, "required": ["dept_name", "month"], "additionalProperties": False},
    }},
    {"type": "function", "function": {
        "name": "trend",
        "description": ("查 12 个月的趋势序列，用于判断单月异常是偶发还是持续、"
                        "以及是不是全院普降（全院普降通常是季节性因素）。"),
        "parameters": {"type": "object", "properties": {
            "dept_name": {"type": "string", "description": "留空=全院"},
        }, "additionalProperties": False},
    }},
    {"type": "function", "function": {
        "name": "calendar_context",
        "description": ("查某个月的日历背景：工作日天数、是否含春节/国庆长假。"
                        "用于排除「季节性正常波动」，避免把节假日当成业务问题上报。"),
        "parameters": {"type": "object", "properties": {
            "month": {"type": "string", "description": "月份 YYYY-MM"},
        }, "required": ["month"], "additionalProperties": False},
    }},
]

HOLIDAYS = {
    "2026-02": "春节（2/15–2/21 长假）",
    "2026-10": "国庆（10/1–10/7 长假）",
    "2026-05": "劳动节（5/1–5/5 小长假）",
}


def _month_range(month: str):
    y, m = map(int, month.split("-"))
    start = f"{y:04d}-{m:02d}-01"
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    end = f"{ny:04d}-{nm:02d}-01"
    return start, end


def t_monthly_summary(month: str) -> dict:
    s, e = _month_range(month)
    row = q("""SELECT COUNT(*) n,
                      SUM(reg_status IN ('已取消','已退费')) cancels,
                      COUNT(DISTINCT dept_id) depts
               FROM registration WHERE reg_date >= %s AND reg_date < %s""", (s, e))[0]
    if not row["n"]:
        return {"ok": False, "error": f"{month} 无数据"}
    eff = row["n"] - (row["cancels"] or 0)
    quota = q("""SELECT COALESCE(SUM(total_quota),0) t FROM doctor_schedule
                 WHERE work_date >= %s AND work_date < %s""", (s, e))[0]["t"]
    docs = q("""SELECT COUNT(DISTINCT s.doctor_id) n FROM doctor_schedule s
                WHERE s.work_date >= %s AND s.work_date < %s""", (s, e))[0]["n"]
    return {"ok": True, "month": month, "有效挂号": int(eff),
            "总挂号": int(row["n"]), "退号率%": round((row["cancels"] or 0) / row["n"] * 100, 1),
            "号源使用率%": round(eff / quota * 100, 1) if quota else 0,
            "出诊医生数": int(docs)}


def t_dept_breakdown(month: str, metric: str = "volume") -> dict:
    """按科室拆某月指标，同时给出「该科室自己前 11 个月的中位数」作为基线。

    为什么要基线：科室规模不同，横向比毫无意义。2 个医生的小科室月挂号 400 例，
    4 个医生的内科 1200 例，直接比大小只会选出大科室。正确做法是每个科室
    跟自己的历史比，看偏离了多少。
    """
    def metrics(row):
        total = int(row["total"] or 0)
        cancels = int(row["cancels"] or 0)
        eff = total - cancels
        qt = int(row["quota"] or 0)
        return {"volume": float(eff),
                "cancel_rate": round(cancels / total * 100, 1) if total else 0.0,
                "quota_usage": round(eff / qt * 100, 1) if qt else 0.0,
                "eff": eff, "cancel_rate_": round(cancels / total * 100, 1) if total else 0.0,
                "quota_usage_": round(eff / qt * 100, 1) if qt else 0.0}

    rows = q("""SELECT d.dept_name,
                       LEFT(r.reg_date,7) m,
                       COUNT(r.reg_id) total,
                       SUM(r.reg_status IN ('已取消','已退费')) cancels
                FROM registration r JOIN department d USING(dept_id)
                GROUP BY d.dept_name, m""")
    quota = {(r["dept_name"], r["m"]): int(r["t"] or 0) for r in q(
        """SELECT d.dept_name, LEFT(s.work_date,7) m, SUM(s.total_quota) t
           FROM doctor_schedule s JOIN doctor doc ON doc.doctor_id=s.doctor_id
           JOIN department d USING(dept_id) GROUP BY d.dept_name, m""")}

    by_dept: dict[str, dict[str, dict]] = {}
    for r in rows:
        r["quota"] = quota.get((r["dept_name"], r["m"]), 0)
        by_dept.setdefault(r["dept_name"], {})[r["m"]] = metrics(r)

    # 单人级号源使用率：科室整体会被正常医生稀释，单看科室均值会漏掉「一个医生停诊」这种情况
    # 拆成两个聚合查询再在 Python 里拼，避免相关子查询触发 only_full_group_by
    doc_names = {r["doctor_id"]: r["doctor_name"] for r in
                 q("SELECT doctor_id, doctor_name FROM doctor")}
    qmap = {(r["dept_name"], r["m"], r["doctor_id"]): int(r["quota"] or 0) for r in q(
        """SELECT d.dept_name, LEFT(s.work_date,7) m, s.doctor_id, SUM(s.total_quota) quota
           FROM doctor_schedule s JOIN doctor doc ON doc.doctor_id=s.doctor_id
           JOIN department d USING(dept_id)
           GROUP BY d.dept_name, m, s.doctor_id""")}
    amap = {(r["dept_name"], r["m"], r["doctor_id"]): int(r["n"] or 0) for r in q(
        """SELECT d.dept_name, LEFT(r.reg_date,7) m, r.doctor_id, COUNT(*) n
           FROM registration r JOIN department d USING(dept_id)
           WHERE r.reg_status NOT IN ('已取消','已退费')
           GROUP BY d.dept_name, m, r.doctor_id""")}
    per_doc = {}
    for (dept, mm, did_), qt in qmap.items():
        if qt <= 0:
            continue
        per_doc[(dept, mm)] = per_doc.get((dept, mm), []) + [{
            "doctor_name": doc_names.get(did_, str(did_)),
            "quota": qt, "actual": amap.get((dept, mm, did_), 0)}]

    import statistics
    out = []
    for dept, months in by_dept.items():
        cur = months.get(month)
        if not cur:
            continue
        hist = [v[metric] for k, v in months.items() if k != month]
        base = statistics.median(hist) if hist else cur[metric]
        dev = (cur[metric] - base) / base * 100 if base else 0
        row = {"科室": dept, "值": cur[metric], "基线中位数": round(base, 1),
               "偏离%": round(dev, 1),
               "有效挂号": cur["eff"], "退号率%": cur["cancel_rate_"],
               "号源使用率%": cur["quota_usage_"]}
        if metric == "quota_usage":
            dorows = per_doc.get((dept, month), [])
            if dorows:
                worst = min(dorows, key=lambda v: v["actual"] / v["quota"])
                row["最低单人使用率%"] = round(worst["actual"] / worst["quota"] * 100, 1)
                row["异常医生"] = worst["doctor_name"]
        out.append(row)
    # 号源维度按「最低单人使用率」排序，其余按偏离幅度排序
    if metric == "quota_usage":
        out.sort(key=lambda x: x.get("最低单人使用率%", 999))
    else:
        out.sort(key=lambda x: abs(x["偏离%"]), reverse=True)
    return {"ok": True, "month": month, "metric": metric, "rows": out}


def t_dept_month_detail(dept_name: str, month: str) -> dict:
    s, e = _month_range(month)
    y, m = map(int, month.split("-"))
    pm = f"{y-1}-12" if m == 1 else f"{y}-{m-1:02d}"
    ps, pe = _month_range(pm)

    def agg(a, b):
        r = q("""SELECT COUNT(r.reg_id) total, SUM(r.reg_status IN ('已取消','已退费')) c
                 FROM registration r JOIN department d USING(dept_id)
                 WHERE d.dept_name=%s AND r.reg_date>=%s AND r.reg_date<%s""", (dept_name, a, b))[0]
        qt = q("""SELECT COALESCE(SUM(s.total_quota),0) t FROM doctor_schedule s
                  JOIN doctor doc ON doc.doctor_id=s.doctor_id JOIN department d USING(dept_id)
                  WHERE d.dept_name=%s AND s.work_date>=%s AND s.work_date<%s""", (dept_name, a, b))[0]["t"]
        docs = q("""SELECT COUNT(DISTINCT s.doctor_id) n FROM doctor_schedule s
                    JOIN doctor doc ON doc.doctor_id=s.doctor_id JOIN department d USING(dept_id)
                    WHERE d.dept_name=%s AND s.work_date>=%s AND s.work_date<%s""", (dept_name, a, b))[0]["n"]
        total = r["total"] or 0
        eff = total - (r["c"] or 0)
        return {"有效挂号": int(eff), "退号率%": round((r["c"] or 0) / total * 100, 1) if total else 0,
                "号源使用率%": round(eff / qt * 100, 1) if qt else 0, "出诊医生数": int(docs)}

    cur_m, prev_m = agg(s, e), agg(ps, pe)
    per_doc = q("""SELECT doc.doctor_name, doc.status,
                          COUNT(DISTINCT s.schedule_id) days,
                          COALESCE(SUM(s.total_quota),0) quota,
                          (SELECT COUNT(*) FROM registration r
                           WHERE r.doctor_id=doc.doctor_id AND r.reg_date>=%s AND r.reg_date<%s
                             AND r.reg_status NOT IN ('已取消','已退费')) actual
                   FROM doctor_schedule s JOIN doctor doc ON doc.doctor_id=s.doctor_id
                   JOIN department d USING(dept_id)
                   WHERE d.dept_name=%s AND s.work_date>=%s AND s.work_date<%s
                   GROUP BY doc.doctor_id ORDER BY days DESC""", (s, e, dept_name, s, e))
    return {"ok": True, "科室": dept_name, "本月": month, "本月指标": cur_m,
            "上月": pm, "上月指标": prev_m,
            "本月每人排班": [{
                "医生": r["doctor_name"], "状态": r["status"],
                "排班天数": r["days"], "放号数": int(r["quota"] or 0),
                "实际挂号": int(r["actual"] or 0),
                "使用率%": round(int(r["actual"] or 0) / int(r["quota"]) * 100, 1) if r["quota"] else 0
            } for r in per_doc]}


def t_trend(dept_name: str = "") -> dict:
    if dept_name:
        rows = q("""SELECT LEFT(r.reg_date,7) m,
                           COUNT(*) total, SUM(r.reg_status IN ('已取消','已退费')) c
                    FROM registration r JOIN department d USING(dept_id)
                    WHERE d.dept_name=%s GROUP BY m ORDER BY m""", (dept_name,))
    else:
        rows = q("""SELECT LEFT(reg_date,7) m,
                           COUNT(*) total, SUM(reg_status IN ('已取消','已退费')) c
                    FROM registration GROUP BY m ORDER BY m""")
    return {"ok": True, "对象": dept_name or "全院",
            "序列": [{"月": r["m"], "有效挂号": int(r["total"] - (r["c"] or 0)),
                     "退号率%": round((r["c"] or 0) / r["total"] * 100, 1)} for r in rows]}


def t_calendar_context(month: str) -> dict:
    s, e = _month_range(month)
    days = q("""SELECT COUNT(DISTINCT work_date) d FROM doctor_schedule
                WHERE work_date>=%s AND work_date<%s""", (s, e))[0]["d"]
    return {"ok": True, "month": month, "有排班天数": int(days),
            "节假日": HOLIDAYS.get(month, "无")}


EXEC = {
    "monthly_summary": t_monthly_summary,
    "dept_breakdown": t_dept_breakdown,
    "dept_month_detail": t_dept_month_detail,
    "trend": t_trend,
    "calendar_context": t_calendar_context,
}


def call_tool(name: str, args: dict) -> dict:
    fn = EXEC.get(name)
    if not fn:
        return {"ok": False, "error": f"未知工具 {name}"}
    try:
        return fn(**args)
    except TypeError as ex:
        return {"ok": False, "error": f"参数不对: {ex}"}
    except Exception as ex:
        return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}


# ============================================================ 决策层
@dataclass
class Action:
    tool: str
    args: dict


class HeuristicBrain:
    """内置归因策略：无模型额度时的决策来源。

    注意它不是「一条写死的路径」，而是按观察到的指标分支：
      总量降 → 找哪个科室降
      该科室退号率高 → 报退号率问题
      该科室号源使用率低 → 报号源浪费
      该科室医生数减少 → 报人力减少
      全院普降 + 节假日 → 判为正常波动，不上报
    """

    name = "heuristic"

    def __init__(self):
        self.done: set[str] = set()
        self.ctx: dict = {}
        self.detail_idx = 0

    def decide(self, month: str, history: list[dict]) -> Action | None:
        # 阶段 1：确认现象
        if "summary" not in self.done:
            self.done.add("summary")
            return Action("monthly_summary", {"month": month})
        # 阶段 2：看全院趋势，判断是不是普降
        if "trend" not in self.done:
            self.done.add("trend")
            return Action("trend", {})
        # 阶段 3：三个维度依次拆（量、退号、号源）
        for metric in ("volume", "cancel_rate", "quota_usage"):
            if metric not in self.done:
                self.done.add(metric)
                return Action("dept_breakdown", {"month": month, "metric": metric})
        # 阶段 4：先查日历。这一步放在钻科室细节之前是有意的——
        #         如果是节假日导致的全院普降，就不该再逐个科室查下去，否则会报一堆假问题。
        if "cal" not in self.done:
            self.done.add("cal")
            return Action("calendar_context", {"month": month})
        if "_seasonal" not in self.done:
            self.done.add("_seasonal")
            cal = next((h["result"] for h in history if h["tool"] == "calendar_context"), {})
            sm = next((h["result"] for h in history if h["tool"] == "monthly_summary"), {})
            tr = next((h["result"] for h in history if h["tool"] == "trend"), {})
            seq = [s for s in (tr.get("序列") or []) if s["月"] < month]
            if seq and sm.get("有效挂号"):
                drop = (seq[-1]["有效挂号"] - sm["有效挂号"]) / max(seq[-1]["有效挂号"], 1)
                if drop > 0.15 and cal.get("节假日") not in (None, "无", ""):
                    self.ctx["seasonal"] = {"drop": drop, "holiday": cal["节假日"]}
                    return None       # 已可解释，不再往下钻
        # 阶段 5：对每个可疑科室逐个查明细
        susp = self.ctx.get("suspects") or []
        if self.detail_idx < len(susp):
            dept = susp[self.detail_idx]
            self.detail_idx += 1
            return Action("dept_month_detail", {"dept_name": dept, "month": month})
        return None


def _jsonable(o):
    """工具结果里有 Decimal（COUNT/SUM 返回），直接 json.dumps 会报错。"""
    import datetime as _dt
    import decimal as _dec
    if isinstance(o, _dec.Decimal):
        return float(o)
    if isinstance(o, (_dt.date, _dt.datetime)):
        return o.isoformat()
    return str(o)


class LLMBrain:
    """有额度时用：把同样的 tool schema 交给模型，由模型决定下一步。

    和 HeuristicBrain 共用同一套 Action 协议，所以 run_agent 的循环、刹车、
    工具执行完全不需要改——切换只换决策来源。
    """

    name = "llm"

    def __init__(self):
        import sys
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ai_lab"))
        import mykey as K
        import requests
        self.K, self.requests = K, requests
        self.messages = [{"role": "system", "content": (
            "你是医院运营分析助手。用户问某月为什么指标异常，你要用工具一步步查证，"
            "不能凭猜测回答。\n"
            "硬性要求：下结论前必须把三个维度都查过一遍——"
            "①挂号量（volume）②退号率（cancel_rate）③号源使用率（quota_usage）。"
            "少查任何一个都不许下结论，因为问题可能藏在任何一个维度里。\n"
            "注意：科室之间规模不同，要跟该科室自己的历史基线比，不要横向比绝对量。"
            "如果发现是节假日导致的正常波动，要明确说这不是问题。"
            "查完后给出结论和证据链，并说明哪些是真问题、哪些是正常波动。")}]
        self.started = False
        self.last_call_id = None
        self.final_text = None
        self.usage = {"prompt": 0, "completion": 0}

    def decide(self, month: str, history: list[dict]):
        if not self.started:
            self.messages.append({"role": "user",
                                  "content": f"{month} 门诊量有异常，帮我查一下是什么原因。"})
            self.started = True
        elif self.last_call_id and history:
            # 工具结果必须用上一轮真实的 tool_call_id 回灌，否则 API 直接 400
            self.messages.append({
                "role": "tool",
                "tool_call_id": self.last_call_id,
                "content": json.dumps(history[-1]["result"], ensure_ascii=False,
                                      default=_jsonable)[:3000],
            })
            self.last_call_id = None

        r = self.requests.post(
            self.K.API_URL,
            headers={"Authorization": f"Bearer {self.K.API_KEY}",
                     "Content-Type": "application/json"},
            json={"model": self.K.MODEL, "messages": self.messages,
                  "tools": TOOLS, "tool_choice": "auto"},
            timeout=120, proxies={"http": None, "https": None})
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        u = data.get("usage") or {}
        self.usage["prompt"] += u.get("prompt_tokens", 0)
        self.usage["completion"] += u.get("completion_tokens", 0)

        msg = data["choices"][0]["message"]
        tcs = msg.get("tool_calls") or []
        if not tcs:
            # 模型不再调工具，说明它认为查完了，把最终回答留下
            self.final_text = msg.get("content") or ""
            return None

        # 只执行第一个 tool_call。如果返回多个，必须把 assistant 消息裁剪成只含这一个，
        # 否则下一轮会因为「有 tool_call 没有对应 tool 响应」被 API 拒绝。
        tc = tcs[0]
        msg["tool_calls"] = [tc]
        self.messages.append(msg)
        self.last_call_id = tc["id"]
        return Action(tc["function"]["name"],
                      json.loads(tc["function"]["arguments"] or "{}"))

    def finalize(self) -> str:
        """轮次用完但模型还没给结论时，强制让它基于已有查询结果收尾。

        不做这一步的后果：模型查了一堆数据却没输出结论，最后落到启发式分支，
        而启发式在 LLM 模式下没有累积线索，会误报「未发现异常」。
        """
        if self.final_text:
            return self.final_text
        self.messages.append({"role": "user", "content": (
            "停止查询，基于上面已经查到的数据给出结论："
            "①哪些是真问题，证据是什么；②哪些是正常波动、为什么；"
            "③如果数据不足以下结论，直接说不足在哪里。")})
        r = self.requests.post(
            self.K.API_URL,
            headers={"Authorization": f"Bearer {self.K.API_KEY}",
                     "Content-Type": "application/json"},
            json={"model": self.K.MODEL, "messages": self.messages},
            timeout=120, proxies={"http": None, "https": None})
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        u = data.get("usage") or {}
        self.usage["prompt"] += u.get("prompt_tokens", 0)
        self.usage["completion"] += u.get("completion_tokens", 0)
        self.final_text = (data["choices"][0]["message"].get("content") or "").strip()
        return self.final_text


# ============================================================ 执行循环
def run_agent(month: str, brain=None, max_rounds=14, max_seconds=240) -> dict:
    """带三个刹车的循环：轮次上限、超时、重复调用拦截。"""
    brain = brain or HeuristicBrain()
    history, seen = [], {}
    t0 = time.time()
    status = "done"

    for rnd in range(1, max_rounds + 1):
        if time.time() - t0 > max_seconds:
            status = "timeout"
            break
        act = brain.decide(month, history)
        if act is None:
            break
        key = (act.tool, json.dumps(act.args, sort_keys=True, ensure_ascii=False))
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > 1:                     # 同一个调用重复，直接停
            history.append({"round": rnd, "tool": act.tool, "args": act.args,
                            "result": {"ok": False, "error": "重复调用，已拦截"}})
            break
        res = call_tool(act.tool, act.args)
        history.append({"round": rnd, "tool": act.tool, "args": act.args, "result": res})
        _collect(brain, act, res)

    # 模型模式下一轮都没剩时，强制收尾，否则会退回到没有线索的启发式分支
    if isinstance(brain, LLMBrain) and not getattr(brain, "final_text", None):
        try:
            brain.finalize()
        except Exception as ex:
            status = f"finalize_failed: {type(ex).__name__}"

    return {"status": status, "trace": history, "rounds": len(history),
            "elapsed": round(time.time() - t0, 2),
            "ctx": getattr(brain, "ctx", {}),
            "brain": getattr(brain, "name", "?"),
            "final_text": getattr(brain, "final_text", None),
            "usage": getattr(brain, "usage", {})}


def _quota_gap(r: dict) -> bool:
    """单医生号源异常判定，两个条件同时满足才算：

    ① 绝对水平低：使用率 < 35%（正常排班区间约 30–55%）
    ② 明显低于同科室同事：最低单人 / 科室整体 < 0.75

    只用绝对阈值会漏（不同科室基础不一样）；只用相对阈值会误报（每个科室总有一个最低的）。
    """
    low = r.get("最低单人使用率%")
    if low is None or low >= 35:
        return False
    dept_avg = r.get("号源使用率%") or 0
    return dept_avg > 0 and (low / dept_avg) < 0.75


def _collect(brain, act, res):
    """把观察到的线索累积到上下文，供后续步骤分支（这就是『多步』的实质）。

    判定用「偏离该科室自身历史基线」，不用「院内科室之间横向比」：
    后者会把规模当异常，比如小科室月挂号 400 例永远比内科 1200 例「低」。
    """
    if not isinstance(brain, HeuristicBrain) or not res.get("ok"):
        return
    if act.tool != "dept_breakdown":
        return
    metric = act.args.get("metric")
    findings = brain.ctx.setdefault("findings", [])
    seen = {f["科室"] for f in findings}
    for r in res["rows"]:
        dev = r["偏离%"]
        hit = None
        if metric == "volume" and dev <= -25:
            hit = ("挂号量下滑", f"{act.args['month']} 有效挂号 {r['有效挂号']:,} 例，"
                                f"低于自身 11 个月基线 {r['基线中位数']:,.0f} 的 {abs(dev):.0f}%")
        elif metric == "cancel_rate" and dev >= 100 and r["退号率%"] >= 10:
            hit = ("退号率异常", f"{act.args['month']} 退号率 {r['退号率%']}%，"
                                f"自身基线 {r['基线中位数']}%，高出 {dev:.0f}%")
        elif metric == "quota_usage" and _quota_gap(r):
            hit = ("号源浪费", f"{act.args['month']} {r.get('异常医生','某医生')} 号源使用率仅 "
                              f"{r['最低单人使用率%']}%，科室当月整体 {r['号源使用率%']}%，"
                              f"号放出去了但几乎没人挂（疑停诊未撤号）")
        if hit and r["科室"] not in seen:
            findings.append({"科室": r["科室"], "类型": hit[0], "证据": hit[1]})
            seen.add(r["科室"])
            brain.ctx.setdefault("suspects", []).append(r["科室"])


# ============================================================ 结论生成
def conclude(result: dict, month: str) -> dict:
    """把 trace + 累积线索转成「结论 + 证据链」。

    LLM 模式下模型的最终回答优先——它比启发式更灵活；
    规则模式下用 ctx 里累积的 findings。
    """
    tr = result["trace"]
    if result.get("final_text"):
        return {"结论": [{"类型": "模型归因", "是否问题": None, "科室": None,
                          "说明": result["final_text"].strip()}], "证据链": tr}
    ctx = result.get("ctx") or {}
    summary = next((t["result"] for t in tr if t["tool"] == "monthly_summary"), {})
    trend = next((t["result"] for t in tr if t["tool"] == "trend"), {})
    cal = next((t["result"] for t in tr if t["tool"] == "calendar_context"), {})
    details = [t["result"] for t in tr
               if t["tool"] == "dept_month_detail" and t["result"].get("ok")]

    findings = [dict(f, 是否问题=True) for f in ctx.get("findings", [])]

    # 季节性：由决策阶段判定后置标记，这里只负责表述
    seasonal = ctx.get("seasonal")
    if seasonal:
        return {"结论": [{
            "类型": "季节性波动", "是否问题": False, "科室": None,
            "说明": (f"全院有效挂号较上月下降 {seasonal['drop']*100:.0f}%，"
                     f"{month} 含{seasonal['holiday']}，属正常波动；"
                     f"科室级下滑是全院趋势的一部分，不作为业务问题上报")}],
            "证据链": tr}

    # 用科室明细补充：人力变化、以及「持续 vs 新发」
    detail_by_dept = {d["科室"]: d for d in details}
    for f in findings:
        d = detail_by_dept.get(f["科室"])
        if not d:
            continue
        pre_, cur_ = d["上月指标"], d["本月指标"]
        if pre_["出诊医生数"] > cur_["出诊医生数"]:
            f["类型"] += "（医生减少）"
            f["证据"] += f"；出诊医生 {pre_['出诊医生数']} → {cur_['出诊医生数']} 人"
        elif f["类型"].startswith("挂号量") and pre_["有效挂号"]:
            # 上月也不比本月好多少（回升不足 35%），说明不是本月新发，而是持续状态
            if cur_["有效挂号"] / pre_["有效挂号"] < 1.35:
                f["类型"] = "挂号量持续偏低"
                f["证据"] += (f"；上月 {pre_['有效挂号']:,} 例，未见明显恢复，"
                              f"属持续性问题而非本月新增")

    if not findings:
        findings = [{"类型": "未发现明显异常", "是否问题": False, "科室": None,
                     "说明": "挂号量、退号率、号源使用率三个维度均在各自历史基线附近"}]
    return {"结论": findings, "证据链": tr}


if __name__ == "__main__":
    import sys
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    month = args[0] if args else "2026-07"
    use_llm = "--llm" in sys.argv

    brain = LLMBrain() if use_llm else None
    r = run_agent(month, brain)
    out = conclude(r, month)

    print(f"=== 归因：{month} · 决策来源 {r['brain']} ===")
    for t in out["证据链"]:
        print(f"  第{t['round']}步 {t['tool']}({json.dumps(t['args'], ensure_ascii=False)})")
    print("\n=== 结论 ===")
    for f in out["结论"]:
        tag = "?" if f.get("是否问题") is None else ("问题" if f["是否问题"] else "非问题")
        print(f"  [{tag}] {f['类型']}"
              f"{'·' + f['科室'] if f.get('科室') else ''}：{f.get('证据') or f.get('说明')}")
    print(f"\n步数 {r['rounds']} · 耗时 {r['elapsed']}s · 状态 {r['status']}")
    if r.get("usage") and (r["usage"].get("prompt") or 0) > 0:
        u = r["usage"]
        print(f"Token: prompt {u['prompt']:,} · completion {u['completion']:,}")
