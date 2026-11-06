#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
跨轮次对比 —— 同一批题跑了好几次，看哪些是稳定失败、哪些只是抖。

为什么需要它：
    单跑一轮，你只知道「这次 86%」。看不出某个失败是「系统真有问题」
    还是「模型今天心情不好」。跑两三轮再对比，稳定失败的才是真缺陷，
    时好时坏的是稳定性问题，两者修法完全不同。

用法：
    python compare.py                          # 自动找 reports/ 下所有 .json
    python compare.py reports/a.json reports/b.json
"""

import glob
import json
import sys
import unicodedata
from collections import OrderedDict
from pathlib import Path

HERE = Path(__file__).resolve().parent


def norm(s):
    s = unicodedata.normalize("NFKC", str(s))
    return s.strip()


def load(paths):
    runs = []
    for p in paths:
        d = json.loads(Path(p).read_text(encoding="utf-8"))
        runs.append({
            "file": Path(p).name,
            "tag": d.get("meta", {}).get("tag", "") or Path(p).stem,
            "when": d.get("meta", {}).get("when", ""),
            "acc": d.get("summary", {}).get("acc"),
            "passed": d.get("summary", {}).get("passed"),
            "n": d.get("summary", {}).get("n"),
            "by_id": {r["id"]: r for r in d["results"]},
        })
    return sorted(runs, key=lambda r: r["when"])


def main():
    args = sys.argv[1:]
    if args:
        paths = args
    else:
        paths = sorted(glob.glob(str(HERE / "reports" / "*.json")))
    if not paths:
        print("× reports/ 下没有 .json，先跑一轮 run_eval.py")
        return

    runs = load(paths)
    all_ids = list(OrderedDict.fromkeys(i for r in runs for i in r["by_id"]))

    print()
    print("═" * 78)
    print("  跨轮次对比")
    print("═" * 78)
    print()
    print(f"  {'轮次':<22}{'题数':>6}{'通过':>6}{'准确率':>9}   跑的时间")
    print("  " + "─" * 74)
    for r in runs:
        print(f"  {r['tag'][:20]:<22}{r['n']:>6}{r['passed']:>6}{r['acc']:>8}%   {r['when']}")

    accs = [r["acc"] for r in runs if r["acc"] is not None]
    if len(accs) > 1:
        print("  " + "─" * 74)
        print(f"  {'极差':<22}{'':>6}{'':>6}{round(max(accs) - min(accs), 1):>8}%   "
              f"差多少个百分点")

    # 按通过率分三档
    rows = []
    for i in all_ids:
        hits = [1 if r["by_id"].get(i, {}).get("pass") else 0
                for r in runs if i in r["by_id"]]
        if not hits:
            continue
        rows.append((i, sum(hits), len(hits), hits))

    total = len(runs)
    stable_fail = [x for x in rows if x[1] == 0]
    flaky = [x for x in rows if 0 < x[1] < x[2]]
    stable_ok = [x for x in rows if x[1] == x[2]]

    def line(x):
        i, ok, n, hits = x
        bar = "".join("✓" if h else "✗" for h in hits)
        return f"  {i:<5} {bar:<{max(total, 3)}}  {ok}/{n}"

    print()
    print(f"  稳定失败（每次都挂，{len(stable_fail)} 题）—— 这才是真该修的")
    print("  " + "─" * 74)
    for x in stable_fail:
        why = next((r["by_id"][x[0]]["why"] for r in runs if x[0] in r["by_id"]), "")
        print(line(x) + "  " + norm(why)[:40])
    if not stable_fail:
        print("  （没有）")

    print()
    print(f"  时好时坏（{len(flaky)} 题）—— 稳定性问题，不是能力问题")
    print("  " + "─" * 74)
    for x in flaky:
        print(line(x))
    if not flaky:
        print("  （没有。如果每轮都没抖动，反而要怀疑是不是缓存住了）")

    print()
    print(f"  稳定通过：{len(stable_ok)}/{len(rows)} 题"
          f"（{round(len(stable_ok) / len(rows) * 100, 1) if rows else 0}%）")
    print()
    print("  怎么用这份表：")
    print("    · 稳定失败 → 改代码/改工具/改 prompt，改完再跑，看它有没有从这一档里消失")
    print("    · 时好时坏 → 别急着说是模型不行，先看是不是某题特别依赖工具参数推理")
    print("    · 稳定通过 → 定期抽查，确认判分没有松到「什么都能过」")
    print()


if __name__ == "__main__":
    main()
