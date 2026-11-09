# -*- coding: utf-8 -*-
"""
plan.py · 仓位与成本（纯函数，无副作用）

存在的理由：agent_size 会把结果写进工位、写进事件流、写进审计；
但"看一眼这只票该买多少"不该产生任何副作用。这里只算，不改账本。

口径与 desk.agent_size 完全一致：
    entry = price * 1.001（含 0.1% 滑点）
    stop  = max(entry - 1.5*ATR, entry*0.92)
    target= entry + 3.0*ATR
    shares= min(风险预算 / 每股风险, 权益*25% / entry, 现金 / (entry*1.005))
               并向下取整到 100 股

成本侧：佣金万 2.5（最低 5 元）、过户费万 0.1（买卖双向）、印花税千 0.5（仅卖出）。
"回本要涨多少"= 含双边费用的盈亏平衡价 / 入场价 - 1。
"""

import datafeed as df
import desk
import factors as facts


def _fees_buy(amount):
    comm = max(desk.MIN_COMM, amount * desk.COMMISSION)
    trans = amount * desk.TRANSFER
    return comm + trans


def _fees_sell(amount):
    comm = max(desk.MIN_COMM, amount * desk.COMMISSION)
    trans = amount * desk.TRANSFER
    stamp = amount * desk.STAMP_TAX
    return comm + trans + stamp


def breakeven_price(shares, cost_total):
    """卖出所得（扣费后）刚好收回成本的价格。二分求解，不含滑点。"""
    lo, hi = 0.0, cost_total / shares * 2 + 1
    for _ in range(60):
        mid = (lo + hi) / 2
        net = mid * shares - _fees_sell(mid * shares)
        if net < cost_total:
            lo = mid
        else:
            hi = mid
    return hi


def compute(code, equity, cash):
    q = df.get_quote(code)
    if not q or not q.get("price"):
        return {"code": code, "ok": False, "reason": "取不到报价"}
    bars = df.get_kline(code, "day", limit=90)["items"]
    # 开盘前（09:15 之前）每股报价与成交额都是 0：直接算会把整单判成"不可成交"。
    # 这里把"当前交易日"换成最后一根已收完的日线，换手标不可判（不猜）。
    pre_open = bool(q.get("preOpen")) or not q.get("amount")
    if pre_open and len(bars) >= 2:
        q = dict(q, price=bars[-1]["c"], amount=bars[-1].get("a"), turnover=None)
    atr_v = desk.atr(bars, 14) or (q["price"] * 0.03)

    price = q["price"]
    entry = round(price * 1.001, 2)
    stop = max(round(entry - 1.5 * atr_v, 2), round(entry * 0.92, 2))
    target = round(entry + 3.0 * atr_v, 2)
    rps = entry - stop                  # 每股风险

    risk_amt = equity * desk.RISK_PER_TRADE
    s_risk = int(risk_amt / rps // 100) * 100 if rps > 0 else 0
    s_cap = int(equity * desk.MAX_POS_PCT / entry // 100) * 100
    s_cash = int(cash / (entry * 1.005) // 100) * 100
    shares = max(0, min(s_risk, s_cap, s_cash))

    binding = "风险预算" if shares == s_risk else ("单票上限" if shares == s_cap else "现金")

    amount = shares * entry
    cost_total = amount + _fees_buy(amount)
    be = breakeven_price(shares, cost_total) if shares >= 100 else None

    # 可交易性不过，计划就只是"如果买得进"的算术，不叫可执行
    blockers, unknowns = [], []
    sm = df.session_minutes()
    early = (sm is not None and 0 <= sm < 60)      # 开盘头一小时：量能与换手还是累计值，判不了
    tv = q.get("turnover")
    if tv is None:
        unknowns.append("换手（开盘前取不到）")
    elif early:
        unknowns.append("换手（开盘 %d 分钟，累计 %.2f%%，按收盘口径不可判）" % (sm, tv))
    elif not (facts.TH["turnover"][0] <= tv <= facts.TH["turnover"][1]):
        blockers.append("换手 %.2f%% 不在 %s~%s" % (tv, *facts.TH["turnover"]))
    amt = q.get("amount")
    if amt is None:
        unknowns.append("成交额（取不到）")
    elif early and amt < facts.TH["liquidity_amt"]:
        unknowns.append("成交额（开盘 %d 分钟，累计 %.2f 亿，按收盘口径不可判）" % (sm, amt / 1e8))
    elif amt < facts.TH["liquidity_amt"]:
        blockers.append("成交额 %.2f 亿 低于 %.1f 亿"
                        % (amt / 1e8, facts.TH["liquidity_amt"] / 1e8))

    return {
        "code": q.get("code", code), "name": q.get("name"),
        "ok": shares >= 100 and not blockers, "blockers": blockers, "unknowns": unknowns,
        "preOpen": pre_open,
        "price": price, "entry": entry, "stop": stop, "target": target,
        "atr": round(atr_v, 3), "risk_per_share": round(rps, 3),
        "rr": round((target - entry) / rps, 2) if rps > 0 else None,
        "shares": shares, "binding": binding,
        "c1_risk": s_risk, "c2_cap": s_cap, "c3_cash": s_cash,
        "risk_amt": round(risk_amt, 2), "risk_pct": round(desk.RISK_PER_TRADE * 100, 2),
        "amount": round(amount, 2), "cost_total": round(cost_total, 2),
        "fee_buy": round(_fees_buy(amount), 2),
        "breakeven": round(be, 2) if be else None,
        "breakeven_pct": round((be / entry - 1) * 100, 2) if be else None,
        "daily_loss_budget": round(equity * desk.DAILY_LOSS_HALT, 2),
        "stop_loss_amt": round(shares * rps, 2),
    }


if __name__ == "__main__":
    import json
    st = desk.load_state()
    for c in ("600744", "603212", "600000"):
        print(json.dumps(compute(c, st["equity"], st["cash"]), ensure_ascii=False))
