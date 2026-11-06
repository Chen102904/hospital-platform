# -*- coding: utf-8 -*-
"""把三个注入异常画成图，用于报告和 GitHub README。"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
import os
import pymysql

from db_conf import DB

for p in [r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf"]:
    if os.path.exists(p):
        font_manager.fontManager.addfont(p)
        plt.rcParams["font.sans-serif"] = [font_manager.FontProperties(fname=p).get_name()]
        break
plt.rcParams["axes.unicode_minus"] = False

C = {"主": "#0F6E56", "次": "#9FD8C0", "警": "#C0392B", "灰": "#8a8a8a"}


def q(sql, args=None):
    c = pymysql.connect(**DB)
    try:
        cur = c.cursor()
        cur.execute(sql, args)
        return cur.fetchall()
    finally:
        c.close()


def main():
    # 1) 内科 vs 全院
    all_m = dict(q("""SELECT LEFT(reg_date,7) m, SUM(reg_status NOT IN ('已取消','已退费')) n
                      FROM registration GROUP BY m ORDER BY m"""))
    int_m = dict(q("""SELECT LEFT(r.reg_date,7) m, SUM(r.reg_status NOT IN ('已取消','已退费')) n
                      FROM registration r JOIN department d USING(dept_id)
                      WHERE d.dept_name='内科' GROUP BY m ORDER BY m"""))
    months = sorted(all_m)

    fig, axes = plt.subplots(1, 3, figsize=(16, 4.6))

    ax = axes[0]
    ax.plot(months, [all_m[m] / 1000 for m in months], marker="o", ms=5,
            color=C["灰"], lw=2, label="全院（千例）")
    ax2 = ax.twinx()
    ax2.plot(months, [int_m[m] for m in months], marker="s", ms=5,
             color=C["主"], lw=2, label="内科（例）")
    ax2.axvline(8.5, color=C["警"], ls="--", lw=1.2, alpha=0.8)
    ax2.annotate("7 月起 2 名医生离职\n内科 1211 → 669 例（-45%）",
                 xy=(9, 669), xytext=(1.2, 960),
                 arrowprops=dict(arrowstyle="->", color=C["警"], lw=1.2,
                                 connectionstyle="arc3,rad=-0.2"),
                 fontsize=8.5, color=C["警"],
                 bbox=dict(boxstyle="round,pad=0.35", fc="white", ec=C["警"], lw=0.8, alpha=0.95))
    ax.set_title("异常 A：内科医生离职导致量下滑", fontsize=12)
    ax.set_xticks(range(len(months)))
    ax.set_xticklabels([m[2:] for m in months], rotation=45, fontsize=8)
    ax.set_ylabel("全院有效挂号（千例）", fontsize=9, color=C["灰"])
    ax2.set_ylabel("内科有效挂号（例）", fontsize=9, color=C["主"])
    ax.grid(alpha=0.2)
    ax.set_axisbelow(True)

    # 2) 儿科退号率
    ax = axes[1]
    cr = dict(q("""SELECT LEFT(r.reg_date,7) m,
                          ROUND(SUM(r.reg_status IN ('已取消','已退费'))/COUNT(*)*100,1) v
                   FROM registration r JOIN department d USING(dept_id)
                   WHERE d.dept_name='儿科' GROUP BY m ORDER BY m"""))
    vals = [float(cr[m]) for m in months]
    ax.plot(months, vals, marker="o", ms=6, color=C["主"], lw=2)
    ax.axhline(3, color=C["灰"], ls=":", lw=1)
    ax.text(len(months) - 0.5, 4.2, "常态约 3%", ha="right", fontsize=8, color=C["灰"])
    idx = months.index("2026-08")
    ax.scatter([months[idx]], [vals[idx]], s=110, color=C["警"], zorder=5)
    ax.annotate(f"8 月 {vals[idx]:.1f}%\n（量正常，只是点了又退）",
                xy=(idx, vals[idx]), xytext=(idx - 6.6, 24),
                arrowprops=dict(arrowstyle="->", color=C["警"], lw=1.2,
                                connectionstyle="arc3,rad=0.18"),
                fontsize=8.5, color=C["警"],
                bbox=dict(boxstyle="round,pad=0.35", fc="white", ec=C["警"], lw=0.8, alpha=0.95))
    ax.set_title("异常 B：儿科退号率暴涨", fontsize=12)
    ax.set_xticks(range(len(months)))
    ax.set_xticklabels([m[2:] for m in months], rotation=45, fontsize=8)
    ax.set_ylabel("退号率 %", fontsize=9)
    ax.grid(alpha=0.2)
    ax.set_axisbelow(True)

    # 3) 骨科 9 月各医生号源使用率
    ax = axes[2]
    rows = q("""SELECT doc.doctor_name, doc.doctor_id, SUM(s.total_quota) quota,
                       (SELECT COUNT(*) FROM registration r WHERE r.doctor_id=doc.doctor_id
                        AND LEFT(r.reg_date,7)='2026-09'
                        AND r.reg_status NOT IN ('已取消','已退费')) actual
                FROM doctor_schedule s JOIN doctor doc ON doc.doctor_id=s.doctor_id
                WHERE LEFT(s.work_date,7)='2026-09'
                  AND doc.dept_id=(SELECT dept_id FROM department WHERE dept_name='骨科')
                GROUP BY doc.doctor_id""")
    names = [r[0] for r in rows]
    usage = [r[3] / r[2] * 100 for r in rows]
    colors = [C["警"] if u == min(usage) else C["次"] for u in usage]
    bars = ax.barh(names, usage, color=colors, edgecolor="white")
    for b, u in zip(bars, usage):
        ax.text(u + 1, b.get_y() + b.get_height() / 2, f"{u:.1f}%",
                va="center", fontsize=10,
                color=C["警"] if u == min(usage) else "#2C2C2A")
    ax.set_xlim(0, 62)
    ax.set_title("异常 D：骨科某医生停诊未撤号", fontsize=12)
    ax.set_xlabel("号源使用率 %（当月放号中被实际挂走的比例）", fontsize=9)
    ax.grid(axis="x", alpha=0.2)
    ax.set_axisbelow(True)
    for s in ["top", "right"]:
        ax.spines[s].set_visible(False)

    fig.suptitle("三个注入异常的形态：一个掉量、一个掉「质」、一个掉在看不见的地方",
                 fontsize=13, y=1.02)
    fig.tight_layout()
    out = "ops_fig_异常形态.png"
    fig.savefig(out, dpi=200, bbox_inches="tight")
    print("已生成", out)


if __name__ == "__main__":
    main()
