# -*- coding: utf-8 -*-
"""
backtest.py · 证伪台（不是"证明我有 edge"的机器）
------------------------------------------------
它只回答一个问题：把同一套漏斗放到历史数据上，扣掉真实成本之后，
它的结果能不能和"买入持有 / 随机选股 / 纯动量"区分开？

数据口径与限制（必须先承认，再看结论）：
1. 样本 = 当前成交额榜前 N 只（存在当期热门偏差与幸存者偏差，结论只能用来证伪，不能用来证明）。
2. 只回放了 SCAN + VET（这两步的输入在日K里都有）。
   SOCIAL 需要"主力净流入"，历史日K里没有 → 含资金面的完整漏斗无法回放。
   也就是说：8 工位里最像"alpha"的那一步，恰好是唯一无法被证伪的一步。
3. 成本：佣金万2.5（最低5元）+ 过户费万0.1 + 卖出印花税万5 + 买入滑点 0.1%。
4. 信号用当日收盘数据生成，次日开盘买入（不偷未来函数）。
"""

import json
import random
import statistics
import sys

sys.path.insert(0, r"./")
import datafeed as df  # noqa

INIT = 100000.0
COMMISSION, MIN_COMM, STAMP, TRANSFER = 0.00025, 5.0, 0.0005, 0.00001
SLIPPAGE = 0.001
MAX_NEW, MAX_POS = 2, 5
RISK_PCT, MAX_POS_PCT = 0.01, 0.25
HOLD_DAYS = 8


def band(v, lo, hi, best_lo, best_hi):
    if v is None:
        return 0.0
    if v <= lo or v >= hi:
        return 0.0
    if best_lo <= v <= best_hi:
        return 1.0
    if v < best_lo:
        return (v - lo) / max(best_lo - lo, 1e-9)
    return (hi - v) / max(hi - best_hi, 1e-9)


def atr(bars, i, n=14):
    if i < n:
        return 0.0
    trs = []
    for k in range(i - n + 1, i + 1):
        h, l, pc = bars[k]["h"], bars[k]["l"], bars[k - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs)


def fee_buy(amount):
    return max(MIN_COMM, amount * COMMISSION) + amount * TRANSFER


def fee_sell(amount):
    return max(MIN_COMM, amount * COMMISSION) + amount * TRANSFER + amount * STAMP


# ---------------------------------------------------------------- 数据
def universe(size=60):
    rows = df.list_snapshot(sort="f6", size=size)
    out = []
    for r in rows:
        c, n = r.get("code"), r.get("name") or ""
        if not c or "ST" in n.upper() or "退" in n or c.startswith(("8", "4")):
            continue
        out.append((c, n))
    return out


def load(codes):
    series = {}
    for i, (code, name) in enumerate(codes):
        try:
            items = df.get_kline(code, "day", limit=260)["items"]
        except Exception as e:
            print("  跳过 %s：%s" % (code, e))
            continue
        if len(items) >= 80:
            series[code] = {"name": name, "bars": items}
    print("  可用样本 %d/%d" % (len(series), len(codes)))
    return series


def build(series):
    """把每只股票的日线整理成按日期对齐的字典，并算出当日特征。"""
    by_date = {}
    for code, s in series.items():
        bars = s["bars"]
        for i, b in enumerate(bars):
            if i < 60:
                continue
            v5 = [x["v"] for x in bars[max(0, i - 5):i]]
            vr = b["v"] / (sum(v5) / len(v5)) if v5 and sum(v5) else None
            hi60 = max(x["h"] for x in bars[i - 59:i + 1])
            run3 = (b["c"] / bars[i - 3]["c"] - 1) * 100 if i >= 3 else None
            a = atr(bars, i)
            by_date.setdefault(b["d"], {})[code] = {
                "close": b["c"], "open": b["o"], "high": b["h"], "low": b["l"],
                "pct": b["pct"], "turn": b["turn"], "amt": b["a"],
                "vr": vr, "pos60": (b["c"] / hi60 * 100 if hi60 else None),
                "run3": run3, "atr": a, "idx": i,
            }
    return by_date


def limit_pct(code):
    return 20.0 if code.startswith(("300", "301", "688")) else 10.0


def score_of(code, f):
    pct, turn, amt, vr = f["pct"], f["turn"], f["amt"], f["vr"]
    if pct is None or not amt:
        return None
    s_pct = band(pct, 1.0, limit_pct(code) - 0.8, 3.0, 5.5)
    s_vr = band(vr, 1.0, 12.0, 2.5, 4.5)
    s_turn = band(turn, 1.5, 30.0, 4.0, 10.0)
    s_amt = band(amt / 1e8, 1.0, 50.0, 3.0, 8.0)
    return round((0.28 * s_pct + 0.18 * s_vr + 0.16 * s_turn + 0.20 * s_amt) * 0.88, 4)


def vet_ok(code, f):
    """VET 里能用日K验证的部分：换手/波动/位置/过热（成交额与ST前置已过）。"""
    return (2 <= (f["turn"] or 0) <= 25
            and 1.5 <= (f["atr"] / f["close"] * 100 if f["close"] else 0) <= 12
            and (f["pos60"] or 0) >= 88
            and (f["run3"] if f["run3"] is not None else 100) < 18
            and (f["amt"] or 0) >= 1e8
            and (f["pct"] or 0) < limit_pct(code) - 2)


# ---------------------------------------------------------------- 引擎
def run(by_date, picker, seed=7):
    """picker(date, feats, n) -> [(code, feat)]，返回选中的标的。"""
    random.seed(seed)
    dates = sorted(by_date)
    cash, pos, eq_curve, trades = INIT, {}, [], []
    pending = []

    for di, d in enumerate(dates):
        # 1) 处理今日到期的挂单（昨日信号 → 今日开盘买）
        for code, shares in pending:
            f = by_date[d].get(code)
            if not f:
                continue
            px = f["open"] * (1 + SLIPPAGE)
            amt = px * shares
            fee = fee_buy(amt)
            if amt + fee <= cash and len(pos) < MAX_POS and shares >= 100:
                cash -= amt + fee
                pos[code] = {"shares": shares, "entry": px, "cost": amt + fee,
                             "stop": px - 1.5 * f["atr"] if f["atr"] else px * 0.92,
                             "target": px + 3.0 * f["atr"] if f["atr"] else px * 1.06,
                             "held": 0, "buy_date": d}
                trades.append({"code": code, "side": "BUY", "px": round(px, 2),
                               "shares": shares, "fee": round(fee, 2), "date": d})
        pending = []

        # 2) 持仓：止损/止盈/时间退出
        for code in list(pos):
            p, f = pos[code], by_date[d].get(code)
            if not f:
                continue
            p["held"] += 1
            exit_px, why = None, None
            if f["low"] <= p["stop"]:
                exit_px, why = min(p["stop"], f["open"]), "STOP"
            elif f["high"] >= p["target"]:
                exit_px, why = max(p["target"], f["open"]), "TARGET"
            elif p["held"] >= HOLD_DAYS:
                exit_px, why = f["close"], "TIME"
            if exit_px:
                amt = exit_px * p["shares"]
                fee = fee_sell(amt)
                cash += amt - fee
                pnl = amt - fee - p["cost"]
                trades.append({"code": code, "side": "SELL", "px": round(exit_px, 2),
                               "shares": p["shares"], "fee": round(fee, 2),
                               "pnl": round(pnl, 2), "why": why, "date": d,
                               "held": p["held"]})
                del pos[code]

        # 3) 今日收盘生成信号，明日开盘执行
        if di + 1 < len(dates) and len(pos) + len(pending) < MAX_POS:
            picks = picker(d, by_date[d], MAX_NEW)
            for code, f in picks:
                if code in pos:
                    continue
                stop = f["open"] * (1 + SLIPPAGE) - 1.5 * f["atr"] if f["atr"] else f["open"] * 0.92
                risk_ps = f["open"] * (1 + SLIPPAGE) - stop
                if risk_ps <= 0:
                    continue
                eq = cash + sum(pp["shares"] * (by_date[d].get(cc, {}).get("close") or pp["entry"])
                                for cc, pp in pos.items())
                sh = int(min(eq * RISK_PCT / risk_ps, eq * MAX_POS_PCT / f["open"],
                             cash * 0.98 / f["open"]) // 100) * 100
                if sh >= 100:
                    pending.append((code, sh))

        mv = sum(p["shares"] * (by_date[d].get(c, {}).get("close") or p["entry"])
                 for c, p in pos.items())
        eq_curve.append((d, cash + mv))

    return {"curve": eq_curve, "trades": trades}


# ---------------------------------------------------------------- 基线
def pick_strategy(d, feats, n):
    cands = [(score_of(c, f), c, f) for c, f in feats.items()]
    cands = [x for x in cands if x[0] is not None and vet_ok(x[1], x[2])]
    cands.sort(reverse=True, key=lambda x: x[0])
    return [(c, f) for _, c, f in cands[:n]]


def pick_random(d, feats, n):
    codes = list(feats)
    random.shuffle(codes)
    return [(c, feats[c]) for c in codes[:n]]


def pick_momentum(d, feats, n):
    rows = [(f["run3"] or -99, c, f) for c, f in feats.items() if f["run3"] is not None]
    rows.sort(reverse=True)
    return [(c, f) for _, c, f in rows[:n]]


def stats(res, label):
    curve = res["curve"]
    total = (curve[-1][1] / INIT - 1) * 100
    rets = [(curve[i][1] / curve[i - 1][1] - 1) for i in range(1, len(curve))]
    vol = statistics.pstdev(rets) * (252 ** 0.5) * 100 if rets else 0
    peak, mdd = 0, 0
    for _, v in curve:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    sells = [t for t in res["trades"] if t["side"] == "SELL"]
    wins = [t for t in sells if t["pnl"] > 0]
    fees = sum(t["fee"] for t in res["trades"])
    half = len(curve) // 2
    h1 = (curve[half][1] / curve[0][1] - 1) * 100 if half else 0
    h2 = (curve[-1][1] / curve[half][1] - 1) * 100 if half else 0
    return {"label": label, "total": round(total, 2), "vol": round(vol, 1),
            "mdd": round(mdd * 100, 1), "trades": len(sells),
            "win": round(len(wins) / len(sells) * 100, 1) if sells else 0,
            "avg_pnl": round(sum(t["pnl"] for t in sells) / len(sells), 1) if sells else 0,
            "fees": round(fees, 0), "days": len(curve),
            "h1": round(h1, 1), "h2": round(h2, 1)}


def buy_hold(by_date):
    dates = sorted(by_date)
    start = dates[60]
    codes = [c for c in by_date[start] if c in by_date[dates[-1]]]
    curve = []
    for d in dates[60:]:
        vals = [by_date[d][c]["close"] / by_date[start][c]["close"]
                for c in codes if c in by_date[d]]
        curve.append((d, INIT * (sum(vals) / len(vals)) if vals else INIT))
    return {"curve": curve, "trades": []}


# ---------------------------------------------------------------- 主流程
def main():
    print("[1/4] 取样本池（当前成交额榜前 60，注意这是当期热门偏差）")
    codes = universe(60)
    print("  样本池 %d 只" % len(codes))
    print("[2/4] 拉日K（260 根）")
    series = load(codes)
    print("[3/4] 对齐日期并计算特征")
    by_date = build(series)
    dates = sorted(by_date)
    print("  交易日 %d 天：%s → %s" % (len(dates), dates[0], dates[-1]))

    print("[4/4] 回放：策略 / 买入持有 / 随机 / 纯动量")
    out = [stats(run(by_date, pick_strategy), "漏斗策略(SCAN+VET)"),
           stats(buy_hold(by_date), "买入持有(等权样本池)"),
           stats(run(by_date, pick_random, seed=11), "随机选股(同规则同成本)"),
           stats(run(by_date, pick_momentum), "纯动量(20日涨幅前2)")]

    rows = ["| 对照 | 区间收益 | 前半段 | 后半段 | 年化波动 | 最大回撤 | 平仓笔数 | 胜率 | 均笔盈亏 | 成本合计 |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    for r in out:
        rows.append("| %s | %+.2f%% | %+.1f%% | %+.1f%% | %.1f%% | %.1f%% | %d | %.1f%% | %+.0f | %.0f |"
                    % (r["label"], r["total"], r["h1"], r["h2"], r["vol"], r["mdd"],
                       r["trades"], r["win"], r["avg_pnl"], r["fees"]))
    table = "\n".join(rows)

    strat, hold, rnd, mom = out
    gaps = {"随机选股": strat["total"] - rnd["total"],
            "纯动量": strat["total"] - mom["total"],
            "买入持有": strat["total"] - hold["total"]}
    beats = [k for k, v in gaps.items() if v > 3.0]
    loses = [k for k, v in gaps.items() if v < -3.0]
    gap_txt = "、".join("%s %+.1f 个点" % (k, v) for k, v in gaps.items())

    if strat["trades"] < 30:
        concl = ("平仓 %d 笔，样本不足 30 笔，任何收益差异都不能当结论——"
                 "这个样本量下，胜率 60%% 和 50%% 在统计上无法区分。" % strat["trades"])
    elif not beats:
        concl = "相对三个基线都没有 3 个点以上的优势（%s）。结论：漏斗没有可被证明的 edge。" % gap_txt
    elif loses:
        concl = ("相对 %s 领先，但被 %s 压过（%s）。说明这套漏斗里真正起作用的不是它独有的部分，"
                 "而是被比较基线也在用的同一个东西（动量/规模），换一个实现就能复现——这不叫 edge。"
                 % ("、".join(beats), "、".join(loses), gap_txt))
    else:
        concl = ("相对三个基线都领先（%s），最大回撤 %.1f%%。但这是单一样本、单一窗口的样本内结果，"
                 "前后半段收益为 %+.1f%% / %+.1f%%，只能说明"
                 "「在这个窗口里没被否掉」，不能说明「被证明」。"
                 % (gap_txt, strat["mdd"], strat["h1"], strat["h2"]))

    if strat["h2"] < mom["h2"] - 5:
        concl += (" 更值得注意的是：后半段纯动量 %+.1f%%、漏斗 %+.1f%%——"
                  "漏斗的过滤在后半段是在扣分，不是在加分。"
                  % (mom["h2"], strat["h2"]))

    report = {
        "asof": dates[-1], "start": dates[60], "days": len(dates),
        "universe": len(series), "table": table, "rows": out,
        "conclusion": concl,
        "limits": [
            "样本取自当前成交额榜，存在当期热门偏差与幸存者偏差，只能证伪不能证明",
            "SOCIAL（主力净流入）无历史数据，含资金面的完整漏斗无法回放",
            "VET 的 PE 校验在历史上不可得，已剔除该项",
            "未考虑涨跌停无法成交、停牌、盘口冲击成本",
        ],
    }
    with open("backtest_report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)

    md = ["# 证伪报告 · 漏斗策略 vs 基线", "",
          "回放区间：%s → %s（%d 个交易日，%d 只样本股）" %
          (report["start"], report["asof"], report["days"], len(series)), "",
          "成本口径：佣金万2.5（最低5元）+ 过户费万0.1 + 卖出印花税万5 + 买入滑点0.1%；"
          "信号用当日收盘生成、次日开盘成交。", "", table, "",
          "## 结论", "", concl, "", "## 这份报告自己承认的限制", ""]
    md += ["- " + x for x in report["limits"]]
    with open("backtest_report.md", "w", encoding="utf-8") as f:
        f.write("\n".join(md))

    print("\n" + table + "\n\n" + concl)
    print("\n已写出 backtest_report.md / backtest_report.json")


if __name__ == "__main__":
    main()
