# -*- coding: utf-8 -*-
"""
datafeed.py · 统一数据接入层
- 行情快照 / 分时 / K线 / 全市场榜单
- 数据源：东方财富公开接口（可替换为券商 Level-1/2 或 Wind/聚源，接口形状保持不动）
"""

import datetime
import json
import re
import time
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Referer": "https://quote.eastmoney.com/",
    "Accept-Encoding": "identity",
}

_CACHE = {}
_TTL = {"quote": 3, "minute": 20, "kline": 120, "list": 20, "index": 120, "fflow": 120,
        "boards": 300, "bg": 60}

# 每路通道的健康状态：这是"我知道自己瞎了没有"的唯一来源
HEALTH = {}


def _mark(channel, ok, err=""):
    if not channel:
        return
    h = HEALTH.setdefault(channel, {"ok": 0, "err": 0, "last_ok": None,
                                    "last_err": None, "err_msg": ""})
    if ok:
        h["ok"] += 1
        h["last_ok"] = time.strftime("%Y-%m-%d %H:%M:%S")
    else:
        h["err"] += 1
        h["last_err"] = time.strftime("%Y-%m-%d %H:%M:%S")
        h["err_msg"] = str(err)[:120]


def health():
    return {k: dict(v) for k, v in HEALTH.items()}


# 沪深A股（剔除北交所）
FS_ASHARE = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"


def fetch_json(url, ttl=5, channel=None):
    now = time.time()
    hit = _CACHE.get(url)
    if hit and now - hit[0] < ttl:
        return hit[1]
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            body = resp.read().decode("utf-8", "replace")
        data = json.loads(body)
    except Exception as e:
        _mark(channel, False, e)
        raise
    _CACHE[url] = (now, data)
    _mark(channel, True)
    return data


def to_secid(raw):
    code = str(raw or "").strip().lower().replace(" ", "")
    market = None
    m = re.match(r"^(sh|sz|bj)(\d{6})$", code)
    if m:
        market, code = {"sh": "1", "sz": "0", "bj": "0"}[m.group(1)], m.group(2)
    m = re.match(r"^(\d{6})\.(sh|sz|bj)$", code)
    if m:
        code, market = m.group(1), {"sh": "1", "sz": "0", "bj": "0"}[m.group(2)]
    if not re.match(r"^\d{6}$", code):
        raise ValueError("代码格式不正确，请输入 6 位代码，如 600519")
    if market is None:
        market = "1" if code[0] in "69" else "0"
    return "%s.%s" % (market, code), code


def _num(v, scale=100.0, nd=4):
    if v in (None, "-", ""):
        return None
    try:
        return round(float(v) / scale, nd)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 实时快照
def get_quote(code):
    secid, pure = to_secid(code)
    # 注意：单票资金流不用 f62 —— 实测同一只票两次调用给出 3 / 0.03，且与 f184 符号相反，
    # 不可信。资金面一律走 get_fundflow()（fflow/daykline）。这里保持不含 mainNet。
    fields = ("f43,f44,f45,f46,f47,f48,f57,f58,f60,f168,f169,f170,"
              "f50,f116,f117,f162,f167,f127")
    data = fetch_json("https://push2.eastmoney.com/api/qt/stock/get?secid=%s&fields=%s"
                      % (secid, fields), _TTL["quote"], channel="quote")
    d = data.get("data")
    if not d:
        raise ValueError("未查询到该代码：%s" % pure)

    price, pre_close = _num(d.get("f43")), _num(d.get("f60"))
    pre_open = False
    # 开盘前 f43 是 0：用昨收顶上，并明确标 preOpen，避免页面出现 0.00 / -100%
    if (not price) and pre_close:
        price, pre_open = round(pre_close, 4), True
    change, pct = _num(d.get("f169")), _num(d.get("f170"))
    if price is not None and pre_close:
        change, pct = round(price - pre_close, 4), round((price - pre_close) / pre_close * 100, 4)
    if pre_open:
        change, pct = 0.0, 0.0

    return {
        "code": d.get("f57") or pure, "name": d.get("f58") or "", "secid": secid,
        "price": price, "preClose": pre_close, "preOpen": pre_open,
        "open": _num(d.get("f46")), "high": _num(d.get("f44")), "low": _num(d.get("f45")),
        "change": change, "pct": pct,
        "volume": d.get("f47"), "amount": d.get("f48"),
        "turnover": _num(d.get("f168")), "volumeRatio": _num(d.get("f50")),
        "mcap": d.get("f116"), "floatMcap": d.get("f117"),
        "pe": _num(d.get("f162")), "pb": _num(d.get("f167")),
        "industry": d.get("f127"),
        "updated": time.strftime("%H:%M:%S"),
    }


# ---------------------------------------------------------------- 分时
def get_minute(code, days=1):
    secid, pure = to_secid(code)

    def fetch(n):
        url = ("https://push2his.eastmoney.com/api/qt/stock/trends2/get?"
               "secid=%s&fields1=f1,f2,f3,f4,f5,f6,f7,f8&"
               "fields2=f51,f52,f53,f54,f55,f56,f57,f58&iscr=0&ndays=%d" % (secid, n))
        return (fetch_json(url, _TTL["minute"], channel="minute").get("data") or {})

    d = fetch(days)
    rows = d.get("trends") or []
    if not rows and days <= 5:
        # 开盘前（09:15 之前）当天还没有分时，往回多取几天，保证图能画出来
        d2 = fetch(5)
        if d2.get("trends"):
            d, rows = d2, d2["trends"]
    if not rows:
        raise ValueError("未获取到分时数据：%s" % pure)

    buckets, cur = {}, None
    for row in rows:
        p = row.split(",")
        if len(p) < 8:
            continue
        date, hhmm = p[0].split(" ")[0], p[0].split(" ")[1]
        if date != cur:
            cur, buckets[date] = date, []
        buckets[date].append([hhmm, float(p[2]), round(float(p[7]), 3), round(float(p[5] or 0), 1)])

    dates = sorted(buckets)
    asof = dates[-1] if dates else None
    today = time.strftime("%Y-%m-%d")
    partial = bool(asof == today and len(buckets[asof]) < 30)   # 09:30 之前只有集合竞价几个点
    return {"code": d.get("code") or pure, "name": d.get("name") or "",
            "preClose": round(float(d.get("preClose")), 4) if d.get("preClose") else None,
            "asof": asof, "stale": bool(asof and asof != today), "partial": partial,
            "points": len(buckets[asof]) if asof else 0,
            "days": [{"date": k, "points": buckets[k]} for k in dates]}


# ---------------------------------------------------------------- K 线
_KLT = {"day": 101, "week": 102, "month": 103, "m5": 5, "m15": 15, "m30": 30, "m60": 60}
_LMT = {"day": 260, "week": 200, "month": 160, "m5": 400, "m15": 400, "m30": 400, "m60": 400}


def get_kline(code, period="day", limit=None, secid=None):
    """secid 可直传（如 "1.000001" 上证指数），用于相对强弱对照；不传就按代码推。"""
    if period not in _KLT:
        raise ValueError("不支持的周期：%s" % period)
    if secid:
        pure = str(code or secid.split(".")[-1])
    else:
        secid, pure = to_secid(code)
    lmt = limit or _LMT[period]
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?"
           "secid=%s&klt=%d&fqt=1&fields1=f1,f2,f3,f4,f5,f6&"
           "fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61&"
           "beg=0&end=20500101&lmt=%d" % (secid, _KLT[period], lmt))
    d = (fetch_json(url, _TTL["kline"], channel="kline").get("data") or {})
    items = []
    for row in (d.get("klines") or []):
        p = row.split(",")
        if len(p) < 7:
            continue
        items.append({"d": p[0], "o": float(p[1]), "c": float(p[2]), "h": float(p[3]),
                      "l": float(p[4]), "v": float(p[5]), "a": float(p[6]),
                      "amp": float(p[7]) if len(p) > 7 else None,
                      "pct": float(p[8]) if len(p) > 8 else None,
                      "chg": float(p[9]) if len(p) > 9 else None,
                      "turn": float(p[10]) if len(p) > 10 else None})
    if not items:
        raise ValueError("未获取到K线数据：%s" % pure)
    items = items[-lmt:]
    return {"code": d.get("code") or pure, "name": d.get("name") or "",
            "period": period, "items": items}


# ---------------------------------------------------------------- 资金流（日线）
def get_fundflow(code, limit=30):
    """主力/小单/中单/大单/超大单 日线净流入。
    口径来源：东财 fflow/daykline（push2his 才有历史，push2 只给当天）。
    行格式：日期,主力净额,小单,中单,大单,超大单,主力占比%,小单占比%,中单占比%,大单占比%,超大单占比%
    金额单位：元；占比单位：%（相对成交额）。取不到就抛异常，由调用方按"失明"处理。"""
    secid, pure = to_secid(code)
    url = ("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get?"
           "secid=%s&fields1=f1,f2,f3,f7&"
           "fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61&klt=101&lmt=%d"
           % (secid, limit))
    d = (fetch_json(url, _TTL["fflow"], channel="fflow").get("data") or {})
    rows = d.get("klines") or []
    if not rows:
        raise ValueError("未获取到资金流数据：%s" % pure)
    items = []
    for row in rows:
        p = row.split(",")
        if len(p) < 7:
            continue
        items.append({"d": p[0], "main": float(p[1]), "small": float(p[2]),
                      "mid": float(p[3]), "big": float(p[4]), "xbig": float(p[5]),
                      "main_pct": float(p[6])})
    items = items[-limit:]
    return {"code": d.get("code") or pure, "name": d.get("name") or "", "items": items}


# ---------------------------------------------------------------- 全市场榜单
_LIST_FIELDS = ("f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f14,f15,f16,f17,f18,"
                "f20,f21,f23,f62,f100")


def list_snapshot(sort="f3", size=30, market=FS_ASHARE):
    """sort: f3 涨幅 / f10 量比 / f6 成交额 / f8 换手率"""
    url = ("https://push2.eastmoney.com/api/qt/clist/get?"
           "pn=1&pz=%d&po=1&np=1&fltt=2&invt=2&fid=%s&fs=%s&fields=%s"
           % (size, sort, market, _LIST_FIELDS))
    data = fetch_json(url, _TTL["list"], channel="list")
    diff = (data.get("data") or {}).get("diff") or []
    out = []
    for d in diff:
        out.append({
            "code": d.get("f12"), "name": d.get("f14"),
            "price": d.get("f2"), "pct": d.get("f3"), "chg": d.get("f4"),
            "volume": d.get("f5"), "amount": d.get("f6"), "amplitude": d.get("f7"),
            "turnover": d.get("f8"), "pe": d.get("f9"), "volumeRatio": d.get("f10"),
            "high": d.get("f15"), "low": d.get("f16"), "open": d.get("f17"),
            "preClose": d.get("f18"), "mcap": d.get("f20"), "floatMcap": d.get("f21"),
            "pb": d.get("f23"), "mainNet": d.get("f62"), "industry": d.get("f100"),
        })
    return out


# ---------------------------------------------------------------- 交易日基准
def session_minutes(now=None):
    """今天开盘已经走了多少分钟；非交易时段返回 None。

    存在的理由：成交额、换手率都是**当日累计值**。09:35 的 0.4% 换手
    和一个正常交易日收盘的 0.4% 完全不是一回事，拿收盘口径的门槛去判开盘头一小时，
    必然把全市场判成"换手太小、成交额不够"。所以这段时间里它们只能算"不可判"。
    """
    n = now or datetime.datetime.now()
    if n.weekday() >= 5:
        return None
    hm = n.hour * 60 + n.minute
    if 9 * 60 + 30 <= hm <= 11 * 60 + 30:
        return hm - (9 * 60 + 30)
    if 13 * 60 <= hm <= 15 * 60:
        return 120 + (hm - 13 * 60)
    return None


# ---------------------------------------------------------------- 背景层（市场状态 / 板块广度 / 行业相对强弱）
def boards():
    """行业板块清单（名称 → 板块代码 + 当日涨跌）。缓存 300 秒，一次 5 页。

    m:90+t:2 里混着一级/二级/三级行业，名称有重复层级但代码不同；
    这里不去重层级，只按"名称精确匹配"把个股的 f127 对到同名的那个板块。
    """
    out = []
    for pn in range(1, 6):
        url = ("https://push2.eastmoney.com/api/qt/clist/get?pn=%d&pz=100&po=1&np=1"
               "&fltt=2&invt=2&fid=f3&fs=m:90+t:2&fields=f12,f14,f3,f104,f105"
               % pn)
        try:
            data = fetch_json(url, _TTL["boards"], channel="list")
        except Exception:
            break
        diff = (data.get("data") or {}).get("diff") or []
        for d in diff:
            # 注意：clist 带 fltt=2 时 f3 已经是"百分数原值"（1.18 就是 1.18%），
            # 再除 100 会得到 0.0118 这种量级错误（这个坑本轮实测踩到过一次）
            try:
                bp = round(float(d.get("f3")), 4)
            except (TypeError, ValueError):
                bp = None
            out.append({"code": d.get("f12"), "name": d.get("f14"),
                        "pct": bp, "up": d.get("f104"), "down": d.get("f105")})
        if len(diff) < 100:
            break
    return out


def market_bg(code=None, industry=None):
    """一只票所在的市场背景：三大指数、板块广度、所属行业的当日强弱。

    为什么不做"个股涨跌家数"：指数接口的 f104/f105 实测恒为 0，clist 一次最多回 100 条，
    全市场要翻 60 页（约 7 秒/轮），实时看板不该背这个成本。所以这里给的是**板块广度**
    （496 个行业/概念板块里上涨/下跌的个数），标签也照这个写，不冒充"个股涨跌家数"。
    """
    idx = []
    try:
        u = ("https://push2.eastmoney.com/api/qt/ulist.np/get?"
             "secids=1.000001,0.399001,0.399006&fields=f12,f14,f3,f6")
        data = fetch_json(u, _TTL["bg"], channel="index")
        for d in (data.get("data") or {}).get("diff") or []:
            idx.append({"code": d.get("f12"), "name": d.get("f14"),
                        "pct": _num(d.get("f3"), 100.0, 4), "amount": d.get("f6")})
    except Exception:
        pass

    bl = boards()
    up = sum(1 for b in bl if (b["pct"] or 0) > 0)
    down = sum(1 for b in bl if (b["pct"] or 0) < 0)
    flat = len(bl) - up - down

    ind_name, ind_pct, excess = industry, None, None
    if industry:
        hit = [b for b in bl if b["name"] == industry]
        if hit:
            # 同名多层级时取成分数最多的那个（三级行业通常最具体）
            hit.sort(key=lambda b: (b.get("up") or 0) + (b.get("down") or 0), reverse=True)
            ind_pct = hit[0]["pct"]
    return {"idx": idx, "boardsUp": up, "boardsDown": down, "boardsFlat": flat,
            "boardsTotal": len(bl), "industry": ind_name, "industryPct": ind_pct,
            "asof": time.strftime("%H:%M:%S")}


def market_asof():
    """最近一个有 K 线的交易日（用上证指数当基准）。

    这是整个系统"现在是哪一天"的唯一真相来源：
    周日跑一轮，它必须诚实地告诉你数据是周五的，而不是装成今天的。
    取不到就返回 None —— 由调用方按"失明"处理，绝不默认放行。
    """
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?"
           "secid=1.000001&klt=101&fqt=1&fields1=f1,f2&fields2=f51,f52"
           "&beg=0&end=20500101&lmt=3")
    for _ in range(3):
        try:
            d = (fetch_json(url, _TTL["index"], channel="index").get("data") or {})
            rows = d.get("klines") or []
            if rows:
                return rows[-1].split(",")[0]
        except Exception:
            continue
    return None


# ---------------------------------------------------------------- 情绪温度 / 谁在买（只做背景，不进闸门）
_POOL_UT = "7eea3edcaed734bea9cbfc24409ed989"


def _pool(kind, date):
    """涨停池 / 跌停池 / 炸板池。date 传 8 位交易日；取不到就返回空，绝不拿空当"0 家"。

    排序字段按池子选，不能统一：炸板池用 `sort=fund:asc` 会返回 `tc=33` 但 `pool=[]`
    （静默失效 —— 只看 len 会得出"今天 0 家炸板"），换 `fbt:asc` 才能拿到全量。
    """
    sort = "fbt:asc" if kind == "TopicZBPool" else "fund:asc"
    url = ("https://push2ex.eastmoney.com/get%s?ut=%s&dpt=wz.ztzt"
           "&Pageindex=0&pagesize=100&sort=%s&date=%s" % (kind, _POOL_UT, sort, date))
    try:
        d = fetch_json(url, 60, channel="pool")
    except Exception:
        return None
    return (d.get("data") or {}).get("pool")


def trading_days(n=3):
    """最近 n 个交易日（升序）。用来给"昨日涨停"对日期，不靠猜星期几。"""
    try:
        k = get_kline("000001", limit=n, secid="1.000001")
    except Exception:
        return []
    return [b["d"] for b in k.get("items") or []]


def _pct_of(codes):
    """一批代码的当日涨跌幅（E 东财 ulist 一次 50 个）。返回 (pcts, 取不到的个数)。"""
    out, miss = [], 0
    for i in range(0, len(codes), 50):
        chunk = codes[i:i + 50]
        secids = ",".join(("1." if c[0] in "69" else "0.") + c for c in chunk)
        url = ("https://push2.eastmoney.com/api/qt/ulist.np/get?secids=%s"
               "&fields=f12,f3&fltt=2&invt=2" % secids)
        try:
            d = fetch_json(url, 20, channel="quote")
        except Exception:
            miss += len(chunk)
            continue
        got = {x.get("f12"): x.get("f3") for x in (d.get("data") or {}).get("diff") or []}
        for c in chunk:
            v = got.get(c)
            if isinstance(v, (int, float)):
                out.append(float(v))
            else:
                miss += 1
    return out, miss


def sentiment():
    """市场情绪温度：涨停家数、跌停家数、最高连板、昨日涨停今日溢价。

    只描述"今天市场在什么温度"，**不进 GATES、不进打分** —— 同一只票的好坏
    不该因为当天情绪热就被改判。取不到的字段一律 None，前端标"不可得"。
    """
    days = trading_days(3)
    today = days[-1] if days else None
    prev = days[-2] if len(days) >= 2 else None
    fmt = lambda d: d.replace("-", "") if d else None

    out = {"today": today, "prev": prev, "zt": None, "dt": None, "maxLbc": None,
           "maxLbcName": None, "prevZt": None, "premium": None, "prevUp": None,
           "prevQuoted": None, "asof": time.strftime("%H:%M:%S")}

    tpool = _pool("TopicZTPool", fmt(today)) if today else None
    if tpool is not None:
        out["zt"] = len(tpool)
        if tpool:
            top = max(tpool, key=lambda p: p.get("lbc") or 0)
            out["maxLbc"] = top.get("lbc")
            out["maxLbcName"] = top.get("n")

    dpool = _pool("TopicDTPool", fmt(today)) if today else None
    if dpool is not None:
        out["dt"] = len(dpool)

    ppool = _pool("TopicZTPool", fmt(prev)) if prev else None
    if ppool is not None:
        out["prevZt"] = len(ppool)
        codes = [p["c"] for p in ppool if p.get("c")]
        pcts, miss = _pct_of(codes)
        if pcts:
            out["premium"] = round(sum(pcts) / len(pcts), 2)
            out["prevUp"] = sum(1 for x in pcts if x > 0)
            out["prevQuoted"] = len(pcts)
        if miss:
            out["prevMiss"] = miss
    return out


def billboard():
    """最近的龙虎榜：上榜家数 + 净买入合计。

    只做"谁在买"的背景温度。注意两点：
    1) filter=(SECURITY_CODE="...") 在本接口实测被**静默忽略**（count 仍返回全量 27 万条），
       所以这里不按票过滤，而是按 TRADE_DATE 倒序取最新一天、逐页读到日期变化为止；
    2) 龙虎榜 T 日盘后才发布，盘中看到的必然是上一交易日的，asof 写清楚。
    """
    rows, page = [], 1
    while page <= 3:
        url = ("https://datacenter-web.eastmoney.com/api/data/v1/get?"
               "reportName=RPT_DAILYBILLBOARD_DETAILSNEW&columns=ALL&pageNumber=%d&pageSize=200"
               "&sortTypes=-1&sortColumns=TRADE_DATE&source=WEB&client=WEB" % page)
        try:
            d = fetch_json(url, 120, channel="lhb")
        except Exception:
            break
        got = ((d.get("result") or {}).get("data")) or []
        if not got:
            break
        rows.extend(got)
        if len(got) < 200:
            break
        page += 1

    if not rows:
        return {"date": None, "count": None, "netBuy": None, "asof": None}
    date = (rows[0].get("TRADE_DATE") or "")[:10]
    by_code = {}
    for r in rows:
        if (r.get("TRADE_DATE") or "")[:10] != date:
            continue
        code = r.get("SECURITY_CODE")
        net = r.get("BILLBOARD_NET_AMT")
        if code is None:
            continue
        by_code[code] = by_code.get(code, 0) + (net or 0)
    net_sum = round(sum(by_code.values()) / 1e8, 2) if by_code else None
    return {"date": date, "count": len(by_code), "netBuy": net_sum,
            "asof": time.strftime("%H:%M:%S")}


# ---------------------------------------------------------------- 盘口结构脉冲
_PULSE = {"t": 0, "v": None}
_VOL = {"t": 0, "v": None}


def pulses(ttl=60):
    """盘口结构脉冲：从涨停池 / 炸板池直接算出的结构指标。

    为什么要有这一层：原来只有"涨停 51 / 跌停 1 / 最高 6 连板"三个数，
    它告诉你温度高不高，但答不了"封不封得住、接力断没断、钱堆在哪个行业"。
    这些全在同一个池子里，零新增请求。

    口径说明（都写在这里，免得日后自己都记不清）：
        炸板率 = 炸板家数 / (涨停 + 炸板)  —— 分母用"摸过涨停的家数"，不是全市场
        一字板 = 9:25:30 前封板且全天没炸过
        早封   = 10:30 前首次封板
        封单中位 = 涨停股封单额的中位数（抗极端值）
    一律不进 GATES、不进打分：今天封板质量差，不该把某只票直接判死。
    """
    if _PULSE["v"] is not None and time.time() - _PULSE["t"] < ttl:
        return _PULSE["v"]
    days = trading_days(3)
    today = days[-1] if days else None
    f8 = today.replace("-", "") if today else None
    out = {"asof": time.strftime("%H:%M:%S"), "today": today, "ok": False,
           "zt": None, "zb": None, "dt": None, "zbRate": None,
           "ladder": None, "maxLbc": None, "maxLbcName": None,
           "sealSum": None, "sealMed": None, "sealTop": None,
           "early": None, "oneWord": None, "indTop": None, "indN": None}
    if not f8:
        return out

    zt = _pool("TopicZTPool", f8)
    zb = _pool("TopicZBPool", f8)
    dt = _pool("TopicDTPool", f8)
    if zt is None:
        return out          # 取不到就是取不到，不拿空当 0 家
    zt = zt or []
    zb = zb or []
    out["ok"] = True
    out["zt"], out["zb"] = len(zt), len(zb)
    out["dt"] = None if dt is None else len(dt)

    touched = len(zt) + len(zb)
    if touched:
        out["zbRate"] = round(len(zb) * 100.0 / touched, 1)

    # 连板梯队：1 板 / 2 板 / 3 板 / 4 板+（梯队断层意味着接力断了）
    tiers = {"首板": 0, "2板": 0, "3板": 0, "4板+": 0}
    max_lbc, max_name = None, None
    for p in zt:
        lbc = p.get("lbc") or 1
        tiers["首板" if lbc <= 1 else "2板" if lbc == 2 else "3板" if lbc == 3 else "4板+"] += 1
        if max_lbc is None or lbc > max_lbc:
            max_lbc, max_name = lbc, p.get("n")
    if zt:
        out["ladder"] = [{"k": k, "n": v} for k, v in tiers.items()]
        out["maxLbc"], out["maxLbcName"] = max_lbc, max_name

    # 封板强度：合计 / 中位 / 最强
    funds = sorted(p.get("fund") for p in zt if p.get("fund"))
    if funds:
        out["sealSum"] = round(sum(funds) / 1e8, 2)
        out["sealMed"] = round(funds[len(funds) // 2] / 1e4, 0)
        top = max(zt, key=lambda p: p.get("fund") or 0)
        out["sealTop"] = {"n": top.get("n"), "v": round((top.get("fund") or 0) / 1e8, 2)}

    # 封板节奏：一字板 / 早盘封板（fbt 是 HHMMSS 整数）
    def _hhmmss(v):
        try:
            s = "%06d" % int(v)
            return int(s[:2]) * 3600 + int(s[2:4]) * 60 + int(s[4:])
        except (TypeError, ValueError):
            return None

    fb = [_hhmmss(p.get("fbt")) for p in zt]
    fb = [x for x in fb if x is not None]
    if fb:
        out["early"] = sum(1 for x in fb if x <= 10 * 3600 + 30 * 60)
        out["oneWord"] = sum(
            1 for p in zt
            if (_hhmmss(p.get("fbt")) is not None and _hhmmss(p.get("fbt")) <= 9 * 3600 + 25 * 60 + 30
                and (p.get("zbc") or 0) == 0))

    # 行业集中度：涨停堆在哪些行业（集中=主线明确，分散=乱炒）
    cnt = {}
    for p in zt:
        nm = p.get("hybk")
        if nm:
            cnt[nm] = cnt.get(nm, 0) + 1
    if cnt:
        top = sorted(cnt.items(), key=lambda kv: -kv[1])[:3]
        out["indTop"] = [{"k": k, "n": v} for k, v in top]
        out["indN"] = len(cnt)

    _PULSE["t"], _PULSE["v"] = time.time(), out
    return out


def volume(ttl=60):
    """两市成交额与量能比：今日 vs 近 5 个交易日全天均额。

    两个诚实点：
    1) 指数 f104/f105（涨跌家数）实测恒为空，别指望；能用的只有 f43/f48/f170。
    2) 盘中今天还没走完，比值天生偏低 —— 所以带 `quoted`（是否已收盘），
       前端必须标"未收盘"，不然这个数会被当成"今天缩量"。
    """
    if _VOL["v"] is not None and time.time() - _VOL["t"] < ttl:
        return _VOL["v"]
    out = {"asof": time.strftime("%H:%M:%S"), "quoted": None,
           "sh": None, "sz": None, "sum": None, "avg5": None,
           "ratio": None, "pct": None}
    amt = {}
    for secid, nm in (("1.000001", "sh"), ("0.399001", "sz")):
        try:
            d = fetch_json("https://push2.eastmoney.com/api/qt/stock/get?secid=%s"
                           "&fields=f43,f48,f170&invt=2&fltt=2" % secid, 30)["data"] or {}
        except Exception:
            return out
        a = d.get("f48")
        if isinstance(a, (int, float)) and a > 0:
            amt[nm] = a
        if nm == "sh" and isinstance(d.get("f170"), (int, float)):
            out["pct"] = round(d["f170"], 2)
    if "sh" not in amt and "sz" not in amt:
        return out
    out["sh"] = round(amt.get("sh", 0) / 1e8, 0)
    out["sz"] = round(amt.get("sz", 0) / 1e8, 0)
    out["sum"] = round((amt.get("sh", 0) + amt.get("sz", 0)) / 1e8, 0)

    # 近 5 个交易日的全天均额（用指数日 K 的 a 字段，含今日，所以取前 5 根）
    base = 0.0
    ok = True
    for secid, nm in (("1.000001", "sh"), ("0.399001", "sz")):
        try:
            it = (get_kline("000001" if nm == "sh" else "399001", "day", 7,
                            secid=secid).get("items") or [])[:-1]
            vals = [x.get("a") for x in it if x.get("a")]
            if not vals:
                ok = False
                break
            base += sum(vals[-5:]) / len(vals[-5:])
        except Exception:
            ok = False
            break
    if ok and base > 0:
        out["avg5"] = round(base / 1e8, 0)
        out["ratio"] = round((amt.get("sh", 0) + amt.get("sz", 0)) / base, 2)
    # 收盘后这个比值才有意义；盘中只当"跑了一半"的参考
    now = datetime.datetime.now()
    hhmm = now.strftime("%H:%M")
    out["quoted"] = not (now.weekday() < 5 and ("09:30" <= hhmm <= "15:00"))

    _VOL["t"], _VOL["v"] = time.time(), out
    return out
