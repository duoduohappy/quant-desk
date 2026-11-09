# -*- coding: utf-8 -*-
"""校准器 · 自进化 L1 的"建议端"。

它做什么：
    读结果标签（labels.summary），判断"影子口径（把风险折进分数）到底比线上口径好还是差"，
    只有在**样本够**且**差异超出噪音**时，才给出一个有界的单参数调整建议。

它刻意不做什么：
    - 不联网、不重新回填、不改账本
    - 不在样本不够时"先调一点试试" —— 那不是校准，是拿真钱做试验
    - 不一次改多项（改了多项，将来好坏都归不了因）

口径（写死在这里，改之前想清楚）：
    主比较量：影子"会被选中"那组的 r0 均值 - "不会选中"那组的 r0 均值
    r0 = 次日开盘买入、持有到当天日K 收盘的收益（最短周期，样本最快到位）
    r1（多持一天）只作为附带参考一起打印，**不参与是否动手的判定** —— 原因见下。
    为什么主判据换成 r0：
        r1 要等"决策日之后的第 2 个交易日"才存在。实测标签文件里 r1 有值 0 条、
        r0 有值 206 条 —— 校准器一直盯着一栏永远空着的数字，那不是严格，是空转。
    为什么只用定稿样本：
        盘中 r0 是现价不是收盘价。拿现价算"平均收益"，等于拿没打完的比赛算比分。
        labels.summary 已按 day_final 过滤，这里直接信任它的 n，不再自己判断。
    达标题条件（全满足才动）：
        shadow.n >= 20（能算收益的影子样本）
        两组各 >= 5
        样本覆盖 >= 20 个交易日（否则一天 40 只票能凑出"20 个样本"，但它们不是 20 个证据）
        差异 >= +0.5 个百分点（低于这个差异，胜负常由单个极端样本决定）
    动作：只动 scan 权重，一次最多两项、每项 0.05，方向是"少追热、多看资金/成交"。

用法：
    python calibrate.py             # 只评估、只打印（默认不写任何文件）
    python calibrate.py --propose   # 达标则写 desk_params.next.json + 审计（仍不生效）
    python calibrate.py --auto      # 达标则直接 params_apply（带"一天最多一次"限制）
"""
import json
import os
import sys

import desk
import labels

MIN_N = 20
MIN_GROUP = 5
MIN_DAYS = 20
MIN_EDGE = 0.5          # 个百分点
MAX_STEP = 0.05
NEXT_FILE = os.path.join(desk.BASE_DIR, "desk_params.next.json")


def _gate(s):
    """逐条检查门槛，返回 (ok, 原因, 主差异 r0, 附带差异 r1)。"""
    sh = (s.get("shadow") or {})
    n, sel_n, nsel_n = sh.get("n") or 0, sh.get("sel_n") or 0, sh.get("not_sel_n") or 0
    sel_r0, nsel_r0 = sh.get("sel_r0"), sh.get("not_sel_r0")
    sel_r1, nsel_r1 = sh.get("sel_r1"), sh.get("not_sel_r1")
    days = s.get("sample_days") or 0
    rec, unt = sh.get("records") or 0, sh.get("untagged") or 0
    fails = []
    if n < MIN_N:
        fails.append("定稿影子样本 n=%d < %d（另有 %d 条等收盘定稿）"
                     % (n, MIN_N, s.get("not_final") or 0))
    if rec and unt and rec < MIN_N:
        # 分母要说清楚：判断很多、但带影子分标签的很少 —— 那不是"样本快够了"
        fails.append("影子分标签只覆盖 %d/%d 条判断（11-09 起才开始记）" % (rec, rec + unt))
    if sel_n < MIN_GROUP or nsel_n < MIN_GROUP:
        fails.append("两组样本不足（选%d / 未选%d，各需≥%d）" % (sel_n, nsel_n, MIN_GROUP))
    if sel_r0 is None or nsel_r0 is None:
        fails.append("r0 还没定稿")
    if days < MIN_DAYS:
        fails.append("只覆盖 %d 个交易日 < %d" % (days, MIN_DAYS))
    edge = (sel_r0 - nsel_r0) if (sel_r0 is not None and nsel_r0 is not None) else None
    aux = (sel_r1 - nsel_r1) if (sel_r1 is not None and nsel_r1 is not None) else None
    return (not fails), fails, edge, aux


def evaluate():
    s = labels.summary()
    ok, fails, edge, aux = _gate(s)
    sh = (s.get("shadow") or {})
    out = {"ok": ok, "fails": fails, "edge": edge, "edge_r1": aux,
           "n": sh.get("n"), "sample_days": s.get("sample_days"),
           "not_final": s.get("not_final"),
           "tagged": sh.get("records"), "untagged": sh.get("untagged")}
    if not ok:
        out["why"] = "样本不足，不改：%s" % "；".join(fails)
        return out
    if edge is None or abs(edge) < MIN_EDGE:
        out["why"] = ("差异 %s 在噪音范围内（门槛 ±%.1f 个百分点），不改" %
                      ("—" if edge is None else "%.2f" % edge, MIN_EDGE))
        out["action"] = None
        return out
    if edge < 0:
        out["why"] = ("影子口径更差（%.2f 个百分点），线上口径不动 —— "
                      "负向结果也是结果，不为凑数去反向调参" % edge)
        out["action"] = None
        return out

    # 达标：朝"风险折价告一段落少追热"的方向动一小步（只动两项，各 0.05）
    cur = desk.params()
    P = cur["scan"]
    patch = {"scan": {
        "w_pct": round(max(0.05, P["w_pct"] - MAX_STEP), 3),
        "w_flow": round(min(0.40, P["w_flow"] + MAX_STEP), 3),
    }}
    out["action"] = patch
    out["why"] = ("影子选中组 r0 高出 %.2f 个百分点（n=%d，%d 个交易日）："
                  "说明把波动/估值/资金折进排序更有效，建议把涨幅权重降 %.2f、资金流权重升 %.2f" %
                  (edge, out["n"], out["sample_days"], MAX_STEP, MAX_STEP))
    return out


def _applied_today():
    """今天已经自动改过几次 —— 一天最多一次，避免"每天调一点"把参数调成噪音。"""
    n = 0
    today = desk._today()
    if not os.path.exists(desk.AUDIT_FILE):
        return 0
    with open(desk.AUDIT_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if (r.get("action") == "PARAMS_APPLY"
                    and str(r.get("source") or "").startswith("calibrate")
                    and str(r.get("ts") or "").startswith(today)):
                n += 1
    return n


_STATUS = {"t": 0, "v": None}


def status(ttl=60):
    """给页面用的一行状态：样本进度 + 上次评估结论。缓存 60 秒，别让 3 秒轮询反复算。

    这一行的意义：把"经验 → 行动"那条腿摆到界面上。没有它，自校准就是个
    只在 API 里存在、界面完全看不见的传说。
    """
    import time as _t
    if _STATUS["v"] is not None and _t.time() - _STATUS["t"] < ttl:
        return _STATUS["v"]
    try:
        s = labels.summary()
    except Exception as e:
        return {"ok": False, "why": "标签读取失败：%s" % e}
    ok, fails, edge, aux = _gate(s)
    sh = s.get("shadow") or {}
    out = {
        "ok": ok, "edge": edge, "edge_r1": aux, "n": sh.get("n"), "sel_n": sh.get("sel_n"),
        "not_sel_n": sh.get("not_sel_n"),
        "tagged": sh.get("records"), "untagged": sh.get("untagged"),
        "samples": s.get("unique_samples"), "need": s.get("threshold"),
        "not_final": s.get("not_final"),
        "sample_days": s.get("sample_days"), "need_days": s.get("threshold_days"),
        "ready_for_shadow": s.get("ready_for_shadow"),
        "metric": "r0（次日开盘→当日收盘，已定稿）",
        "why": ("达标：r0 差异 %.2f 个百分点，本次收盘后可调" % edge) if ok
               else "样本不足，不改：%s" % "；".join(fails),
        "last": None,
    }
    # 上一次评估留下的痕迹（说明它真的在跑，不是只写了段代码）
    try:
        if os.path.exists(desk.AUDIT_FILE):
            with open(desk.AUDIT_FILE, "r", encoding="utf-8") as f:
                for line in f.readlines()[-300:][::-1]:
                    line = line.strip()
                    if not line:
                        continue
                    r = json.loads(line)
                    if r.get("action") in ("CALIBRATE", "CALIBRATE_APPLY"):
                        out["last"] = {"at": r.get("ts"), "ok": r.get("ok"),
                                       "why": r.get("why"), "edge": r.get("edge")}
                        break
    except Exception:
        pass
    _STATUS["t"], _STATUS["v"] = _t.time(), out
    return out


def run(apply=False, propose=False):
    r = evaluate()
    desk.audit({"agent": "CHIEF", "action": "CALIBRATE",
                "ok": r["ok"], "edge": r["edge"], "n": r["n"],
                "why": r["why"], "suggest": r.get("action")})
    if not r["ok"] or not r.get("action"):
        return r
    patch, why = r["action"], r["why"]

    if apply:
        n_today = _applied_today()
        if n_today >= 1:
            r["why"] = "今天已经自动校准过 %d 次，本次跳过（限速：一天一次）" % n_today
            r["applied"] = False
            return r
        new = desk.params_apply(patch, source="calibrate:auto", note=why)
        r["applied"] = True
        r["version"] = new["version"]
        desk.audit({"agent": "CHIEF", "action": "CALIBRATE_APPLY",
                    "version": new["version"], "edge": r["edge"], "n": r["n"]})
        return r

    with open(NEXT_FILE, "w", encoding="utf-8") as f:
        json.dump({"patch": patch, "why": why, "edge": r["edge"], "n": r["n"],
                   "sample_days": r["sample_days"],
                   "at": desk.datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
                  f, ensure_ascii=False, indent=1)
    r["proposed"] = True
    r["file"] = NEXT_FILE
    return r


def main():
    apply = "--auto" in sys.argv
    propose = "--propose" in sys.argv
    r = run(apply=apply, propose=propose)
    print(json.dumps(r, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
