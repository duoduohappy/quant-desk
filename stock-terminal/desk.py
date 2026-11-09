# -*- coding: utf-8 -*-
"""
desk.py · 赛博交易室 · 8 智能体（A股版）
------------------------------------------------
分工原则：
  - 判断类（发现/审查/资金面）→ 规则 + 可解释评分
  - 算术类（仓位/费用/撮合/风控）→ 确定性代码，绝不交给模型
  - 不可逆动作（买入）→ 必经 APPROVE 闸门（纸面模式可自动批准，但留痕）

A 股真实约束已内置：
  T+1、涨跌停、100 股整手、佣金/印花税/过户费、停牌与 ST 剔除
"""

import json
import math
import os
import re
import threading
import time
from datetime import datetime

import datafeed as df
import factors as facts   # 因子：只做证据与有界微调，不做决策（plan 用惰性导入避免循环依赖）

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "desk_state.json")
AUDIT_FILE = os.path.join(BASE_DIR, "desk_audit.jsonl")
PARAMS_FILE = os.path.join(BASE_DIR, "desk_params.json")
PARAMS_DIR = os.path.join(BASE_DIR, "desk_params.d")   # 每个版本一份快照，回滚就是拿它回来
LOCK = threading.RLock()

INIT_EQUITY = 100000.0
RISK_PER_TRADE = 0.010     # 单笔最大风险：权益的 1%
MAX_POS_PCT = 0.25         # 单票最大仓位 25%
DAILY_LOSS_HALT = 0.03     # 日亏 3% 停摆
COMMISSION = 0.00025       # 佣金万 2.5
MIN_COMM = 5.0
STAMP_TAX = 0.0005         # 印花税（卖出）
TRANSFER = 0.00001         # 过户费

# ---------------------------------------------------------------- 可调参数（带版本）
# 为什么外置：自进化要改的就是"权重和惩罚"，但改必须能退回去。
# 所以参数不写在代码里，写进 desk_params.json；每次写入先把旧版本存一份快照。
# 代码里只留**默认值**（文件丢了也能跑），以及不可调的硬约束（风控/费用/闸门）。
DEFAULT_PARAMS = {
    "version": 0,
    "updated_at": None,
    "source": "default",
    "note": "初始默认值，未校准过",
    "scan": {"w_pct": 0.28, "w_vr": 0.18, "w_turn": 0.16,
             "w_amt": 0.20, "w_flow": 0.10, "w_mcap": 0.08, "scale": 0.88},
    "shadow": {"min": 0.55, "pen_atr9": 0.10, "pen_atr6": 0.05, "pen_pos70": 0.08,
               "pen_flow": 0.08, "pen_rs": 0.08, "pen_pe": 0.05, "cap": 0.30},
}
# 说明：这里**故意没有**"影子分参与排序"的开关。
# 影子分一旦能影响排序，它就不再是"对照组"，对照也就废了 —— 想让它生效，
# 正确做法是改 scan 权重（params_apply 写新版本、可回滚），而不是给影子分开个开关。

_params_cache = {"mtime": None, "obj": None}

# 参数允许的取值范围。作用不是"防止有人故意搞破坏"，而是防止一次手滑/一次错误 patch
# 把打分整体放大五倍 —— 参数改动必须能在事后一分钟内解释清楚，越界就不是调参是事故。
_PARAM_BOUNDS = {
    "scan": {"w_pct": (0.0, 1.0), "w_vr": (0.0, 1.0), "w_turn": (0.0, 1.0),
             "w_amt": (0.0, 1.0), "w_flow": (0.0, 1.0), "w_mcap": (0.0, 1.0),
             "scale": (0.50, 1.00)},
    "shadow": {"min": (0.0, 1.0), "pen_atr9": (0.0, 0.30), "pen_atr6": (0.0, 0.30),
               "pen_pos70": (0.0, 0.30), "pen_flow": (0.0, 0.30), "pen_rs": (0.0, 0.30),
               "pen_pe": (0.0, 0.30), "cap": (0.0, 0.50)},
}


def _clamp_params(obj):
    """把越界参数压回允许范围，返回 (修正后的对象, 修正说明列表)。"""
    fixed = []
    for sec, fields in _PARAM_BOUNDS.items():
        d = obj.get(sec)
        if not isinstance(d, dict):
            continue
        for k, (lo, hi) in fields.items():
            v = d.get(k)
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            if v < lo or v > hi:
                d[k] = min(hi, max(lo, v))
                fixed.append("%s.%s %s→%s（允许 %s~%s）" % (sec, k, v, d[k], lo, hi))
    return obj, fixed

STATIONS = [
    ("CHIEF", "交易台主管", "never takes a position", "永不下场持仓"),
    ("SCAN", "阿尔法猎手", "finding what's next", "找到下一个"),
    ("VET", "标的审查", "is it real, can it trade", "这票能买吗"),
    ("SOCIAL", "资金与情绪", "where the money goes", "钱在往哪走"),
    ("SIZE", "仓位计算", "how much", "买多少"),
    ("FILLS", "执行成交", "execution", "动手成交"),
    ("RISK", "持仓风控", "testing, watching", "盯着持仓"),
    ("CRAWLER", "数据采集", "kinda first", "最先看到"),
]


# ---------------------------------------------------------------- 状态
def _today():
    return datetime.now().strftime("%Y-%m-%d")


def _deepmerge(base, over):
    """把 over 覆盖到 base 上，返回新对象。缺字段回落默认值，别让半个配置文件把系统弄崩。"""
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            out[k] = _deepmerge(base[k], v)
        else:
            out[k] = v
    return out


def params():
    """当前生效的可调参数（按文件 mtime 缓存，改文件不用重启）。"""
    try:
        mt = os.path.getmtime(PARAMS_FILE)
    except OSError:
        return dict(DEFAULT_PARAMS)
    if _params_cache["mtime"] == mt and _params_cache["obj"] is not None:
        return _params_cache["obj"]
    try:
        with open(PARAMS_FILE, "r", encoding="utf-8") as f:
            disk = json.load(f)
        obj = _deepmerge(DEFAULT_PARAMS, disk)
    except Exception:
        obj = dict(DEFAULT_PARAMS)
    _params_cache["mtime"], _params_cache["obj"] = mt, obj
    return obj


def params_apply(patch, source="manual", note=""):
    """写入新参数：先归档旧版本，再落新文件，并写审计。

    回滚靠的是 desk_params.d/v{n}.json —— 没有快照就不许改参数，
    所以这里先写快照再写主文件，顺序不能反（中途挂了至少旧版还在）。
    """
    with LOCK:
        cur = params()
        old_ver = int(cur.get("version") or 0)
        # 无条件归档旧版本（含首次写入时的 v0 = 出厂默认）：
        # 回滚到默认值必须永远可用，所以 v0 快照也要落盘
        os.makedirs(PARAMS_DIR, exist_ok=True)
        snap = os.path.join(PARAMS_DIR, "v%d.json" % old_ver)
        if not os.path.exists(snap):
            with open(snap, "w", encoding="utf-8") as f:
                json.dump(cur, f, ensure_ascii=False, indent=1)
        new = _deepmerge(cur, patch or {})
        new, fixed = _clamp_params(new)
        new["version"] = old_ver + 1
        new["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        new["source"] = source
        if fixed:
            new["note"] = "%s（已修正越界：%s）" % (note or cur.get("note") or "", "；".join(fixed))
        else:
            new["note"] = note or cur.get("note") or ""
        tmp = PARAMS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(new, f, ensure_ascii=False, indent=1)
        os.replace(tmp, PARAMS_FILE)
        # 新版本也留一份快照，回滚到"当前"时不用特殊处理
        os.makedirs(PARAMS_DIR, exist_ok=True)
        with open(os.path.join(PARAMS_DIR, "v%d.json" % new["version"]), "w",
                  encoding="utf-8") as f:
            json.dump(new, f, ensure_ascii=False, indent=1)
        _params_cache["mtime"] = None
        audit({"agent": "CHIEF", "action": "PARAMS_APPLY", "from": old_ver,
               "to": new["version"], "source": source, "note": note,
               "clamped": fixed,
               "scan": new.get("scan"), "shadow": new.get("shadow")})
        return new


def params_versions():
    """已归档的版本列表（给回滚用）。"""
    out = []
    if not os.path.isdir(PARAMS_DIR):
        return out
    for name in sorted(os.listdir(PARAMS_DIR)):
        if not (name.startswith("v") and name.endswith(".json")):
            continue
        path = os.path.join(PARAMS_DIR, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                o = json.load(f)
            out.append({"version": int(o.get("version") or 0),
                        "updated_at": o.get("updated_at"), "source": o.get("source"),
                        "note": o.get("note"), "file": path})
        except Exception:
            continue
    return out


def params_rollback(to_version):
    """回滚到某个已归档版本。回滚本身也要留痕（并先把当前版本归档）。

    版本号只增不减：回滚是把**旧配置**当成**新版本**发布，
    而不是把 version 倒退回去 —— 否则下次改参数会复用同一个号、覆盖掉历史快照，
    那"能回滚"就是假的（第一次回滚之后，历史就没了）。
    """
    with LOCK:
        path = os.path.join(PARAMS_DIR, "v%d.json" % int(to_version))
        if not os.path.exists(path):
            raise ValueError("没有 v%d 这版快照，不能回滚" % int(to_version))
        with open(path, "r", encoding="utf-8") as f:
            snap = json.load(f)
        os.makedirs(PARAMS_DIR, exist_ok=True)
        cur = params()
        # 先把当前这份存起来，回滚错了还能再回来
        with open(os.path.join(PARAMS_DIR, "v%d.json" % int(cur.get("version") or 0)),
                  "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=1)
        maxv = max([int(x["version"]) for x in params_versions()] + [0, int(cur.get("version") or 0)])
        new = dict(snap)
        new["version"] = maxv + 1
        new["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        new["source"] = "rollback:v%d" % int(to_version)
        new["rolled_back_to"] = int(to_version)
        new["note"] = "回滚到 v%d 的配置（发布为 v%d）" % (int(to_version), new["version"])
        tmp = PARAMS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(new, f, ensure_ascii=False, indent=1)
        os.replace(tmp, PARAMS_FILE)
        with open(os.path.join(PARAMS_DIR, "v%d.json" % new["version"]), "w",
                  encoding="utf-8") as f:
            json.dump(new, f, ensure_ascii=False, indent=1)
        _params_cache["mtime"] = None
        audit({"agent": "CHIEF", "action": "PARAMS_ROLLBACK",
               "from": cur.get("version"), "to": int(to_version),
               "new_version": new["version"]})
        return new


def default_state():
    return {
        "version": 1,
        "day": _today(),
        "init": INIT_EQUITY,
        "equity": INIT_EQUITY,
        "cash": INIT_EQUITY,
        "day_start_equity": INIT_EQUITY,
        "halt": False,
        "cycle": 0,
        "last_run": None,
        "settings": {"auto_approve": False, "max_leads_per_cycle": 6,
                     "max_new_per_cycle": 2},
        "positions": [],
        "trades": [],
        "leads": [],
        "events": [],
        "stats": {"scanned": 0, "cleared": 0, "confirmed": 0,
                  "filled": 0, "closed": 0, "wins": 0, "losses": 0},
        "ledger": {"ok": True, "diff": 0.0},
        "freshness": {"asof": None, "stale": False, "blind": False},
        "stations": {sid: {"state": "idle", "score": None, "metrics": {},
                           "msg": "", "at": None} for sid, *_ in STATIONS},
    }


def load_state():
    if not os.path.exists(STATE_FILE):
        return default_state()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            st = json.load(f)
        base = default_state()
        base.update(st)
        for sid, *_ in STATIONS:
            base["stations"].setdefault(sid, {"state": "idle", "score": None,
                                              "metrics": {}, "msg": "", "at": None})
        return base
    except Exception:
        return default_state()


def save_state(st):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


def audit(row):
    row["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(AUDIT_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def ev(st, who, text, kind="info"):
    st["events"].insert(0, {"t": datetime.now().strftime("%H:%M:%S"),
                            "who": who, "text": text, "kind": kind})
    del st["events"][120:]


def set_station(st, sid, state=None, score=None, metrics=None, msg=None):
    s = st["stations"][sid]
    if state:
        s["state"] = state
    if score is not None:
        s["score"] = round(float(score), 3)
    if metrics:
        s["metrics"].update(metrics)
    if msg is not None:
        s["msg"] = msg
    s["at"] = datetime.now().strftime("%H:%M:%S")


# ---------------------------------------------------------------- 指标
def atr(bars, n=14):
    if len(bars) < n + 1:
        return 0.0
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-n:]) / n


def ma(bars, n):
    if len(bars) < n:
        return None
    return sum(b["c"] for b in bars[-n:]) / n


def band(v, lo, hi, best_lo, best_hi):
    """分段打分：区间外 0 分，最优区间 1 分，中间线性过渡"""
    if v is None:
        return 0.0
    if v < lo or v > hi:
        return 0.0
    if best_lo <= v <= best_hi:
        return 1.0
    if v < best_lo:
        return (v - lo) / max(best_lo - lo, 1e-9)
    return (hi - v) / max(hi - best_hi, 1e-9)


def limit_pct(code):
    """涨跌停幅度：创业板/科创板 20%，其余 10%"""
    return 20.0 if code.startswith("30") or code.startswith("688") else 10.0


def fee_buy(amount):
    return max(MIN_COMM, round(amount * COMMISSION, 2)) + round(amount * TRANSFER, 2)


def fee_sell(amount):
    return max(MIN_COMM, round(amount * COMMISSION, 2)) \
        + round(amount * STAMP_TAX, 2) + round(amount * TRANSFER, 2)


# ---------------------------------------------------------------- CRAWLER
def agent_crawler(st):
    t0 = time.time()
    merged = {}
    lists = {}
    for name, sort in (("涨幅榜", "f3"), ("量比榜", "f10"), ("成交额榜", "f6")):
        try:
            rows = df.list_snapshot(sort=sort, size=40)
            lists[name] = rows
            for r in rows:
                if not r.get("code"):
                    continue
                merged.setdefault(r["code"], {}).update(r)
                merged[r["code"]].setdefault("sources", [])
                merged[r["code"]]["sources"].append(name)
        except Exception as e:
            ev(st, "CRAWLER", "榜单拉取失败（%s）：%s" % (name, e), "alert")
    took = round((time.time() - t0) * 1000)
    set_station(st, "CRAWLER", "working",
                metrics={"sources": "%d/3" % len(lists),
                         "universe": len(merged), "latency": "%dms" % took},
                msg="已拉到 %d 只标的 · 耗时 %dms" % (len(merged), took))
    ev(st, "CRAWLER", "采集完成：%d 只标的，3 个榜单，%dms" % (len(merged), took))
    return list(merged.values())


# ---------------------------------------------------------------- SCAN
def shadow_score(base, fxc, atr_pct, pos60, pe):
    """影子分：把"风险"折进排序分，而不是只在 VET 闸门里一刀切。

    单一假设，别改着改着变成第二个变量：线上分数（base）一分不动，只按可复核的
    四条风险做**有界折价**（上限 30%），看"如果当初让风险参与排序会怎样"。

    为什么值得单独记：线上分数奖励的是"热"（涨幅/量比/换手/成交额），风险只在
    VET 里当硬门槛 —— 结果是"最热的票排在前面，然后被 position/valuation 一刀砍掉"。
    影子分就是把这个矛盾量化出来，等标签够了再决定要不要真的改权重。

    **只写审计、只做对照，绝不参与任何决策**（不改 score、不改 verdict、不改排序）。
    各项惩罚与上限、判"会被选中"的门槛都读 desk_params.json（可校准、可回滚）。
    """
    pen, why = 0.0, []
    P = params()["shadow"]
    if atr_pct is not None:
        if atr_pct > 9:
            pen += P["pen_atr9"]; why.append("波动>9%")
        elif atr_pct > 6:
            pen += P["pen_atr6"]; why.append("波动>6%")
    if pos60 is not None and pos60 < 70:
        pen += P["pen_pos70"]; why.append("离60日高点<70%")
    fl = (fxc or {}).get("flow") or {}
    if fl.get("main_pct") is not None and fl["main_pct"] < -3:
        pen += P["pen_flow"]; why.append("主力净流出>3%")
    rs = (fxc or {}).get("rs") or {}
    if rs.get("excess5") is not None and rs["excess5"] < -3:
        pen += P["pen_rs"]; why.append("5日弱于大盘>3%")
    if pe is not None and pe > 100:
        pen += P["pen_pe"]; why.append("PE>100")
    pen = round(min(pen, P["cap"]), 3)
    s = round(max(0.0, (base or 0) * (1 - pen)), 3)
    return {"s": s, "pen": pen, "why": why, "sel": bool(s >= P["min"]),
            "base": base, "mode": P.get("mode", "record")}


def _factor_adjust(fxc):
    """把因子结论换成"有界微调"（±6%）。返回 (adj, notes)。
    只奖励/惩罚能被数据支撑的三件事：相对强弱、资金是否持续、因子是否打架。
    决策权仍在 VET 的硬门槛与人工闸门，这里只调排序。"""
    if not fxc:
        return 0.0, ["因子不可判"]
    adj = 0.0
    notes = []
    rs = fxc.get("rs") or {}
    fl = fxc.get("flow") or {}
    e5 = rs.get("excess5")
    if e5 is not None:
        if e5 >= 3.0:
            adj += 0.03; notes.append("强于大盘 %+.1f%%" % e5)
        elif e5 <= -3.0:
            adj -= 0.03; notes.append("弱于大盘 %+.1f%%" % e5)
    mp, cum3 = fl.get("main_pct"), fl.get("cum3")
    if mp is not None and mp >= 2.0 and (cum3 or 0) > 0:
        adj += 0.02; notes.append("主力连续净流入")
    cross = (fxc.get("cross") or {}).get("count") or 0
    if cross:
        adj -= 0.04; notes.append("因子冲突 %d 项" % cross)
    if (fxc.get("volume") or {}).get("diverge"):
        adj -= 0.02; notes.append("价新高但量缩")
    return max(-0.06, min(0.06, adj)), notes


def agent_scan(st, rows):
    cands = []
    for r in rows:
        code, name = r.get("code"), r.get("name") or ""
        pct, vr = r.get("pct"), r.get("volumeRatio")
        turn, amt = r.get("turnover"), r.get("amount")
        mcap, flow = r.get("mcap"), r.get("mainNet")
        if not code or pct is None:
            continue
        if "ST" in name.upper() or "退" in name:
            continue
        if code.startswith("8") or code.startswith("4"):   # 北交所不参与
            continue
        if pct >= limit_pct(code) - 0.8:                   # 板上一字，买不进
            continue
        if not amt or amt < 1e8:                           # 成交额 < 1亿
            continue

        s_pct = band(pct, 1.0, limit_pct(code) - 0.8, 3.0, 5.5)
        s_vr = band(vr, 1.0, 12.0, 2.5, 4.5)
        s_turn = band(turn, 1.5, 30.0, 4.0, 10.0)
        s_amt = band(amt / 1e8, 1.0, 50.0, 3.0, 8.0)
        s_flow = band((flow or 0) / amt, -0.10, 0.30, 0.02, 0.12)
        s_mcap = band((mcap or 0) / 1e8, 20, 3000, 40, 300)

        # 权重来自 desk_params.json（可校准、可回滚）；按总和归一，
        # 这样调某一项只改比例，不会因为手滑把全表分数整体放大缩小
        P = params()["scan"]
        w = {"pct": P["w_pct"], "vr": P["w_vr"], "turn": P["w_turn"],
             "amt": P["w_amt"], "flow": P["w_flow"], "mcap": P["w_mcap"]}
        tot = sum(w.values()) or 1.0
        score = (w["pct"] * s_pct + w["vr"] * s_vr + w["turn"] * s_turn +
                 w["amt"] * s_amt + w["flow"] * s_flow + w["mcap"] * s_mcap) / tot
        # 满分不做满分：留出可解释的差距
        score = score * P["scale"]

        cands.append({
            "code": code, "name": name,
            "price": r.get("price"), "pct": pct, "volumeRatio": vr,
            "turnover": turn, "amount": amt, "mcap": mcap,
            "mainNet": flow, "industry": r.get("industry"),
            # pe / pb 一直在这儿被丢掉，导致 VET 的 valuation 永远"PE — 不可判"。
            # 榜单接口本来就带 f9/f23，原样传下去，PE 才能变成一条真检查。
            "pe": r.get("pe"), "pb": r.get("pb"),
            "sources": r.get("sources", []),
            "score": round(score, 3),
            "reason": "涨幅%.1f%% 量比%.2f 换手%.1f%% 额%.1f亿 主力%.2f亿" % (
                pct or 0, vr or 0, turn or 0, (amt or 0) / 1e8, (flow or 0) / 1e8),
        })

    cands.sort(key=lambda x: -x["score"])
    top = cands[:st["settings"]["max_leads_per_cycle"]]

    # ---- 因子反哺打分（只改排序与证据，不改漏斗的否决权） ----
    # 口径：在基础分上做**有界微调**（±6%），每条调整都能说清楚理由；
    # 权重是经验值，没有样本外证据支撑，所以给上限、并写进 cand.factor 里可追溯。
    adj_n = 0
    for c in top:
        fxc, notes = None, []
        try:
            fxc = facts.compute(c["code"])
        except Exception:
            notes.append("因子不可判")
        adj, notes2 = _factor_adjust(fxc)
        notes += notes2
        if adj:
            adj_n += 1
            c["score"] = round(min(0.88, max(0.0, c["score"] * (1 + adj))), 3)
            c["reason"] += "｜因子" + ("%+.0f%%" % (adj * 100))
        c["factor"] = {
            "adj": round(adj, 3),
            "notes": notes,
            "rs_state": (fxc or {}).get("rs", {}).get("state"),
            "excess5": (fxc or {}).get("rs", {}).get("excess5"),
            "flow_pct": (fxc or {}).get("flow", {}).get("main_pct"),
            "flow_state": (fxc or {}).get("flow", {}).get("state"),
            "cross": (fxc or {}).get("cross", {}).get("count"),
        }
    top.sort(key=lambda x: -x["score"])

    st["stats"]["scanned"] += len(rows)
    set_station(st, "SCAN", "working",
                score=top[0]["score"] if top else 0,
                metrics={"universe": len(rows), "leads": len(top),
                         "threshold": 0.55},
                msg="扫描 %d 只 → %d 条线索" % (len(rows), len(top)))
    ev(st, "SCAN", "扫描 %d 只，产出 %d 条线索，最高分 %.2f（因子微调 %d 条，±6%% 上限）"
       % (len(rows), len(top), top[0]["score"] if top else 0, adj_n))
    return top


# ---------------------------------------------------------------- VET
def explain_verdict(checks, verdict, failed, unknown, GATES):
    """把 12 条 checks 排成一句人能听懂的话（规则版：不调用模型，可审计、可复现）。

    硬规矩：**数字原样取自 check["detail"]**，这里不许算新数字 ——
    解释层只做"排序 + 复述"，不做"再判一次"。
    措辞用短语表而不是 check 的 label 原文：label 里带阈值（"距 60 日高点 ≥ 88%（不追高）"），
    直接拼进气泡会又长又绕，读不出重点。

    三种语义必须分开，混在一起就是骗人：
      data = 数据拿不到（不是不合格，也别装作判过）
      gate = 真踩了硬门槛（这一单不能发）
      soft = 能发，只是某些项偏弱
    """
    SHORT = {"st": "ST/退市风险", "suspended": "停牌中", "liquidity": "成交额不够（太冷）",
             "age": "上市不足 60 个交易日", "position": "位置太高", "overheat": "已经过热",
             "cross_t": "因子自相矛盾", "plan": "这单发不出去", "early": "开盘不满 60 分钟",
             "valuation": "估值太贵", "volatility": "波动太大", "turnover": "换手不健康"}
    by = {x["key"]: x for x in checks}

    # 1) 先看"判不了"：关键证据缺失时不许说成"不合格"
    hard_missing = [k for k in ("cross_t", "plan") if by.get(k) and by[k].get("unknown")]
    if hard_missing:
        x = by[hard_missing[0]]
        return {"kind": "data", "key": x["key"],
                "head": "判不了：%s 取不到（数据缺失，不等于不合格）" % SHORT.get(x["key"], x["label"])}

    # 2) 硬门槛：按"改了它这单才可能成立"的顺序排，只讲最根本那条
    EXPLAIN_ORDER = ["st", "suspended", "cross_t", "plan", "liquidity",
                     "position", "overheat", "early", "age", "valuation"]
    gate_fail = [k for k in EXPLAIN_ORDER if k in failed and k in GATES]
    if gate_fail:
        k = gate_fail[0]
        x = by.get(k) or {}
        d = (x.get("detail") or "").strip()
        head = "不能发·%s%s" % (SHORT.get(k, x.get("label", k)),
                              ("（%s）" % d) if d else "")
        if len(gate_fail) > 1:
            head += " +%d 条" % (len(gate_fail) - 1)
        return {"kind": "gate", "key": k, "head": head, "gates": gate_fail}

    # 3) 只有软项偏弱：可以发，但说清弱在哪
    soft = [k for k in failed if k not in GATES]
    if soft:
        k = soft[0]
        x = by.get(k) or {}
        d = (x.get("detail") or "").strip()
        return {"kind": "soft", "key": k,
                "head": "能发·但%s%s" % (SHORT.get(k, x.get("label", k)),
                                       ("（%s）" % d) if d else "")}

    # 4) 通过
    judged = [x for x in checks if not x.get("unknown")]
    head = "能发：%d/%d 项通过" % (sum(1 for x in judged if x["ok"]), len(judged))
    if unknown:
        head += "（%d 项不可判）" % len(unknown)
    return {"kind": "clear", "key": None, "head": head}


def agent_vet(st, c):
    try:
        bars = df.get_kline(c["code"], "day", limit=90)["items"]
    except Exception as e:
        set_station(st, "VET", "alert", msg="K线获取失败")
        return {"verdict": "REJECT", "score": 0, "checks": [],
                "failed": ["no_bars"], "note": str(e)}

    checks = []

    def add(key, label, ok, detail, value=None, threshold=None):
        checks.append({"key": key, "label": label, "ok": bool(ok),
                       "detail": detail, "value": value, "threshold": threshold,
                       "unknown": value is None})

    add("st", "非 ST / 退市风险", "ST" not in c["name"].upper() and "退" not in c["name"],
        c["name"], value=c["name"], threshold="名称不含 ST/退")
    add("suspended", "正常交易（非停牌）", c["price"] not in (None, 0), "现价 %s" % c["price"],
        value=c["price"], threshold="price > 0")
    # 成交额 / 换手率是当日累计值：开盘头一小时拿收盘口径的门槛去判，等于把全市场判死刑。
    # 这段时间里它们进 unknown（不可判），另用一条明写的"开盘满 60 分钟"策略闸门来拦。
    sm = df.session_minutes()
    early = (sm is not None and 0 <= sm < 60)
    if early:
        add("liquidity", "成交额 ≥ 1.5 亿（收盘口径）", True,
            "开盘 %d 分钟，累计 %.2f 亿，尚未成熟" % (sm, (c["amount"] or 0) / 1e8),
            value=None, threshold=">= 1.5 亿（收盘口径）")
    else:
        add("liquidity", "成交额 ≥ 1.5 亿", (c["amount"] or 0) >= 1.5e8,
            "%.2f 亿" % ((c["amount"] or 0) / 1e8),
            value=round((c["amount"] or 0) / 1e8, 2), threshold=">= 1.5 亿")
    if early:
        add("turnover", "换手率 2%~25%（收盘口径）", True,
            "开盘 %d 分钟，累计 %.2f%%，按累计值无法判定" % (sm, (c["turnover"] or 0)),
            value=None, threshold="2% ~ 25%（收盘口径）")
    else:
        add("turnover", "换手率 2%~25%", 2 <= (c["turnover"] or 0) <= 25,
            "%.2f%%" % (c["turnover"] or 0),
            value=round(c["turnover"] or 0, 2), threshold="2% ~ 25%")
    if early:
        add("early", "开盘满 60 分钟（流动性指标成熟）", False,
            "开盘 %d 分钟：量能与换手还在累积，这一单先不确认" % sm,
            value=sm, threshold=">= 60 分钟")
    add("limit", "未接近涨停（买得进）", (c["pct"] or 0) <= limit_pct(c["code"]) - 2,
        "%.2f%% / 上限 %.0f%%" % (c["pct"] or 0, limit_pct(c["code"])),
        value=round(c["pct"] or 0, 2), threshold="<= %.0f%%" % (limit_pct(c["code"]) - 2))
    add("age", "上市 ≥ 60 个交易日", len(bars) >= 60, "%d 根日K" % len(bars),
        value=len(bars), threshold=">= 60 根")

    a = atr(bars, 14)
    atr_pct = (a / c["price"] * 100) if c["price"] else 0
    add("volatility", "ATR 1.5%~12%（可控）", 1.5 <= atr_pct <= 12, "ATR %.2f%%" % atr_pct,
        value=round(atr_pct, 2), threshold="1.5% ~ 12%")

    hi60 = max(b["h"] for b in bars[-60:])
    pos60 = c["price"] / hi60 * 100 if hi60 else 0
    add("position", "距 60 日高点 ≥ 88%（不追高）", pos60 >= 88, "%.1f%%" % pos60,
        value=round(pos60, 2), threshold=">= 88%")

    last3 = bars[-3:]
    run3 = (c["price"] / last3[0]["c"] - 1) * 100 if last3 and last3[0]["c"] else 100
    add("overheat", "近 3 日涨幅 < 18%（不过热）", run3 < 18, "%.1f%%" % run3,
        value=round(run3, 2), threshold="< 18%")

    pe = c.get("pe")
    if pe is None:
        # 线索可能是修复前存下的（payload 里没带 pe），当场补一次，别把"没传"当"没有"
        try:
            pe = df.get_quote(c["code"]).get("pe")
            if pe is not None:
                c["pe"] = pe
        except Exception:
            pass
    try:
        pe = float(pe)
    except (TypeError, ValueError):
        pe = None
    add("valuation", "PE 合理（0 < PE < 150）", pe is not None and 0 < pe < 150,
        "PE %s" % ("%.1f" % pe if pe is not None else "—（取不到）"),
        value=pe, threshold="0 < PE < 150（亏损为负 → 否决）")

    # 11) 因子交叉校验：冲突比缺失更危险（量价打架 / 价涨资金逃 / 价跌资金进）
    fxc = None
    try:
        fxc = facts.compute(c["code"])
        cross = fxc.get("cross") or {}
        n = cross.get("count") or 0
        add("cross_t", "因子交叉校验（量价 / 资金 / 强弱不打架）", n == 0,
            "、".join(cross.get("items") or []) if n else "无冲突",
            value=n, threshold="0 项冲突")
    except Exception as e:
        add("cross_t", "因子交叉校验（量价 / 资金 / 强弱不打架）", False,
            "因子不可判：%s" % e, value=None, threshold="0 项冲突")

    # 12) 仓位可执行：plan.py 的 blockers 是"这单发不发得出去"，不是建议
    try:
        import plan as _plan
        pl = _plan.compute(c["code"], st.get("equity", INIT_EQUITY), st.get("cash", 0))
        blockers = pl.get("blockers") or []
        shares = pl.get("shares") or 0
        ok = (not blockers) and shares >= 100
        if ok:
            detail = "%d 股 / 成本 %.0f / 回本 +%.2f%%" % (
                shares, pl.get("cost_total") or 0, pl.get("breakeven_pct") or 0)
        else:
            detail = "；".join(blockers) if blockers else "不足 1 手（预算不够）"
        add("plan", "仓位可执行（三约束取最小 + 可成交）", ok, detail,
            value=shares, threshold=">= 100 股且无 blockers")
    except Exception as e:
        add("plan", "仓位可执行（三约束取最小 + 可成交）", False,
            "计划算不出来：%s" % e, value=None, threshold=">= 100 股且无 blockers")

    judged = [x for x in checks if not x.get("unknown")]
    passed = sum(1 for x in judged if x["ok"])
    score = passed / len(judged) if judged else 0
    # 影子分：同样的底分，但把风险折进去。只落审计做对照，不参与这里的任何判断
    sh = shadow_score(c.get("score"), fxc, atr_pct, pos60, pe)
    failed = [x["key"] for x in judged if not x["ok"]]
    unknown = [x["key"] for x in checks if x.get("unknown")]
    # 硬门槛：这些不是"扣分项"，是"这票是不是本策略要的东西"
    # （买不进 / 不是强势突破 / 已经过热 / 因子自相矛盾 / 这单根本发不出去 —— 任一条成立就直接否决）
    # early 也在里面：开盘头一小时量能还没成熟，这一单先不确认（不是数据有问题，是时机不成熟）
    GATES = ("st", "suspended", "liquidity", "age", "position", "overheat",
             "cross_t", "plan", "early")
    gate_fail = [x for x in failed if x in GATES]
    # 两条关键证据缺失（拿不到因子 / 算不出仓位）时不许"因缺失而通过"
    missing_hard = [x["key"] for x in checks
                    if x["key"] in ("cross_t", "plan") and x.get("unknown")]
    verdict = "CLEAR" if (not gate_fail and not missing_hard and score >= 0.75) else "REJECT"

    msg = "通过 %d/%d 项" % (passed, len(judged))
    if unknown:
        msg += "（%s 不可判）" % ",".join(unknown)
    set_station(st, "VET", "working" if verdict == "CLEAR" else "alert",
                score=score,
                metrics={"passed": "%d/%d" % (passed, len(judged)),
                         "atr": "%.2f%%" % atr_pct, "pos60": "%.1f%%" % pos60,
                         "unknown": ",".join(unknown) or "—"},
                msg=msg)

    why = explain_verdict(checks, verdict, failed, unknown, GATES)
    # 解释挂在 VET 工位上：前端气泡只读这一句（whyKind=gate 时才抢台词，其余让位给资金流/冲突）
    vst = st["stations"]["VET"]
    vst["why"] = why["head"]
    vst["whyKind"] = why["kind"]
    vst["whyKey"] = why.get("key")
    vst["whyAt"] = time.time()

    audit({"agent": "VET", "code": c["code"], "verdict": verdict,
           "score": round(score, 3), "failed": failed, "unknown": unknown,
           "explain": why,
           # name/price 是给"结果回填"用的：事后复盘要知道当时这票叫什么、什么价，
           # 否则只能靠 code 反查（停牌改名就对不上了）。
           "name": c.get("name"), "price": c.get("price"),
           # 决策那一刻的因子快照：以后要离线拟合"影子模型"、或回放"如果把权重改成X会怎样"，
           # 必须先有这一刻的输入值。只存 checks 不够（checks 是结论，不是特征）。
           "factor": c.get("factor") or {},
           # 影子分：同一时刻、同一底分，换成"风险折价"口径会得多少分。
           # 只回答问题"如果当初换个算法会怎样"，不改变任何决策；标签够了再拿它做对照。
           "shadow": sh,
           "industry": c.get("industry"), "pe": c.get("pe"), "pb": c.get("pb"),
           "checks": checks})
    if verdict == "REJECT":
        ev(st, "VET", "%s %s 否决：%s" % (c["code"], c["name"],
                                          ", ".join(failed) or "综合分不足"), "warn")
    else:
        st["stats"]["cleared"] += 1
        ev(st, "VET", "%s %s 通过审查（%.2f）%s" % (c["code"], c["name"], score,
                                               "，%s 不可判" % ",".join(unknown) if unknown else ""), "ok")
    return {"verdict": verdict, "score": score, "checks": checks, "failed": failed,
            "unknown": unknown, "atr": a, "bars": bars, "shadow": sh, "explain": why}


# ---------------------------------------------------------------- SOCIAL
def agent_social(st, c):
    flow = c.get("mainNet")
    flow_ratio = (flow / c["amount"]) if (flow is not None and c["amount"]) else None
    s_flow = band(max(flow_ratio, 0.0), 0.0, 0.30, 0.02, 0.12) if flow_ratio is not None else 0.0
    s_vr = band(c["volumeRatio"], 1.0, 12.0, 2.0, 7.0)
    s_turn = band(c["turnover"], 1.0, 30.0, 3.0, 15.0)
    score = 0.5 * s_flow + 0.3 * s_vr + 0.2 * s_turn
    # 硬门槛：资金数据缺失或净流出时，情绪再热也不确认（一个确认不算两个）
    verdict = "CONFIRM" if (score >= 0.5 and flow_ratio is not None and flow_ratio > 0) else "PASS"

    flow_txt = "%.2f亿" % (flow / 1e8) if flow is not None else "—"
    ratio_txt = "%.1f%%" % (flow_ratio * 100) if flow_ratio is not None else "—"
    set_station(st, "SOCIAL", "working",
                score=min(score, 0.99),
                metrics={"mainNet": flow_txt, "flowRatio": ratio_txt,
                         "vr": "%.2f" % (c["volumeRatio"] or 0)},
                msg="主力净流入 %s（占成交 %s）" % (flow_txt, ratio_txt))
    if verdict == "CONFIRM":
        st["stats"]["confirmed"] += 1
        ev(st, "SOCIAL", "%s 资金面确认（%.2f），主力净流入 %s"
           % (c["code"], score, flow_txt), "ok")
    else:
        ev(st, "SOCIAL", "%s 资金面未确认（%.2f）" % (c["code"], score))
    return {"verdict": verdict, "score": score, "flowRatio": flow_ratio,
            "evidence": {
                "inputs": {"mainNet": flow, "amount": c["amount"],
                           "flowRatio": flow_ratio, "volumeRatio": c["volumeRatio"],
                           "turnover": c["turnover"]},
                "thresholds": {"score": ">= 0.50", "flowRatio": "> 0（净流入）"},
                "parts": {"s_flow": round(s_flow, 3), "s_vr": round(s_vr, 3),
                          "s_turn": round(s_turn, 3)},
            }}


# ---------------------------------------------------------------- SIZE
def agent_size(st, c, vet):
    bars = vet["bars"]
    atr_v = vet.get("atr") or (c["price"] * 0.03)
    entry = round(c["price"] * 1.001, 2)                     # 含 0.1% 滑点
    stop = max(round(entry - 1.5 * atr_v, 2), round(entry * 0.92, 2))
    target = round(entry + 3.0 * atr_v, 2)
    risk_per_share = entry - stop
    if risk_per_share <= 0:
        return None

    equity = st["equity"]
    risk_amt = equity * RISK_PER_TRADE
    shares = int(risk_amt / risk_per_share // 100) * 100
    cap_shares = int(equity * MAX_POS_PCT / entry // 100) * 100
    cash_shares = int(st["cash"] / (entry * 1.005) // 100) * 100
    shares = min(shares, cap_shares, cash_shares)
    if shares < 100:
        set_station(st, "SIZE", "alert", msg="资金不足或波动过大，放弃")
        return None

    cost = shares * entry
    rr = round((target - entry) / risk_per_share, 2)
    set_station(st, "SIZE", "working",
                metrics={"shares": shares, "cost": "%.0f" % cost,
                         "stop": stop, "target": target, "rr": rr},
                msg="%d 股 · 成本 %.0f 元 · 止损 %.2f · 目标 %.2f (%.1fR)"
                    % (shares, cost, stop, target, rr))
    ev(st, "SIZE", "%s 计划买入 %d 股，止损 %.2f，目标 %.2f，盈亏比 %.1fR"
       % (c["code"], shares, stop, target, rr))
    return {"entry": entry, "stop": stop, "target": target,
            "shares": int(shares), "cost": cost, "rr": rr}


# ---------------------------------------------------------------- FILLS
def agent_fills(st, lead):
    """执行层。前置守卫（approve）挡不住的情况，这里再断言一遍 ——
    执行层是最后一道门，不能假设调用方已经检查过。"""
    plan = lead["plan"]
    c = lead["cand"]
    code = c["code"]

    def refuse(why, key):
        lead["state"] = "FAILED"
        lead["note"] = "执行层拒绝：%s" % why
        audit({"agent": "FILLS", "action": "FILL_REFUSED", "lead": lead["id"],
               "code": code, "why": key})
        ev(st, "FILLS", "%s 成交被拒绝：%s" % (code, why), "warn")
        return False

    # 1) 同码只允许一笔持仓（重复批准 / 同码双单的最后一道防线）
    if any(p["code"] == code for p in st["positions"]):
        return refuse("%s 已在持仓中，不允许二次买入" % code, "already_holding")
    # 2) 同一张线索不能成交两次
    if any(t.get("id") == lead["id"] for t in st["trades"]):
        return refuse("线索 %s 已经成交过" % lead["id"], "lead_replayed")
    # 3) 整手与最小手数
    shares = plan.get("shares") or 0
    if shares < 100 or shares % 100:
        return refuse("股数 %s 不是 ≥1 手的整手" % shares, "lot")
    # 4) 涨停附近买不进（原逻辑保留）
    if (c["pct"] or 0) >= limit_pct(code) - 1:
        ev(st, "FILLS", "%s 接近涨停，拒绝成交" % code, "warn")
        return refuse("%s 接近涨停" % code, "limit_up")
    # 4b) 价格复核：执行层不假设调用方已经查过现价（approve 会查，这里再查一次）
    g = price_guard(lead)
    if not g["ok"]:
        return refuse(g["why"], "price_drift")

    amount = shares * plan["entry"]
    comm = max(MIN_COMM, round(amount * COMMISSION, 2))
    trans = round(amount * TRANSFER, 2)
    total = amount + comm + trans
    # 5) 现金
    if total > st["cash"]:
        return refuse("现金不足（需 %.2f / 有 %.2f）" % (total, st["cash"]), "cash")
    # 6) 风险预算与单票上限（计划可能已过期，权益变了）
    risk = shares * (plan["entry"] - plan["stop"])
    if risk > st["equity"] * RISK_PER_TRADE * 1.3:
        return refuse("风险 %.2f 超预算 %.2f 的 1.3 倍（计划可能过期）"
                      % (risk, st["equity"] * RISK_PER_TRADE), "risk")
    if amount > st["equity"] * MAX_POS_PCT * 1.2:
        return refuse("单票仓位 %.2f 超 %.0f%% 上限" % (amount, MAX_POS_PCT * 100), "position_cap")

    st["cash"] = round(st["cash"] - total, 2)
    pos = {
        "code": c["code"], "name": c["name"], "shares": plan["shares"],
        "entry": plan["entry"], "cost": round(total, 2),
        "stop": plan["stop"], "target": plan["target"],
        "buy_date": _today(), "buy_time": datetime.now().strftime("%H:%M:%S"),
        "lead": lead["id"], "industry": c.get("industry"),
        "last": c["price"],
    }
    st["positions"].append(pos)
    st["trades"].append({
        "id": lead["id"], "code": c["code"], "name": c["name"], "side": "BUY",
        "shares": plan["shares"], "price": plan["entry"],
        "amount": round(amount, 2), "fee": round(comm + trans, 2),
        "pnl": None, "reason": "ENTRY", "t": datetime.now().strftime("%m-%d %H:%M:%S"),
    })
    st["stats"]["filled"] += 1
    lead["state"] = "FILLED"
    set_station(st, "FILLS", "working",
                metrics={"last": "%s %d股" % (c["code"], plan["shares"]),
                         "amount": "%.0f" % amount},
                msg="成交 %d 股 @ %.2f" % (plan["shares"], plan["entry"]))
    ev(st, "FILLS", "成交 %s %s %d 股 @ %.2f，手续费 %.2f"
       % (c["code"], c["name"], plan["shares"], plan["entry"], comm + trans), "ok")
    audit({"agent": "FILLS", "lead": lead["id"], "side": "BUY",
           "code": c["code"], "shares": plan["shares"], "price": plan["entry"]})
    return True


# ---------------------------------------------------------------- RISK
def agent_risk(st):
    exits = []
    for pos in list(st["positions"]):
        try:
            q = df.get_quote(pos["code"])
            price = q["price"]
        except Exception:
            continue
        pos["last"] = price
        if not price:
            continue

        today = _today()
        hold = (datetime.strptime(today, "%Y-%m-%d")
                - datetime.strptime(pos["buy_date"], "%Y-%m-%d")).days
        reason = None
        if hold < 1:
            continue                                   # T+1：当日买入不可卖
        if price <= pos["stop"]:
            reason = "STOP_LOSS"
        elif price >= pos["target"]:
            reason = "TAKE_PROFIT"
        elif hold >= 8:
            reason = "TIME_EXIT"
        if not reason:
            continue

        amount = pos["shares"] * price
        fee = fee_sell(amount)
        proceeds = round(amount - fee, 2)
        pnl = round(proceeds - pos["cost"], 2)
        st["cash"] = round(st["cash"] + proceeds, 2)
        st["positions"].remove(pos)
        st["stats"]["closed"] += 1
        st["stats"]["wins" if pnl > 0 else "losses"] += 1
        st["trades"].append({
            "id": pos["lead"], "code": pos["code"], "name": pos["name"], "side": "SELL",
            "shares": pos["shares"], "price": price, "amount": round(amount, 2),
            "fee": fee, "pnl": pnl, "reason": reason,
            "t": datetime.now().strftime("%m-%d %H:%M:%S"),
        })
        exits.append((pos, pnl, reason))
        ev(st, "RISK", "平仓 %s %s @ %.2f（%s），盈亏 %+.0f"
           % (pos["code"], pos["name"], price, reason, pnl),
           "ok" if pnl > 0 else "warn")
        audit({"agent": "RISK", "code": pos["code"], "reason": reason,
               "price": price, "pnl": pnl})

    w, l = st["stats"]["wins"], st["stats"]["losses"]
    set_station(st, "RISK", "alert" if exits else "idle",
                metrics={"open": len(st["positions"]), "record": "%dW %dL" % (w, l)},
                msg="盯 %d 个持仓 · 战绩 %dW %dL" % (len(st["positions"]), w, l))
    return exits


# ---------------------------------------------------------------- 结算 / 主管
def mark_to_market(st):
    mv = 0.0
    for pos in st["positions"]:
        mv += pos["shares"] * (pos.get("last") or pos["entry"])
    st["equity"] = round(st["cash"] + mv, 2)
    return st["equity"]


# ---------------------------------------------------------------- 账本恒等式
def ledger_check(st, fix_equity=True, enforce=True):
    """恒等式： 现金 + 持仓市值 = 初始权益 + 已实现盈亏 + 未实现盈亏

    这条等式一旦不平，说明某个记账口径被悄悄改了（手续费漏记、
    持仓成本基算错、平仓没扣费）。这种错误不会抛异常，只会让权益
    慢慢漂移，所以必须每轮显式验一次。
    """
    mv = sum(p["shares"] * (p.get("last") or p["entry"]) for p in st["positions"])
    realized = sum((t.get("pnl") or 0) for t in st["trades"] if t.get("side") == "SELL")
    unreal = sum(p["shares"] * (p.get("last") or p["entry"]) - p["cost"]
                 for p in st["positions"])
    equity = round(st["cash"] + mv, 2)
    expect = round(st["init"] + realized + unreal, 2)
    diff = round(equity - expect, 2)

    # 第二道：成交明细可重放（买 - 卖 = 当前持仓）
    bought, sold = {}, {}
    for t in st["trades"]:
        d = bought if t.get("side") == "BUY" else sold
        d[t["code"]] = d.get(t["code"], 0) + (t.get("shares") or 0)
    open_sh = {p["code"]: p["shares"] for p in st["positions"]}
    replay_bad = []
    for code in set(bought) | set(sold) | set(open_sh):
        gap = bought.get(code, 0) - sold.get(code, 0) - open_sh.get(code, 0)
        if gap != 0:
            replay_bad.append({"code": code, "bought": bought.get(code, 0),
                               "sold": sold.get(code, 0),
                               "open": open_sh.get(code, 0), "gap": gap})

    ok = abs(diff) <= 0.05 and not replay_bad
    out = {"ok": ok, "equity": equity, "expect": expect, "diff": diff,
           "cash": round(st["cash"], 2), "mv": round(mv, 2),
           "realized": round(realized, 2), "unrealized": round(unreal, 2),
           "replay_bad": replay_bad,
           "at": datetime.now().strftime("%H:%M:%S")}
    if fix_equity:
        st["equity"] = equity
    st["ledger"] = out
    if not ok and enforce:
        st["halt"] = True
        reason = ("账本不平 %.2f" % diff) if abs(diff) > 0.05 else \
                 ("成交明细对不上持仓：%s" % (out["replay_bad"][0]["code"],))
        set_station(st, "CHIEF", "alert", msg="%s，已自动停摆" % reason)
        ev(st, "CHIEF", "账本自检失败：%s（权益 %.2f / 应计 %.2f / 差 %.2f），全场停摆"
           % (reason, equity, expect, diff), "alert")
        audit({"agent": "LEDGER", "action": "MISMATCH", "equity": equity,
               "expect": expect, "diff": diff, "replay_bad": replay_bad})
    return out


# ---------------------------------------------------------------- 新鲜度
def freshness(st):
    """数据是不是今天的。周日跑一轮，它必须承认这是周五收盘。"""
    asof = df.market_asof()
    now = datetime.now()
    hhmm = now.strftime("%H:%M")
    in_session = now.weekday() < 5 and (
        ("09:30" <= hhmm <= "11:30") or ("13:00" <= hhmm <= "15:00"))
    today = _today()
    stale = (asof is not None and asof != today)
    unknown = asof is None                      # 取不到基准 = 不知道今天有没有数据
    blind = bool(unknown or (in_session and stale))
    out = {"asof": asof, "today": today, "inSession": in_session,
           "stale": bool(stale), "unknown": unknown, "blind": blind,
           "channels": df.health()}
    st["freshness"] = out
    return out


def agent_chief(st, summary=""):
    eq = mark_to_market(st)
    day_pnl = round(eq - st["day_start_equity"], 2)
    day_ret = day_pnl / st["day_start_equity"] * 100 if st["day_start_equity"] else 0
    if day_pnl <= -st["day_start_equity"] * DAILY_LOSS_HALT:
        st["halt"] = True
        ev(st, "CHIEF", "日亏 %.2f%% 触及 %.0f%% 熔断，全场停摆"
           % (day_ret, DAILY_LOSS_HALT * 100), "alert")
    set_station(st, "CHIEF", "alert" if st["halt"] else "working",
                metrics={"equity": eq, "dayPnl": day_pnl, "dayRet": "%.2f%%" % day_ret,
                         "open": len(st["positions"])},
                msg=summary or "权益 %.0f · 今日 %+.0f (%+.2f%%)" % (eq, day_pnl, day_ret))
    return {"equity": eq, "dayPnl": day_pnl, "dayRet": day_ret}


# ---------------------------------------------------------------- 同码去重
OPEN_STATES = ("SCANNED", "VETTED", "CONFIRMED", "STAGED")
_DEDUPE_KEEP = OPEN_STATES                # 同一只票在途的线索只留最新一条


def dedupe_leads(st):
    """同一只票只允许一条在途线索。
    跨天重复是真的会发生：昨天的 STAGED 没处理，今天又扫出同一只票 → 两条 STAGED，
    人点两下就是两次买入。这里把旧的标成 SUPERSEDED（留痕，不删），只留最新一条。
    返回被取代的条数。"""
    groups = {}
    for l in st["leads"]:
        if l.get("state") in _DEDUPE_KEEP:
            groups.setdefault(l["code"], []).append(l)
    n = 0
    for code, ls in groups.items():
        if len(ls) < 2:
            continue
        ls.sort(key=lambda x: (x.get("d") or "", x.get("t") or ""), reverse=True)
        keep = ls[0]
        for old in ls[1:]:
            old["state"] = "SUPERSEDED"
            old["note"] = "同码去重：被 %s 取代（保留最新，旧单不再可批）" % keep["id"]
            n += 1
            audit({"agent": "CHIEF", "action": "SUPERSEDE", "lead": old["id"],
                   "by": keep["id"], "code": code})
    if n:
        ev(st, "CHIEF", "同码去重：%d 条旧线索标为 SUPERSEDED（同票只留最新一张单）" % n, "warn")
    return n


# ---------------------------------------------------------------- 编排
def run_cycle(st, reason="auto"):
    with LOCK:
        st["cycle"] += 1
        st["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if st["halt"]:
            ev(st, "CHIEF", "熔断中，本轮跳过", "alert")
            save_state(st)
            return st

        rows = agent_crawler(st)
        fresh = freshness(st)
        ch = fresh["channels"] or {}
        live = sum(1 for v in ch.values() if v.get("last_ok"))
        st["stations"]["CRAWLER"]["metrics"].update(
            {"asof": fresh["asof"] or "—", "channels": "%d/%d" % (live, len(ch) or 1)})
        if fresh["unknown"]:
            set_station(st, "CRAWLER", "alert", msg="数据基准不可用：不知道这批数据是哪天的")
            ev(st, "CRAWLER", "行情基准取不到（指数接口无响应），无法确认数据日期："
               "本轮按失明处理，不新开仓", "alert")
            audit({"agent": "CRAWLER", "action": "ASOF_UNKNOWN"})
        elif fresh["stale"]:
            ev(st, "CRAWLER", "数据停在 %s（今日 %s）%s"
               % (fresh["asof"], fresh["today"],
                  "，盘中失明，本轮不新开仓" if fresh["blind"] else "，非交易时段属正常"),
               "alert" if fresh["blind"] else "info")
        if fresh["blind"] and not fresh["unknown"]:
            set_station(st, "CRAWLER", "alert",
                        msg="数据停在 %s，今日盘中数据缺失" % fresh["asof"])
            audit({"agent": "CRAWLER", "action": "STALE_DATA",
                   "asof": fresh["asof"], "today": fresh["today"]})
        cands = agent_scan(st, rows)
        dedupe_leads(st)                 # 先收拾历史重复，再决定新单
        if fresh["blind"]:
            cands = []                      # 失明时不产出任何新仓位
        opened = 0
        # 去重挡掉多少：这不是性能指标，而是**样本产出的瓶颈** ——
        # 一只票当天判过一次就不会再进 VET，所以"每天能长出多少条判断"是被业务规则限死的。
        # 把它写成一条审计，样本积累速度就不用靠猜。
        skipped = {"held": 0, "same_day": 0, "open": 0, "bought": 0}
        vetted = 0

        for c in cands:
            held = {p["code"] for p in st["positions"]}
            same_day = {l["code"] for l in st["leads"]
                        if (l.get("d") or "") == _today()}
            open_code = {l["code"]: l for l in st["leads"]
                         if l.get("state") in OPEN_STATES}
            bought_today = {t["code"] for t in st["trades"]
                            if t["side"] == "BUY" and t["t"].startswith(datetime.now().strftime("%m-%d"))}
            if c["code"] in held:
                skipped["held"] += 1
                continue
            if c["code"] in bought_today:
                skipped["bought"] += 1
                continue
            if c["code"] in same_day:
                skipped["same_day"] += 1
                continue
            if c["code"] in open_code:
                # 跨天也不能重复挂单：昨天没批的那张单还算在途
                skipped["open"] += 1
                ev(st, "CHIEF", "%s %s 已有在途线索 %s，本轮不重复挂单"
                   % (c["code"], c["name"], open_code[c["code"]]["id"]), "info")
                continue
            if len(st["positions"]) >= 5:
                ev(st, "CHIEF", "持仓已达 5 只上限，停止开新仓", "warn")
                break
            if opened >= st["settings"].get("max_new_per_cycle", 2):
                break

            lead_id = "#%03d-%s" % (st["cycle"], c["code"])
            lead = {"id": lead_id, "code": c["code"], "name": c["name"],
                    "score": c["score"], "state": "SCANNED", "cand": c,
                    "d": _today(), "t": datetime.now().strftime("%H:%M:%S"), "note": ""}

            vet = agent_vet(st, c)
            vetted += 1
            lead["vet"] = {"verdict": vet["verdict"], "score": vet["score"],
                           "failed": vet["failed"], "evidence": vet["checks"],
                           "shadow": vet.get("shadow"), "explain": vet.get("explain")}
            lead["asof"] = fresh["asof"]
            if vet["verdict"] != "CLEAR":
                lead["state"] = "REJECTED"
                lead["note"] = "审查未通过：" + ", ".join(vet["failed"])
                st["leads"].insert(0, lead)
                continue
            lead["state"] = "VETTED"

            soc = agent_social(st, c)
            _ev = soc.get("evidence") or {}
            _in = _ev.get("inputs") or {}
            # 把资金流原始数存进线索里，前端"资金流网络"才有真实依据（不是画个装饰）
            lead["social"] = {"verdict": soc["verdict"], "score": soc["score"],
                              "flowRatio": soc.get("flowRatio"),
                              "mainNet": _in.get("mainNet"),
                              "amount": _in.get("amount")}
            if soc["verdict"] != "CONFIRM":
                lead["state"] = "PASSED"
                lead["note"] = "资金面未确认（一个确认不算两个）"
                st["leads"].insert(0, lead)
                continue
            lead["state"] = "CONFIRMED"

            plan = agent_size(st, c, vet)
            if not plan:
                lead["state"] = "DROP"
                lead["note"] = "仓位计算为 0（资金或波动不满足）"
                st["leads"].insert(0, lead)
                continue
            lead["plan"] = plan
            lead["state"] = "STAGED"
            lead["note"] = "待人工批准（%s）" % datetime.now().strftime("%H:%M:%S")
            st["leads"].insert(0, lead)
            ev(st, "CHIEF", "%s %s 通过双确认，挂单待人工批准 %s"
               % (c["code"], c["name"], plan["shares"]), "ok")
            audit({"agent": "CHIEF", "action": "STAGED", "lead": lead_id,
                   "code": c["code"], "shares": plan["shares"], "entry": plan["entry"]})

            if st["settings"]["auto_approve"]:
                ev(st, "CHIEF", "%s 纸面模式自动批准（实盘需人工 APPROVE）" % lead_id)
                audit({"agent": "CHIEF", "action": "AUTO_APPROVE", "lead": lead_id,
                       "mode": "paper"})
                lead["state"] = "APPROVED"
                if agent_fills(st, lead):
                    opened += 1

        agent_risk(st)
        agent_chief(st)

        # 一轮结束，工位回落到待机，避免前端一直亮
        for sid, *_ in STATIONS:
            if sid in ("CRAWLER", "RISK", "CHIEF"):
                continue
            if st["stations"][sid]["state"] == "working":
                st["stations"][sid]["state"] = "idle"

        del st["leads"][60:]
        ledger_check(st)                 # 每轮结束验账，不平即停摆
        # 样本产出账单：本轮有几个候选、几个真进了 VET、几个被去重挡掉（分原因）
        # 自进化要的独立样本是"每天新判几只票"，不看这条就只能靠感觉估进度
        audit({"agent": "CHIEF", "action": "CYCLE_SUMMARY", "cycle": st["cycle"],
               "cands": len(cands), "vetted": vetted, "skipped": skipped,
               "leads": len(st["leads"]), "positions": len(st["positions"])})
        save_state(st)
        return st


def reset():
    with LOCK:
        st = default_state()
        save_state(st)
        return st


PRICE_DRIFT_MAX = 0.010      # 批准前允许的价格偏离（1%）。超过就不许拿旧价成交。


def price_guard(lead):
    """批准前按**现价**复核一次入场价。

    为什么必须有这一步：线索里的 plan.entry 是生成那一刻的快照价，STAGED 之后可能
    放了几小时、甚至过夜。`agent_fills` 又直接拿 plan["entry"] 记账，结果就是
    "按旧价买入"—— 股数、止损距离、风险预算全都不准，而且账本上看不出来。
    所以这里宁可不成交：偏离超阈值就拒绝，让人重新跑一轮出新单。

    口径：偏离 = 现价 / 快照入场价 - 1，双向都拦（比市场便宜同样买不到）。
    取不到现价也拒 —— 看不见价格就不许下单，这是 fail-closed 的同一套逻辑。
    """
    plan = lead.get("plan") or {}
    entry = plan.get("entry")
    code = lead.get("code") or (lead.get("cand") or {}).get("code")
    out = {"code": code, "entry": entry, "live": None, "drift_pct": None,
           "ok": False, "why": None}
    if not entry:
        out["why"] = "这单没有入场价，不能批准"
        return out
    try:
        q = df.get_quote(code)
    except Exception as e:
        out["why"] = "取不到现价（%s），不能批准 —— 看不见价就不下单" % str(e)[:60]
        return out
    live = q.get("price")
    if not isinstance(live, (int, float)) or live <= 0:
        out["why"] = "取不到可用现价（%s），不能批准" % code
        return out
    out["live"] = live
    drift = (live / float(entry) - 1.0) * 100.0
    out["drift_pct"] = round(drift, 2)
    if abs(drift) > PRICE_DRIFT_MAX * 100:
        out["why"] = ("现价 %.2f 与这单入场价 %.2f 偏离 %+.2f%%（上限 %.1f%%）："
                      "拿旧价成交会让股数与风险预算都不准，请重新跑一轮再判"
                      % (live, entry, drift, PRICE_DRIFT_MAX * 100))
        return out
    out["ok"] = True
    return out


# ---------------------------------------------------------------- 读数回喂
# 为什么要这一层：蜘蛛的触角读的是"页面上的字"，而机器判的是"接口返回的数"。
# 两条路径同源，但中间隔着渲染、缓存、格式化。任何一处出错，人看到的就不是机器判的 ——
# 而人正是看着页面按"批准"的。所以读数要回喂：页面读回来的值必须和重算的接口值对得上，
# 对不上就不许批这一单。这不是装饰，它拦的是"照着旧数字下单"。
RD_DECLARE = 300            # 页面读数超过这么久没上报，就不算"校验过"（只标未校验，不拦）
RD_REL_SLOW = 0.015         # 日线口径的慢变量：盘中不会变，只容格式化误差
RD_REL_LIVE = 0.25          # 盘中实时量（成交额/资金流）：页面渲染与重算之间本来就隔着几分钟

# 页面 label → (接口字段路径, 数值类型, 稳定度)
# 只有 slow 字段不符才算"页面错了"，live 字段不符只记成"盘中漂移"——
# 拿重算的当前值去比几分钟前渲染的页面，一定会有差，那不是 bug，是时间差。
RD_FIELDS = {
    "MA20 / MA60": ("structure.ma20", "num", "slow"),
    "均线斜率 20日": ("structure.slope20", "pct", "slow"),
    "均线斜率 60日": ("structure.slope60", "pct", "slow"),
    "ATR / 占价": ("structure.atr", "num", "slow"),
    "跳空 / 连板": ("structure.gaps", "num", "slow"),
    "日K 根数": ("tradable.bars", "num", "slow"),
    "近3日涨幅": ("structure.run3", "pct", "live"),
    "成交额": ("volume.amount", "money", "live"),
    "量比 / 换手": ("volume.volumeRatio", "num", "live"),
    "主力净流入": ("flow.main", "money", "live"),
    "占成交额": ("flow.main_pct", "pct", "live"),
    "今日超额": ("rs.excess", "pct", "live"),
}


def _dig(obj, path):
    for k in path.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(k)
    return obj


def _page_num(txt):
    """把页面上的文字换成数：'3.94 亿' / '6472 万' / '4790 万 / 4320 万' / '0 次 / 0 板'。

    两数拼一行（'x / y'）时只取第一段；抓第一串数字而不是整串匹配，
    因为页面会带单位后缀（'次'、'板'）。换不出来返回 None —— 不能让"读不出来"变成 0。
    """
    if not isinstance(txt, str):
        return None
    s = txt.replace(",", "").strip()
    if not s or s in ("—", "-", "–"):
        return None
    s = s.split("/")[0].strip()
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    v = float(m.group(0))
    tail = s[m.end():m.end() + 2]
    if "亿" in tail:
        v *= 1e8
    elif "万" in tail:
        v *= 1e4
    return v


def report_readback(code, items):
    """前端把蜘蛛这一轮读到的页面行传上来，这里当场比对并写盘。

    比对基准是**重新算一遍**的 factors，不是前端带上来的数 —— 前端自报等于没校验。
    """
    with LOCK:
        st = load_state()
        code = (code or "").strip()
        rows = []
        for it in (items or [])[:60]:
            k = (it.get("k") or "").strip() if isinstance(it, dict) else ""
            v = (it.get("v") or "").strip() if isinstance(it, dict) else ""
            if k and v:
                rows.append({"k": k, "v": v})

        checked, bad, live_diff, skipped = [], [], [], []
        api_doc = None
        try:
            api_doc = facts.compute(code) if code else None
        except Exception as e:
            api_doc = None
            skipped.append({"k": "*", "why": "重算失败：%s" % e})

        if api_doc:
            for r in rows:
                spec = RD_FIELDS.get(r["k"])
                if not spec:
                    continue                                   # 不在校验清单里的行，只记录不比
                path, kind, grade = spec
                want = _dig(api_doc, path)
                got = _page_num(r["v"])
                if want is None:
                    skipped.append({"k": r["k"], "why": "接口这项没有值"})
                    continue
                if got is None:
                    skipped.append({"k": r["k"], "why": "页面这行不是数（%s）" % r["v"][:12]})
                    continue
                want = float(want)
                rel = RD_REL_SLOW if grade == "slow" else RD_REL_LIVE
                tol = max(abs(want) * rel, 0.005 if kind == "pct" else 0.0,
                          1e4 if kind == "money" else 0.0)
                ok = abs(got - want) <= tol
                item = {"k": r["k"], "page": r["v"], "api": round(want, 2),
                        "grade": grade, "ok": ok}
                checked.append(item)
                if not ok:
                    if grade == "slow":
                        bad.append(item)                   # 慢变量不符=页面真的错了
                    else:
                        live_diff.append(item)             # 快变量不符=盘中时间差，只记录

        out = {"code": code, "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "ts": time.time(), "n": len(checked), "bad": bad,
               "live_diff": live_diff, "skipped": skipped,
               "ok": bool(checked) and not bad}
        st["readback"] = out
        save_state(st)
        audit({"agent": "LEDGER", "action": "READBACK", "code": code,
               "n": len(checked), "bad": len(bad), "live_diff": len(live_diff),
               "items": bad[:5]})
        if bad:
            ev(st, "VET", "%s 页面读数与接口对不上：%s（已禁止批准）"
               % (code, "、".join("%s 页面 %s / 接口 %s" % (b["k"], b["page"], b["api"])
                                 for b in bad[:2])), "alert")
            save_state(st)
        return out


def readback_guard(code):
    """批准前的读数校验门。三种结论，措辞要能分清：
      ok      —— 页面读回来的值和重算的接口值一致
      drift   —— 对不上，这单不许批（人看到的不是机器判的）
      unknown —— 这票还没被读过，或者读数过期了：不拦，但要标出来
    """
    st = load_state()
    rb = st.get("readback") or {}
    if not rb or rb.get("code") != code:
        return {"state": "unknown", "why": "这票还没读过（蜘蛛没在这只票上取过数）"}
    age = time.time() - (rb.get("ts") or 0)
    if age > RD_DECLARE:
        return {"state": "unknown", "why": "读数已过期 %.0f 秒（超过 %.0f 秒）" % (age, RD_DECLARE)}
    if rb.get("bad"):
        b = rb["bad"][0]
        return {"state": "drift", "age": round(age),
                "why": "页面读数与接口不一致：%s 页面 %s / 接口 %s（%d 项不符）"
                       % (b["k"], b["page"], b["api"], len(rb["bad"])),
                "items": rb["bad"][:5]}
    if not rb.get("n"):
        return {"state": "unknown", "why": "读到的行没有一项能对上校验清单"}
    out = {"state": "ok", "age": round(age), "n": rb.get("n"),
           "why": "页面 %d 项读数与接口一致" % rb.get("n")}
    if rb.get("live_diff"):
        out["why"] += "（另有 %d 项盘中量随时间变化，不拦）" % len(rb["live_diff"])
    return out


def preview_order(lead_id):
    """批准前的预览：把这单到底要花多少、亏多少、占多少现金算清楚。
    只读，不动账本；让人在点"批准"之前先看见数字。"""
    st = load_state()
    lead = next((x for x in st["leads"] if x["id"] == lead_id), None)
    if not lead:
        raise ValueError("找不到线索 %s" % lead_id)
    if lead["state"] != "STAGED":
        raise ValueError("线索 %s 当前状态是 %s，只有 STAGED 才能批准"
                         % (lead_id, lead["state"]))
    plan = lead.get("plan") or {}
    code = lead["code"]
    shares = plan.get("shares") or 0
    entry = plan.get("entry") or 0
    stop = plan.get("stop") or 0
    target = plan.get("target") or 0
    amount = shares * entry
    comm = max(MIN_COMM, round(amount * COMMISSION, 2))
    trans = round(amount * TRANSFER, 2)
    total = amount + comm + trans
    risk = shares * (entry - stop)
    warns = []
    if any(p["code"] == code for p in st["positions"]):
        warns.append("已在持仓：批准会被执行层拒绝")
    if total > st["cash"]:
        warns.append("现金不足 %.2f 元" % (total - st["cash"]))
    if amount > st["equity"] * MAX_POS_PCT:
        warns.append("单票仓位超 %.0f%% 上限" % (MAX_POS_PCT * 100))
    if risk > st["equity"] * RISK_PER_TRADE * 1.3:
        warns.append("风险超单笔预算的 1.3 倍，计划可能已过期")
    if (lead.get("d") or "") != _today():
        warns.append("这张单挂在 %s，已过夜（行情可能已变）" % lead.get("d"))
    twins = [x["id"] for x in st["leads"]
             if x["code"] == code and x["state"] == "STAGED" and x["id"] != lead_id]
    if twins:
        warns.append("同码还有待审批线索 %s" % ",".join(twins))
    g = price_guard(lead)          # 价格复核：这一步只读行情，不动账本
    if not g["ok"]:
        warns.append(g["why"])
    rb = readback_guard(code)      # 读数复核：人看到的那一页，和机器判的数是不是同一个
    if rb["state"] == "drift":
        warns.append("页面读数与接口不一致，不能批：" + rb["why"])
    return {"lead": lead_id, "code": code, "name": lead["name"],
            "shares": shares, "entry": entry, "stop": stop, "target": target,
            "rr": plan.get("rr"), "amount": round(amount, 2),
            "fee": round(comm + trans, 2), "total": round(total, 2),
            "cash_before": st["cash"], "cash_after": round(st["cash"] - total, 2),
            "risk": round(risk, 2), "risk_pct": round(risk / st["equity"] * 100, 2),
            "live": g["live"], "drift_pct": g["drift_pct"],
            "price_ok": g["ok"], "price_why": g["why"],
            "readback": rb, "readback_ok": rb["state"] != "drift",
            "warns": warns}


def approve(lead_id):
    """人工批准。所有拒绝都要说清楚原因 —— 静默返回等于"点了没反应"，比报错更糟。"""
    with LOCK:
        st = load_state()
        lead = next((x for x in st["leads"] if x["id"] == lead_id), None)
        if not lead:
            raise ValueError("找不到线索 %s" % lead_id)
        if lead["state"] != "STAGED":
            raise ValueError("线索 %s 当前状态是 %s，只有 STAGED 才能批准"
                             % (lead_id, lead["state"]))
        # 幂等与防重复：同码已经在仓里，就不许再买一次
        if lead["code"] in {p["code"] for p in st["positions"]}:
            audit({"agent": "CHIEF", "action": "APPROVE_DENIED", "lead": lead_id,
                   "code": lead["code"], "why": "already_holding"})
            raise ValueError("%s 已在持仓中，重复批准会造成二次买入" % lead["code"])
        twins = [x["id"] for x in st["leads"]
                 if x["code"] == lead["code"] and x["state"] == "STAGED" and x["id"] != lead_id]
        if twins:
            audit({"agent": "CHIEF", "action": "APPROVE_DENIED", "lead": lead_id,
                   "why": "duplicate_open", "twins": twins})
            raise ValueError("同码还有待审批线索 %s，请先否决它再批这张"
                             % ",".join(twins))
        # 价格复核：STAGED 之后行情可能已经走掉，旧价成交会让股数和风险预算都失真
        g = price_guard(lead)
        if not g["ok"]:
            audit({"agent": "CHIEF", "action": "APPROVE_DENIED", "lead": lead_id,
                   "code": lead["code"], "why": "price_drift", "price": g})
            ev(st, "CHIEF", "%s 批准被拒：%s" % (lead_id, g["why"]), "warn")
            raise ValueError(g["why"])
        # 读数复核：人看着那一页按的批准，页面读回来的数必须和机器判的数一致。
        # 不一致说明"看到的就是错的"，这时候批准等于照旧数字下单 —— 直接拒。
        rb = readback_guard(lead["code"])
        if rb["state"] == "drift":
            audit({"agent": "CHIEF", "action": "APPROVE_DENIED", "lead": lead_id,
                   "code": lead["code"], "why": "readback_drift", "readback": rb})
            ev(st, "CHIEF", "%s 批准被拒：页面读数与接口不一致" % lead_id, "alert")
            save_state(st)
            raise ValueError("页面读数与接口不一致，不能批：" + rb["why"])
        ev(st, "CHIEF", "%s 人工批准" % lead_id, "ok")
        audit({"agent": "CHIEF", "action": "APPROVE", "lead": lead_id})
        lead["state"] = "APPROVED"
        ok = agent_fills(st, lead)
        agent_chief(st)
        ledger_check(st)
        save_state(st)
        if not ok:
            raise ValueError(lead.get("note") or "成交被拒绝")
        return st


def reject(lead_id, reason="MANUAL"):
    """人工否决：和机器否决一样留痕，但理由栏标为人。"""
    with LOCK:
        st = load_state()
        lead = next((x for x in st["leads"] if x["id"] == lead_id), None)
        if not lead:
            raise ValueError("找不到线索 %s" % lead_id)
        if lead["state"] != "STAGED":
            raise ValueError("线索 %s 当前状态是 %s，只有 STAGED 才能否决"
                             % (lead_id, lead["state"]))
        lead["state"] = "REJECTED"
        lead["note"] = "人工否决（%s）" % reason
        ev(st, "CHIEF", "%s 人工否决" % lead_id, "warn")
        audit({"agent": "CHIEF", "action": "REJECT", "lead": lead_id,
               "who": "human", "reason": reason})
        save_state(st)
        return st
        return st


def halt(reason="MANUAL"):
    """STOP ALL：停开新仓 + 尝试清仓（遵守 T+1）+ 留痕"""
    with LOCK:
        st = load_state()
        st["halt"] = True
        ev(st, "CHIEF", "STOP ALL 触发（%s），停止扫描与开仓" % reason, "alert")
        audit({"agent": "CHIEF", "action": "HALT", "reason": reason})
        for pos in list(st["positions"]):
            if pos["buy_date"] == _today():
                ev(st, "RISK", "%s T+1 锁定，明日开盘再平" % pos["code"], "warn")
                continue
            try:
                price = df.get_quote(pos["code"])["price"]
            except Exception:
                continue
            amount = pos["shares"] * price
            fee = fee_sell(amount)
            proceeds = round(amount - fee, 2)
            pnl = round(proceeds - pos["cost"], 2)
            st["cash"] = round(st["cash"] + proceeds, 2)
            st["positions"].remove(pos)
            st["stats"]["closed"] += 1
            st["stats"]["wins" if pnl > 0 else "losses"] += 1
            st["trades"].append({"id": pos["lead"], "code": pos["code"], "name": pos["name"],
                                 "side": "SELL", "shares": pos["shares"], "price": price,
                                 "amount": round(amount, 2), "fee": fee, "pnl": pnl,
                                 "reason": "STOP_ALL",
                                 "t": datetime.now().strftime("%m-%d %H:%M:%S")})
            ev(st, "RISK", "STOP ALL 平仓 %s @ %.2f，盈亏 %+.0f" % (pos["code"], price, pnl),
               "ok" if pnl > 0 else "warn")
        agent_chief(st, "已停摆")
        save_state(st)
        return st


def resume():
    with LOCK:
        st = load_state()
        st["halt"] = False
        st["day_start_equity"] = st["equity"]
        ev(st, "CHIEF", "恢复运行，日亏基准已重置", "ok")
        save_state(st)
        return st


def close_position(code, reason="MANUAL"):
    with LOCK:
        st = load_state()
        for pos in list(st["positions"]):
            if pos["code"] != code:
                continue
            if pos["buy_date"] == _today():
                ev(st, "RISK", "%s T+1 限制，当日买入不可卖出" % code, "warn")
                break
            try:
                price = df.get_quote(code)["price"]
            except Exception:
                break
            amount = pos["shares"] * price
            fee = fee_sell(amount)
            proceeds = round(amount - fee, 2)
            pnl = round(proceeds - pos["cost"], 2)
            st["cash"] = round(st["cash"] + proceeds, 2)
            st["positions"].remove(pos)
            st["stats"]["closed"] += 1
            st["stats"]["wins" if pnl > 0 else "losses"] += 1
            st["trades"].append({"id": pos["lead"], "code": code, "name": pos["name"],
                                 "side": "SELL", "shares": pos["shares"], "price": price,
                                 "amount": round(amount, 2), "fee": fee, "pnl": pnl,
                                 "reason": reason,
                                 "t": datetime.now().strftime("%m-%d %H:%M:%S")})
            ev(st, "RISK", "手动平仓 %s @ %.2f，盈亏 %+.0f" % (code, price, pnl),
               "ok" if pnl > 0 else "warn")
        agent_chief(st)
        save_state(st)
        return st
