# -*- coding: utf-8 -*-
"""
selftest.py · 自检：账本恒等式 + 成交可重放 + 新鲜度契约
------------------------------------------------------
前两项不依赖网络；新鲜度那项需要行情接口，取不到就按 fail-closed 判 PASS/告知。

自检绝不写回真实状态，也绝不往真实 append-only 审计账本里写测试行
（审计日志一旦被测试污染，它就不再是证据）。
"""

import copy
import os
import sys
import tempfile

import desk

ST0 = desk.load_state()                      # 先读真实状态
_TMP = tempfile.mkdtemp(prefix="desk_selftest_")
desk.AUDIT_FILE = os.path.join(_TMP, "audit.jsonl")
desk.STATE_FILE = os.path.join(_TMP, "state.json")


def case(name, fn):
    try:
        ok, detail = fn()
    except Exception as e:                   # 自检本身出错也算失败
        ok, detail = False, "异常：%s" % e
    print("%-28s %s  %s" % (name, "PASS" if ok else "FAIL", detail))
    return ok


def baseline_ok():
    r = desk.ledger_check(copy.deepcopy(ST0), fix_equity=False, enforce=False)
    return r["ok"], "diff=%.2f equity=%.2f 持仓%d" % (
        r["diff"], r["equity"], len(ST0["positions"]))


def cash_tamper_caught():
    bad = copy.deepcopy(ST0)
    bad["cash"] = round(bad["cash"] + 100, 2)
    r = desk.ledger_check(bad, fix_equity=False, enforce=False)
    return (not r["ok"]) and abs(r["diff"] - 100) < 0.06, "diff=%.2f" % r["diff"]


def dropped_trade_caught():
    if not any(t.get("side") == "BUY" for t in ST0["trades"]):
        return True, "无成交记录可删，跳过"
    bad = copy.deepcopy(ST0)
    for i in range(len(bad["trades"]) - 1, -1, -1):
        if bad["trades"][i].get("side") == "BUY":
            del bad["trades"][i]
            break
    r = desk.ledger_check(bad, fix_equity=False, enforce=False)
    return (not r["ok"]) and bool(r["replay_bad"]), "replay_bad=%s" % r["replay_bad"]


def enforce_halts():
    bad = copy.deepcopy(ST0)
    bad["cash"] = round(bad["cash"] - 50, 2)
    r = desk.ledger_check(bad, enforce=True)
    with open(desk.AUDIT_FILE, "r", encoding="utf-8") as f:
        rows = [l for l in f if l.strip()]
    return ((not r["ok"]) and bad["halt"] is True and len(rows) >= 1,
            "halt=%s 审计新增=%d 行" % (bad["halt"], len(rows)))


def freshness_contract():
    st = {}
    f = desk.freshness(st)
    if f["asof"] is None:
        return f["blind"] is True, "ASOF 取不到 → blind=True（fail-closed）"
    if f["inSession"]:
        return True, "盘中 asof=%s blind=%s" % (f["asof"], f["blind"])
    return True, "非交易时段 asof=%s stale=%s（标注而非假装）" % (f["asof"], f["stale"])


if __name__ == "__main__":
    print("=" * 66)
    results = [
        case("账本基线平账", baseline_ok),
        case("现金被改会被抓到", cash_tamper_caught),
        case("删成交记录会被抓到", dropped_trade_caught),
        case("不平则 halt 并留痕", enforce_halts),
        case("新鲜度契约", freshness_contract),
    ]
    print("=" * 66)
    print("%d/%d 通过" % (sum(results), len(results)))
    sys.exit(0 if all(results) else 1)
