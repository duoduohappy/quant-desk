# -*- coding: utf-8 -*-
"""
drill_stage.py · 审批闸门演练
--------------------------------
今天（周日，数据为 11-06 收盘）资金面全线为负，没有任何线索能自然走到 STAGED，
所以人工审批闸门不会被触发。这个脚本把两条**真实候选**（真实价格/真实 K 线/真实仓位算法）
手工置为 STAGED，并在 note 与审计里都标 `DRILL`，用来演示人机边界。

它不伪造行情，只跳过 SOCIAL 这一关；生产路径上的线索不会被打上 DRILL。
"""

import datafeed as df
import desk

st = desk.load_state()
seen, picked = set(), []
for l in st["leads"]:
    if l["state"] == "PASSED" and l["code"] not in seen and l.get("cand"):
        seen.add(l["code"])
        picked.append(l)
    if len(picked) >= 2:
        break

for l in picked:
    bars = df.get_kline(l["code"], "day", limit=90)["items"]
    a = desk.atr(bars, 14)
    plan = desk.agent_size(st, l["cand"], {"bars": bars, "atr": a})
    if not plan:
        print("%s 仓位计算为 0，跳过" % l["code"])
        continue
    l["state"] = "STAGED"
    l["plan"] = plan
    l["drill"] = True
    l["note"] = "演练单（DRILL）：资金面未确认，仅用于验证人工审批闸门"
    desk.audit({"agent": "CHIEF", "action": "DRILL_STAGED",
                "lead": l["id"], "code": l["code"], "shares": plan["shares"]})
    print("已置为 STAGED：%s %s %d 股 @ %.2f" %
          (l["id"], l["code"], plan["shares"], plan["entry"]))

desk.save_state(st)
print("完成。到 /desk 上点「批准」或「否决」。")
