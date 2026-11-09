# -*- coding: utf-8 -*-
"""factors.py · 因子计算（纯函数）

设计原则：
1) 只做"能从真实数据算出来"的因子，算不出来一律 None，并在 unknown 里列出；
2) 不做任何决策，不做任何副作用（不写 state、不写审计、不调 station）；
3) 决策仍然只留给 desk.py 的漏斗 + 人工闸门 —— 这里是"证据"，不是"结论"。

对外：compute(code) -> dict    /    facts(code) -> 单行摘要（给日志和气泡用）
"""

import datafeed as df

# 因子阈值集中在这里，改口径只改一处
TH = {
    "liquidity_amt": 1.5e8,      # 成交额 >= 1.5 亿才谈得上能买
    "turnover": (2.0, 25.0),     # 换手 2%~25%
    "atr_pct": (1.5, 12.0),      # ATR 1.5%~12%
    "pos_hi": 88.0,              # 距 60 日高点 >= 88%
    "run3": 18.0,                # 近 3 日涨幅 < 18%
    "vol_ratio_hi": 1.5,         # 成交额 / 5 日均额 > 1.5 倍 = 放量
    "vol_ratio_lo": 0.7,         # < 0.7 倍 = 缩量
    "flow_pct": 2.0,             # 主力净流入占成交额 ±2% 才算有方向
    "flow_conflict": 5.0,        # 价涨却净流出 / 价跌却净流入 的判定门槛（%）
    "rs_band5": 3.0,             # 近 5 日超越大盘 ±3 个百分点 = 强/弱
    "rs_band20": 5.0,            # 近 20 日 ±5 个百分点 = 强/弱
}


# ---------------------------------------------------------------- 小工具
def _atr(bars, n=14):
    if len(bars) < n + 1:
        return 0.0
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-n:]) / n


def _ma(vals, n):
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def _slope(vals, n, lookback=10):
    """均线斜率，按 %/10日 表示（用复权收盘价，避免分红造成假突破）"""
    if len(vals) < n + lookback:
        return None
    a = sum(vals[-(n + lookback):-lookback]) / n
    b = sum(vals[-n:]) / n
    if not a:
        return None
    return round((b / a - 1) * 100, 2)


def _limit_pct(code):
    """涨跌停幅度：创业板/科创板 20%，其余 10%（与 desk.py 同口径）"""
    return 20.0 if code.startswith("30") or code.startswith("688") else 10.0


def _f(v, nd=2):
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 主入口
def compute(code):
    """算一只票的因子快照。所有取不到的项 -> None + unknown 名单。"""
    code = str(code or "").strip()
    q = df.get_quote(code)
    try:
        bars = df.get_kline(code, "day", limit=120)["items"]
    except Exception:
        bars = []

    asof = bars[-1]["d"] if bars else None
    price = q.get("price")
    pct = q.get("pct")
    name = q.get("name") or ""
    unknown = []

    # 开盘前（09:15 之前）get_quote 的 price / amount / turnover 全是 0：
    # 直接拿它算，会把量能算成"0 成交额"、把可交易性判成"成交额不够"，全是假结论。
    # 这里改用最后一根已收完的日线当"当前交易日"，并把换手/量比标成不可判（不猜）。
    pre_open = bool(q.get("preOpen")) or not q.get("amount")
    if pre_open and len(bars) >= 2:
        last, prev = bars[-1], bars[-2]
        price = last["c"]
        if prev["c"]:
            pct = round((last["c"] / prev["c"] - 1) * 100, 4)
        q = dict(q, price=price, pct=pct, amount=last.get("a"),
                 turnover=None, volumeRatio=None, preOpen=True)

    # 开盘头一小时：成交额/换手是当日累计值，用收盘口径去判等于把全市场判成"量不够"。
    # 这段时间它们进 unknown（不可判），别写成硬否决。
    _sm = df.session_minutes()
    early = (_sm is not None and 0 <= _sm < 60)

    # ---------------- B 价格结构 ----------------
    closes = [b["c"] for b in bars]
    ma20 = _ma(closes, 20)
    ma60 = _ma(closes, 60)
    s20 = _slope(closes, 20, 10)
    s60 = _slope(closes, 60, 10)
    hi60 = max((b["h"] for b in bars[-60:]), default=None)
    lo60 = min((b["l"] for b in bars[-60:]), default=None)
    atr_v = _atr(bars, 14)
    atr_pct = (atr_v / price * 100) if (price and atr_v) else None

    pos_hi = (price / hi60 * 100) if (price and hi60) else None
    pos_lo = (price / lo60 * 100) if (price and lo60) else None
    above_ma20 = (price > ma20) if (price and ma20) else None

    # 跳空：最近 10 根里开盘价相对前收 > 2% 的次数
    gaps = 0
    for i in range(max(1, len(bars) - 10), len(bars)):
        pc = bars[i - 1]["c"]
        if pc and abs(bars[i]["o"] / pc - 1) * 100 >= 2.0:
            gaps += 1
    # 连板（粗）：最近 3 根收盘涨幅都 >= 9.9%
    boards = 0
    for i in range(max(1, len(bars) - 3), len(bars)):
        pc = bars[i - 1]["c"]
        if pc and (bars[i]["c"] / pc - 1) * 100 >= 9.9:
            boards += 1

    if not bars:
        unknown += ["ma20", "ma60", "pos60", "atr", "gaps"]
    if ma60 is None:
        unknown.append("ma60_slope")

    structure = {
        "ma20": _f(ma20), "ma60": _f(ma60),
        "slope20": s20, "slope60": s60,
        "above_ma20": above_ma20,
        "pos_hi": _f(pos_hi, 1), "pos_lo": _f(pos_lo, 1),
        "hi60": _f(hi60), "lo60": _f(lo60),
        "atr": _f(atr_v, 3), "atr_pct": _f(atr_pct),
        "gaps": gaps, "boards": boards,
        "run3": _f((price / bars[-3]["c"] - 1) * 100, 2) if (price and len(bars) >= 3) else None,
    }
    # 结构结论（描述性，不是决策）
    flags = []
    if s20 is not None and s20 > 0 and above_ma20:
        flags.append("顺势")
    if s20 is not None and s20 < 0 and not above_ma20:
        flags.append("逆势")
    if pos_hi is not None and pos_hi >= 97:
        flags.append("贴顶")
    if pos_lo is not None and pos_lo <= 5:
        flags.append("贴底")
    if atr_pct is not None and atr_pct > 5:
        flags.append("高波动")
    if gaps >= 2:
        flags.append("跳空频繁")
    if boards >= 2:
        flags.append("连板")
    structure["flags"] = flags

    # ---------------- C 量能 ----------------
    amt = q.get("amount")
    avg5 = _ma([b["a"] for b in bars], 5) if bars else None
    avg20 = _ma([b["a"] for b in bars], 20) if bars else None
    vr5 = (amt / avg5) if (amt and avg5) else None
    vr20 = (amt / avg20) if (amt and avg20) else None

    vol_state = None
    if vr5 is not None:
        if vr5 >= TH["vol_ratio_hi"]:
            vol_state = "放量"
        elif vr5 <= TH["vol_ratio_lo"]:
            vol_state = "缩量"
        else:
            vol_state = "平量"

    # 量价背离：价创 20 日新高，但成交额低于 20 日均额的 0.8 倍
    diverge = False
    if price and bars and amt and avg20:
        hi20 = max(b["h"] for b in bars[-20:])
        if price >= hi20 and amt < avg20 * 0.8:
            diverge = True

    if amt is None:
        unknown.append("amount")
    if avg5 is None:
        unknown.append("amt_avg5")

    volume = {
        "volumeRatio": _f(q.get("volumeRatio")), "turnover": _f(q.get("turnover")),
        "amount": amt, "amt_avg5": _f(avg5, 0), "amt_avg20": _f(avg20, 0),
        "vr5": _f(vr5), "vr20": _f(vr20),
        "state": vol_state, "diverge": diverge,
    }
    if diverge:
        volume["warn"] = "价新高但量缩（背离）"

    # ---------------- D 资金面（数据源：东财 fflow/daykline，元） ----------------
    flow = {"main": None, "main_pct": None, "cum3": None, "cum5": None,
            "cum10": None, "state": None, "warn": None, "asof": None}
    try:
        ff = df.get_fundflow(code, 30)["items"]
        if ff:
            last = ff[-1]
            main = last["main"]
            flow["asof"] = last["d"]
            flow["main"] = _f(main, 0)
            flow["main_pct"] = _f(last["main_pct"])
            flow["cum3"] = _f(sum(x["main"] for x in ff[-3:]), 0)
            flow["cum5"] = _f(sum(x["main"] for x in ff[-5:]), 0)
            flow["cum10"] = _f(sum(x["main"] for x in ff[-10:]), 0)
            mp = last["main_pct"]
            if mp >= TH["flow_pct"]:
                flow["state"] = "主力净流入"
            elif mp <= -TH["flow_pct"]:
                flow["state"] = "主力净流出"
            else:
                flow["state"] = "基本持平"
            # 连续性：3 日累计与当日同向才算"持续"
            if flow["cum3"] is not None:
                if main > 0 and flow["cum3"] <= 0:
                    flow["warn"] = "今天进、3 日累计还是净流出（不代表持续进场）"
                elif main < 0 and flow["cum3"] >= 0:
                    flow["warn"] = "今天流出、3 日累计仍净流入（可能是获利回吐）"
    except Exception:
        unknown.append("fundflow")
        flow["state"] = "不可判"

    # ---------------- E 相对强弱（对照上证指数，复权口径一致） ----------------
    rs = {"index_name": "上证指数", "idx_pct": None, "excess": None,
          "r5": None, "idx_r5": None, "excess5": None,
          "r20": None, "idx_r20": None, "excess20": None,
          "state": None, "asof": None}
    try:
        idx = df.get_kline("000001", "day", limit=120, secid="1.000001")["items"]
        flow_asof = rs["asof"] = idx[-1]["d"]
        ic = [b["c"] for b in idx]
        ipct = idx[-1].get("pct")
        if ipct is None and len(ic) >= 2:
            ipct = (ic[-1] / ic[-2] - 1) * 100
        rs["idx_pct"] = _f(ipct)
        if pct is not None and ipct is not None:
            rs["excess"] = _f(pct - ipct)
        if price and len(closes) >= 6 and len(ic) >= 6:
            rs["r5"] = _f((price / closes[-6] - 1) * 100)
            rs["idx_r5"] = _f((ic[-1] / ic[-6] - 1) * 100)
            rs["excess5"] = _f(rs["r5"] - rs["idx_r5"])
        if price and len(closes) >= 21 and len(ic) >= 21:
            rs["r20"] = _f((price / closes[-21] - 1) * 100)
            rs["idx_r20"] = _f((ic[-1] / ic[-21] - 1) * 100)
            rs["excess20"] = _f(rs["r20"] - rs["idx_r20"])
        e5, e20 = rs["excess5"], rs["excess20"]
        if e5 is not None and e20 is not None:
            if e5 >= TH["rs_band5"] and e20 >= TH["rs_band20"]:
                rs["state"] = "强于大盘（5/20 日双强）"
            elif e5 <= -TH["rs_band5"] and e20 <= -TH["rs_band20"]:
                rs["state"] = "弱于大盘（5/20 日双弱）"
            elif e5 >= TH["rs_band5"]:
                rs["state"] = "短期强、中期未确认"
            elif e5 <= -TH["rs_band5"]:
                rs["state"] = "短期弱、中期未确认"
            else:
                rs["state"] = "与大盘同步"
    except Exception:
        unknown.append("index")
        rs["state"] = "不可判"

    # ---------------- 交叉校验：因子之间打架（比缺失更危险） ----------------
    cross = []
    if diverge:
        cross.append("价创新高但成交额缩（量价背离）")
    if vr5 is not None and pct is not None and vr5 >= TH["vol_ratio_hi"] and pct < 0:
        cross.append("放量下跌（出货嫌疑）")
    if (pct is not None and flow["main_pct"] is not None):
        if pct > 0 and flow["main_pct"] <= -TH["flow_conflict"]:
            cross.append("价涨但主力净流出 %.1f%%（拉高出货迹象）" % flow["main_pct"])
        if pct < 0 and flow["main_pct"] >= TH["flow_conflict"]:
            cross.append("价跌但主力净流入 %.1f%%（有人在接）" % flow["main_pct"])

    # ---------------- F 可交易性（不可成交 ≠ 分数低） ----------------
    reasons = []
    pct = q.get("pct")
    lim = _limit_pct(code)
    if "ST" in name.upper() or "退" in name:
        reasons.append("ST/退市风险")
    if not price:
        reasons.append("无现价（停牌？）")
    if early and (amt or 0) < TH["liquidity_amt"]:
        unknown.append("amount(开盘 %d 分钟，累计值)" % _sm)
    elif (amt or 0) < TH["liquidity_amt"]:
        reasons.append("成交额 < 1.5 亿（买不进量）")
    if len(bars) < 60:
        reasons.append("上市不足 60 根日K")
    tv = q.get("turnover")
    if tv is None:
        unknown.append("turnover")          # 开盘前换手取不到 → 不可判，不当成 0% 去否决
    elif early:
        unknown.append("turnover(开盘 %d 分钟，累计值)" % _sm)
    elif not (TH["turnover"][0] <= tv <= TH["turnover"][1]):
        reasons.append("换手 %.2f%% 不在 2%%~25%%" % tv)
    if pct is not None and pct >= lim - 2:
        reasons.append("涨幅 %.1f%% 接近涨停（买不进）" % pct)
    # 一字板：今日最高=最低=现价
    if bars and bars[-1]["d"] == asof:
        last = bars[-1]
        if last["h"] == last["l"]:
            reasons.append("一字板（无法成交）")

    tradable = {"ok": not reasons, "reasons": reasons, "early": early,
                "session_minutes": _sm, "limit_pct": lim, "bars": len(bars),
                "liquidity_ok": (amt or 0) >= TH["liquidity_amt"]}

    # ---------------- 背景层：市场状态 / 板块广度 / 所属行业当日强弱 ----------------
    # 只做背景参考，不参与打分、不进 GATES（个股超额有没有意义取决于大盘环境，这一步先让人看见）
    bg, val = None, None
    try:
        bg = df.market_bg(code, q.get("industry"))
        if bg.get("industryPct") is not None and pct is not None and not pre_open:
            bg["excess"] = round(pct - bg["industryPct"], 4)
    except Exception:
        unknown.append("bg")
    try:
        val = {"pe": q.get("pe"), "pb": q.get("pb"),
               "mcap": q.get("mcap"), "floatMcap": q.get("floatMcap")}
        if val["pe"] is None:
            unknown.append("pe")
    except Exception:
        val = None
        unknown.append("valuation")

    # ---------------- 情绪温度 / 谁在买（背景层，不进打分也不进 GATES） ----------------
    # 存在的理由：同一只票的"好"，会随市场温度变味。今天涨停 52 家、最高 6 连板时追一只票，
    # 和跌停 30 家时买同一只票，风险不是一回事 —— 但这不该改判个股本身，只该让人看见温度差。
    sent, bill = None, None
    try:
        sent = df.sentiment()
        if sent.get("zt") is None:
            unknown.append("sentiment")
    except Exception:
        unknown.append("sentiment")
    try:
        bill = df.billboard()
        if bill.get("date") is None:
            unknown.append("billboard")
    except Exception:
        unknown.append("billboard")

    # ---------------- 盘口结构 / 大盘量能（背景层，不进打分也不进 GATES） ----------------
    # 情绪温度只有"几个数"，答不了"封不封得住、接力断没断、钱堆在哪个行业"。
    # 这五个板块全来自同一份池数据 + 两个指数，零新增请求成本。
    pulse, vol = None, None
    try:
        pulse = df.pulses()
        if not pulse.get("ok"):
            unknown.append("pulse")
    except Exception:
        unknown.append("pulse")
    try:
        vol = df.volume()
        if vol.get("sum") is None:
            unknown.append("volume")
    except Exception:
        unknown.append("volume")

    # ---------------- 新鲜度 ----------------
    today_asof = df.market_asof()
    fresh = (asof == today_asof) if (asof and today_asof) else None
    if fresh is None:
        unknown.append("freshness")

    return {
        "code": q.get("code") or code, "name": name, "asof": asof,
        "today_asof": today_asof, "fresh": fresh,
        "price": price, "pct": pct, "updated": q.get("updated"),
        "structure": structure, "volume": volume, "tradable": tradable,
        "flow": flow, "rs": rs, "bg": bg, "valuation": val,
        "sent": sent, "bill": bill, "pulse": pulse, "vol": vol,
        "industry": q.get("industry"),
        "cross": {"items": cross, "count": len(cross)},
        "unknown": unknown,
    }


def _self_test():
    for c in ("600744", "600038", "300313", "000541", "600000"):
        try:
            d = compute(c)
            s, v, t = d["structure"], d["volume"], d["tradable"]
            print("%s %s 价%s pct%s asof=%s fresh=%s" %
                  (d["code"], d["name"], d["price"], d["pct"], d["asof"], d["fresh"]))
            print("   结构: 斜率20=%s 斜率60=%s 距高=%s%% ATR=%s%% flags=%s" %
                  (s["slope20"], s["slope60"], s["pos_hi"], s["atr_pct"], s["flags"]))
            print("   量能: 量比=%s 换手=%s vr5=%s %s%s" %
                  (v["volumeRatio"], v["turnover"], v["vr5"], v["state"],
                   " 背离!" if v["diverge"] else ""))
            print("   可交易: %s %s" % ("OK" if t["ok"] else "不可成交", t["reasons"]))
            f, r = d["flow"], d["rs"]
            print("   资金: %s 主力=%s 元(占成交额 %s%%) 3日累计=%s 状态=%s%s" %
                  (f["asof"], f["main"], f["main_pct"], f["cum3"], f["state"],
                   " 提示:" + f["warn"] if f["warn"] else ""))
            print("   强弱: %s 大盘=%s%% 超额=%s%% 5日超额=%s%% 20日超额=%s%% → %s" %
                  (r["asof"], r["idx_pct"], r["excess"], r["excess5"], r["excess20"], r["state"]))
            print("   冲突: %s" % (d["cross"]["items"] or "无"))
            print("   不可判: %s" % (d["unknown"] or "无"))
        except Exception as e:
            print("%s 失败: %s" % (c, e))


if __name__ == "__main__":
    _self_test()
