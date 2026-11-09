"""结果回填（自进化的底座）。

为什么要有这个文件：
    desk_audit.jsonl 只记「当时判成什么」（score / verdict / failed / checks），
    不记「后来赚没赚」。没有结果标签，任何权重调整、阈值调整、
    甚至换成模型打分，都只是拿一批没有答案的样本去调参 —— 那不是自进化，是过拟合。

口径（写死在代码里，换成别的口径要在这里改，不许在报告里偷偷改）：
    entry = 决策日之后第一个交易日的开盘价
            理由：这套系统是 PAPER · MANUAL，人工点头之后才下单，
            不可能"按信号出现那一刻的价格成交"，用次日开盘最接近真实约束。
    r0 / r1 / r3 / r5 = 该开盘价之后第 0/1/3/5 根日K 的收盘价相对 entry 的涨跌幅
            r0 = 这笔"次日开盘买入、持有到收盘"的结果（最短周期）
    verdict 全量回填：CLEAR 和 REJECT 都要。
            REJECT 是反事实对照组 —— 没有它就无法回答"门槛是不是过严"。

只读 desk_audit.jsonl，只写 desk_labels.jsonl（append-only，同一 (ts, code) 取最后一行）。
不碰 desk_state.json，不影响下单。

用法：
    python labels.py            # 回填一次，打印覆盖率 + 分数分层 + CLEAR/REJECT 对照
    python labels.py --json     # 只输出机读结果
"""
import json
import os
import sys

import datafeed

HERE = os.path.dirname(os.path.abspath(__file__))
AUDIT_FILE = os.path.join(HERE, "desk_audit.jsonl")
LABEL_FILE = os.path.join(HERE, "desk_labels.jsonl")
DAYS = (0, 1, 3, 5)


def _read_jsonl(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue          # 半行/坏行直接跳过，别让一行脏数据把整批标签废掉
    return rows


def decisions():
    """从审计里挑出所有"对某只票做过判断"的记录（VET 是主口径）。"""
    out = []
    for r in _read_jsonl(AUDIT_FILE):
        if r.get("agent") != "VET" or not r.get("code"):
            continue
        out.append({
            "ts": r.get("ts", ""),
            "code": r.get("code"),
            "name": r.get("name") or "",
            "verdict": r.get("verdict"),
            "score": r.get("score"),
            "failed": r.get("failed") or [],
            "unknown": r.get("unknown") or [],
            "price": r.get("price"),
            # 影子分（只记录、不决策）：老审计行没有这个字段，缺就是缺，不补默认值
            "shadow": r.get("shadow"),
        })
    return out


def label_one(d, bars, name=""):
    """给一条判断算事后收益。bars 是该票的日K（升序）；返回 append-only 的一行。"""
    dec_date = (d["ts"] or "")[:10]
    row = {"ts": d["ts"], "code": d["code"], "name": d["name"] or name,
           "verdict": d["verdict"], "score": d["score"],
           "failed": d["failed"], "date": dec_date,
           "shadow": d.get("shadow"),
           "entry": None, "entry_d": None, "asof": None,
           "r0": None, "r1": None, "r3": None, "r5": None,
           "fwd": 0, "status": "wait_open", "at": ""}

    fwd = [b for b in bars if b["d"] > dec_date]
    if not fwd:
        return row                      # 决策日之后还没有交易日：pending，等他开盘

    entry = fwd[0]["o"]
    row["entry"] = round(entry, 3)
    row["entry_d"] = fwd[0]["d"]
    row["fwd"] = len(fwd)
    row["asof"] = fwd[-1]["d"]
    for k in DAYS:
        if len(fwd) > k and entry:
            row["r" + str(k)] = round((fwd[k]["c"] / entry - 1) * 100, 2)
    row["status"] = "done" if len(fwd) >= DAYS[-1] + 1 else "partial"
    row["day_final"] = _day_final(row["asof"])
    return row


def _day_final(asof):
    """asof 那天是不是"已经收盘、数字不会再动"。
    盘中每次跑一遍 r0 都会变（收盘价其实是现价），不区分的话标签文件会被原地翻倍，
    而且会把"盘中波动"当成"结果"。"""
    import datetime
    today = datetime.date.today().isoformat()
    if not asof or asof < today:
        return True
    now = datetime.datetime.now()
    return now.hour >= 15 and now.minute >= 5


def backfill(verbose=True):
    dec = decisions()
    keys_done = {}
    for r in _read_jsonl(LABEL_FILE):
        keys_done[(r.get("ts"), r.get("code"))] = r     # 后来的覆盖先前的

    # 同一只票只取一次K线（一轮扫描里同一 code 会被重复判很多次）
    codes = sorted({d["code"] for d in dec})
    bars_by_code, name_by_code = {}, {}
    for code in codes:
        try:
            k = datafeed.get_kline(code, "day", limit=120)
            bars_by_code[code] = k["items"]
            name_by_code[code] = k.get("name") or ""
        except Exception as e:
            bars_by_code[code] = []
            name_by_code[code] = "K线取不到：%s" % e

    fresh = []
    for d in dec:
        bars = bars_by_code.get(d["code"]) or []
        if not bars:
            continue
        row = label_one(d, bars, name_by_code.get(d["code"], ""))
        row["at"] = __import__("time").strftime("%Y-%m-%d %H:%M:%S")
        old = keys_done.get((d["ts"], d["code"]))
        # 只在"有变化"时才追加。注意这里不比 r0：盘中 r0 是现价，每跑一次都会变，
        # 比它会导致标签文件被反复重写。只在「有了新的完整交易日」或「当天收盘定稿」时追加。
        if old and (old.get("fwd") == row.get("fwd")
                    and old.get("entry") == row.get("entry")
                    and old.get("day_final") == row.get("day_final")):
            continue
        fresh.append(row)

    if fresh:
        with open(LABEL_FILE, "a", encoding="utf-8") as f:
            for row in fresh:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    merged = dict(keys_done)
    for row in fresh:
        merged[(row["ts"], row["code"])] = row
    return dec, list(merged.values()), fresh


def _bucket(score):
    if score is None:
        return "无分"
    if score >= 0.95:
        return "0.95~1.00"
    if score >= 0.85:
        return "0.85~0.95"
    if score >= 0.75:
        return "0.75~0.85"
    if score >= 0.60:
        return "0.60~0.75"
    return "<0.60"


def report(dec, labels, fresh):
    out = {"decisions": len(dec), "labels": len(labels), "new": len(fresh)}
    st = {}
    for r in labels:
        st[r["status"]] = st.get(r["status"], 0) + 1
    out["status"] = st

    # 只有拿到 r0 的样本能进分层表（其余是 pending，不能拿去算平均）
    got = [r for r in labels if r.get("r0") is not None]
    out["labeled_r0"] = len(got)

    # 一条判断 = 一个 cycle，同一交易日同一只票会被反复判很多次（VET 每轮都跑）。
    # 拿它当独立样本会虚增 n、把同一只票的收益重复计入 —— 校准必须按 (日期, 代码) 去重，
    # 保留当天最后一条（那才是这一天最终的态度）。
    dedup = {}
    for r in got:
        dedup[(r.get("date"), r.get("code"))] = r
    got = sorted(dedup.values(), key=lambda r: (r.get("date") or "", r.get("code") or ""))
    out["unique_samples"] = len(got)

    buckets = {}
    for r in got:
        b = buckets.setdefault(_bucket(r.get("score")), {"n": 0, "r0": [], "r1": [], "r5": []})
        b["n"] += 1
        b["r0"].append(r["r0"])
        if r.get("r1") is not None:
            b["r1"].append(r["r1"])
        if r.get("r5") is not None:
            b["r5"].append(r["r5"])
    out["by_score"] = {
        k: {"n": v["n"],
            "avg_r0": round(sum(v["r0"]) / len(v["r0"]), 2) if v["r0"] else None,
            "avg_r1": round(sum(v["r1"]) / len(v["r1"]), 2) if v["r1"] else None,
            "avg_r5": round(sum(v["r5"]) / len(v["r5"]), 2) if v["r5"] else None}
        for k, v in buckets.items()
    }
    by_verdict = {}
    for v in ("CLEAR", "REJECT"):
        rows = [r for r in got if r.get("verdict") == v]
        by_verdict[v] = {"n": len(rows),
                         "avg_r0": round(sum(r["r0"] for r in rows) / len(rows), 2) if rows else None}
    out["by_verdict"] = by_verdict
    out["file"] = LABEL_FILE
    out["bytes"] = os.path.getsize(LABEL_FILE) if os.path.exists(LABEL_FILE) else 0
    return out


def pace():
    """样本长得多快 —— 决定"什么时候才允许动权重"。

    只读审计、不联网。注意口径差别：
        uniq（这里）= 当天判过的**不同票数**，是"潜在样本"；
        summary.unique_samples = 已经有事后收益、能进对照的样本。
    两者差一天（要等下一个交易日开盘才有 entry），所以 ETA 用 uniq 估，
    真实可比日期还要再往后推一天。
    """
    rows = [r for r in _read_jsonl(AUDIT_FILE)
            if r.get("agent") == "VET" and r.get("code")]
    by_day = {}
    for r in rows:
        d = (r.get("ts") or "")[:10]
        if len(d) != 10:
            continue
        s = by_day.setdefault(d, {"vet": 0, "codes": set()})
        s["vet"] += 1
        s["codes"].add(r["code"])
    days, cum = [], 0
    for d in sorted(by_day):
        u = len(by_day[d]["codes"])
        cum += u
        days.append({"date": d, "vet": by_day[d]["vet"], "uniq": u, "cum": cum})
    recent = days[-5:]
    avg = round(sum(x["uniq"] for x in recent) / len(recent), 1) if recent else None

    # 已落地样本：有 entry（次日开盘价）的按 (date, code) 去重 —— 这才是能拿去做对照的
    latest = {}
    for r in _read_jsonl(LABEL_FILE):
        latest[(r.get("ts"), r.get("code"))] = r
    settled = {(r.get("date"), r.get("code")) for r in latest.values() if r.get("entry")}
    settled_n = len(settled)

    def eta(need, base):
        if not avg or avg <= 0:
            return None
        gap = max(0, need - base)
        return 0 if gap == 0 else int(-(-gap // avg))   # 向上取整

    return {
        "days": days[-10:],
        "judged_uniq": cum,          # 判过的不同票数（跨日累加）—— 潜在样本
        "settled_uniq": settled_n,   # 其中已有次日开盘价的 —— 真能对照的
        "avg_per_day": avg,          # 最近 5 个有数据日的日均新增
        "eta_20": eta(20, settled_n),    # 到"第一次能谈影子口径差异"
        "eta_300": eta(300, settled_n),  # 到"能谈权重"
        "note": "eta 按已落地样本算（不是判票数）；同码当日去重会压低 avg_per_day，那是业务规则不是 bug",
    }


def summary():
    """只读标签文件，不联网、不重算 —— 给接口/看板用。
    返回"是否有足够样本支撑校准"的判断，而不是一个好看的数字。"""
    rows = _read_jsonl(LABEL_FILE)
    latest = {}
    for r in rows:
        latest[(r.get("ts"), r.get("code"))] = r
    all_rows = list(latest.values())
    got = [r for r in all_rows if r.get("r0") is not None]
    dedup = {}
    for r in got:
        dedup[(r.get("date"), r.get("code"))] = r
    all_uniq = list(dedup.values())
    # 只有 day_final（那天已收盘）的样本才算“结果”。盘中 r0 其实是现价，
    # 拿它算平均等于拿没打完的比赛算比分 —— 这类样本单独计数（not_final），
    # 绝不混进任何均值、也不混进“覆盖了几个交易日”。
    uniq = [r for r in all_uniq if r.get("day_final")]
    by_verdict = {}
    for v in ("CLEAR", "REJECT"):
        rows_v = [r for r in uniq if r.get("verdict") == v]
        by_verdict[v] = {
            "n": len(rows_v),
            "avg_r0": round(sum(r["r0"] for r in rows_v) / len(rows_v), 2) if rows_v else None,
        }
    need = 300          # 门槛①：>=300 个 (交易日, 代码) 样本
    need_days = 20      # 门槛②：>=20 个交易日 —— 见下，光有样本数不够
    sample_days = len({r.get("date") for r in uniq if r.get("date")})

    # 影子分对照：同一批独立样本上，"风险折价口径"选中 vs 没选中，事后收益差多少。
    # 注意这是**观察**不是结论：n 小于 20 时任何差异都能被单个极端样本解释掉。
    # uniq 已经是“定稿样本”，所以这里不用再过滤 day_final
    sh = [r for r in uniq if isinstance(r.get("shadow"), dict)]
    sel = [r for r in sh if r["shadow"].get("sel")]
    nsel = [r for r in sh if not r["shadow"].get("sel")]

    def _avg(rows, k):
        vals = [r[k] for r in rows if r.get(k) is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    tagged = sum(1 for r in all_rows if isinstance(r.get("shadow"), dict))
    shadow = {
        # records=记了影子分的判断数（含还没出结果的）；untagged=压根没有影子分标签的判断数。
        # 两个数一起看才诚实：分母 148 条判断里可能只有 3 条带标签 —— 那不是“样本够了”。
        "records": tagged,
        "untagged": len(all_rows) - tagged,
        "n": len(sh),
        "sel_n": len(sel),
        "not_sel_n": len(nsel),
        "sel_r1": _avg(sel, "r1"),
        "not_sel_r1": _avg(nsel, "r1"),
        "sel_r0": _avg(sel, "r0"),
        "not_sel_r0": _avg(nsel, "r0"),
        "ready": len(sh) >= 20,
        "note": "records=已记下影子分的判断数（含还没出结果的）；n=其中已定稿、能算收益的。"
                "n<20 前的差异不构成证据；untagged 是有判断但完全没影子分标签的条数",
    }

    # 双门槛：样本数够 ≠ 市场状态够。
    # 一天判 40 只票、7 天就能凑出 280 个样本，但它们全挤在 7 个交易日里，
    # 彼此高度相关（同一批行情环境），能提供的独立信息量远小于 280。
    # 所以样本数之外必须再要 20 个交易日 —— 这不是变严，是别拿"同一天的 40 只票"冒充 40 个证据。
    ready = len(uniq) >= need and sample_days >= need_days
    return {
        "decisions": len(all_rows),
        "unique_samples": len(uniq),          # 定稿（已收盘）的唯一样本 —— 唯一可用的分母
        "not_final": len(all_uniq) - len(uniq),   # 有 r0 但还没收盘定稿的，不计入结论
        "sample_days": sample_days,
        "pending": len(all_rows) - len(got),
        "by_verdict": by_verdict,
        "shadow": shadow,
        "pace": pace(),
        "ready_for_shadow": ready,
        "need_more": max(0, need - len(uniq)),
        "need_days_more": max(0, need_days - sample_days),
        "threshold": need,
        "threshold_days": need_days,
        "note": "样本不够时任何分层/权重结论都不成立；ready=false 表示现在不该拟合模型（要样本数≥300 且交易日≥20）",
    }


def main():
    dec, labels, fresh = backfill()
    rep = report(dec, labels, fresh)
    if "--json" in sys.argv:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
        return
    print("判断记录 %d 条 / 结果标签 %d 条（本轮新增 %d）" % (rep["decisions"], rep["labels"], rep["new"]))
    print("状态分布：", rep["status"])
    print("能算 r0 的：%d 条判断 → 按(交易日,代码)去重后 %d 个独立样本" % (rep["labeled_r0"], rep["unique_samples"]))
    print("分数分层（r0 = 次日开盘买入持有到收盘的收益 %）：")
    for k, v in sorted(rep["by_score"].items()):
        print("  %-10s n=%-4d r0=%s%%  r1=%s%%  r5=%s%%" % (
            k, v["n"],
            "—" if v["avg_r0"] is None else v["avg_r0"],
            "—" if v["avg_r1"] is None else v["avg_r1"],
            "—" if v["avg_r5"] is None else v["avg_r5"]))
    print("判决对照（看门槛是不是太严）：")
    for k, v in rep["by_verdict"].items():
        print("  %-6s n=%-4d r0=%s%%" % (k, v["n"], "—" if v["avg_r0"] is None else v["avg_r0"]))
    print("标签文件：%s（%d B）" % (rep["file"], rep["bytes"]))


if __name__ == "__main__":
    main()
