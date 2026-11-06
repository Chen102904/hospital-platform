# -*- coding: utf-8 -*-
"""
对归因 Agent 做评测：跑完 12 个月，拿「注入的已知异常」当答案对账。

为什么要这样测：
  只看单个月「答得像不像」没有意义——模型可以编一个听起来合理的原因。
  只有事先埋好已知答案（哪个科室、哪个月、什么原因），才能算出
  「真异常有没有找到」「没问题的月份有没有瞎报」。

评分口径（写死，避免事后调）：
  - 命中：该月结论里出现「注入的科室」且类型关键词对上
  - 误报：非注入月份里报出的「是否问题=True」条目（内科离职后的持续偏低不计，见下）
  - 假异常 C（春节）：期望判为「非问题」，报成问题算误报
"""
import json
import time

import ops_agent as A
from gen_ops_data import INJECTED

MONTHS = ["2025-11", "2025-12", "2026-01", "2026-02", "2026-03", "2026-04",
          "2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10"]

# 注入异常 → 期望命中的关键词
EXPECT = {
    "A": {"month": "2026-07", "dept": "内科", "kw": ["挂号量", "人力", "医生"], "is_problem": True},
    "B": {"month": "2026-08", "dept": "儿科", "kw": ["退号"], "is_problem": True},
    "C": {"month": "2026-02", "dept": None, "kw": ["季节性", "波动"], "is_problem": False},
    "D": {"month": "2026-09", "dept": "骨科", "kw": ["号源"], "is_problem": True},
}

# 内科 7 月医生离职后，8/9/10 月会持续偏低。这是真实情况，不算误报，
# 但要单独统计——它检验 Agent 会不会把「持续性老问题」当成「本月新问题」。
PERSIST = {"2026-08", "2026-09", "2026-10"}


def single_turn(month: str):
    """对照组：模拟「智能问数」——只查一次挂号量，然后直接下结论。

    用来回答面试必问的那句「这跟写个脚本有什么区别」：
    单轮只能看到「谁的量掉了」，看不到「量没掉但退号率暴涨」和「号放了没人挂」。
    """
    res = A.t_dept_breakdown(month, "volume")
    return [(r["科室"], "挂号量下滑") for r in res["rows"] if r["偏离%"] <= -25]


def main():
    rows, t0 = [], time.time()
    for m in MONTHS:
        r = A.run_agent(m)
        out = A.conclude(r, m)
        rows.append({"month": m, "步数": r["rounds"], "耗时": r["elapsed"],
                     "结论": out["结论"]})

    # ---- 打分
    hit, miss, false_pos, persist = [], [], [], []
    by_month = {x["month"]: x["结论"] for x in rows}

    for key, exp in EXPECT.items():
        res = by_month.get(exp["month"], [])
        ok = False
        for f in res:
            if exp["is_problem"]:
                if f.get("是否问题") and (exp["dept"] in (f.get("科室") or "")) and \
                        any(k in f["类型"] + f.get("证据", "") for k in exp["kw"]):
                    ok = True
            else:
                if (not f.get("是否问题")) and any(k in f["类型"] + f.get("说明", "") for k in exp["kw"]):
                    ok = True
        (hit if ok else miss).append(key)

    for x in rows:
        m = x["month"]
        for f in x["结论"]:
            if not f.get("是否问题"):
                continue
            if m in PERSIST and f.get("科室") == "内科" and "持续" in f["类型"]:
                persist.append((m, f["类型"]))
                continue
            if any(EXPECT[k]["month"] == m and EXPECT[k]["dept"] == f.get("科室") for k in EXPECT):
                continue
            false_pos.append((m, f.get("科室"), f["类型"]))

    # ---- 输出
    print(f"{'月份':<9}{'步数':>4}{'耗时':>7}  结论")
    print("-" * 96)
    for x in rows:
        brief = "；".join(
            f"{'!' if f.get('是否问题') else '·'}{f.get('科室') or '全院'}:{f['类型']}"
            for f in x["结论"]) or "—"
        print(f"{x['month']:<9}{x['步数']:>4}{x['耗时']:>6.1f}s  {brief}")

    real_cnt = sum(1 for k in EXPECT if EXPECT[k]["is_problem"])
    real_hit = [k for k in hit if EXPECT[k]["is_problem"]]
    print("\n" + "=" * 60)
    print(f"真异常检出   {len(real_hit)}/{real_cnt}   {sorted(real_hit)}")
    if miss:
        print(f"漏报         {sorted(miss)}")
    print(f"假异常处理   {'正确判为非问题（未误报）' if 'C' in hit else '误判为业务问题'}")
    print(f"误报          {len(false_pos)} 条" + (f"  {false_pos}" if false_pos else ""))
    print(f"持续性问题    {len(persist)} 条被正确标为「持续」而非新问题")
    print(f"平均步数      {sum(x['步数'] for x in rows)/len(rows):.1f}")
    print(f"总耗时        {time.time()-t0:.1f}s（12 个月）")
    print("=" * 60)

    # ---- 对照组：单轮查询能检出几个
    print("\n对照：单轮「智能问数」vs 多步归因（同样 4 个注入异常）")
    print(f"{'异常':<4}{'月份':<10}{'单轮':<32}{'多步'}")
    print("-" * 72)
    single_hit = 0
    for key, exp in EXPECT.items():
        if not exp["is_problem"]:
            continue
        got = single_turn(exp["month"])
        ok = any(d == exp["dept"] for d, _t in got)
        single_hit += 1 if ok else 0
        multi_ok = key in real_hit
        print(f"{key:<4}{exp['month']:<10}"
              f"{('检出 ' + exp['dept']) if ok else '漏（只看到量，看不到原因）':<32}"
              f"{'检出' if multi_ok else '漏'}")
    print("-" * 72)
    print(f"单轮检出 {single_hit}/3 · 多步检出 {len(real_hit)}/3")

    json.dump(rows, open("eval_result.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1, default=str)


if __name__ == "__main__":
    main()
