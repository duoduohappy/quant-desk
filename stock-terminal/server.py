# -*- coding: utf-8 -*-
"""
server.py · 赛博交易室 · 服务入口
- /            行情终端（数据侧：实时/分时/日K/周K/月K/五日）
- /desk        交易室（8 智能体可视化）
- /api/...     数据接口
- /api/desk/... 交易室接口
"""

import json
import os
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import datafeed as df
import desk
import factors as facts
import plan

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = 8765
AUTO_INTERVAL = 180          # 自动跑一轮的间隔（秒）


_LOOP_LABELS = {"t": 0, "v": {}}


def _loop_labels(ttl=60):
    """闭环链路条要用的标签口径。60 秒缓存：每 3 秒轮询不该反复读 jsonl。

    注意口径：`settled` 是已经有收盘结果的唯一样本，`pending` 是判了但还没结果的。
    别拿影子分样本数(shadow.n)冒充它 —— 那个现在是 0，会让人误以为回写整条腿没跑。
    """
    import time as _t
    if _LOOP_LABELS["v"] and _t.time() - _LOOP_LABELS["t"] < ttl:
        return _LOOP_LABELS["v"]
    try:
        import labels as lab
        s = lab.summary()
        p = lab.pace()
        sh = (s.get("shadow") or {})
        v = {"samples": s.get("unique_samples"), "need": s.get("threshold"),
             "sample_days": s.get("sample_days"), "need_days": s.get("threshold_days"),
             "settled": p.get("settled_uniq"), "judged": p.get("judged_uniq"),
             "pending": s.get("pending"),
             "shadow_n": sh.get("n"),
             # 两个分母：还有多少条样本在等收盘定稿、多少条判断压根没影子分标签。
             # 不显示这两个数，“样本 0/300”就会像坏了，而其实是“盘中不算数 + 标签刚开始记”。
             "not_final": s.get("not_final"),
             "tagged": sh.get("records"), "untagged": sh.get("untagged")}
    except Exception as e:
        return {"err": str(e)}
    _LOOP_LABELS["t"], _LOOP_LABELS["v"] = _t.time(), v
    return v


def _state_payload():
    st = desk.load_state()
    try:
        desk.ledger_check(st, fix_equity=False, enforce=False)
        if not (st.get("freshness") or {}).get("asof"):
            desk.freshness(st)
    except Exception:
        pass
    for p in st["positions"]:
        last = p.get("last") or p["entry"]
        p["float_pnl"] = round((last - p["entry"]) * p["shares"], 2)
        p["float_pct"] = round((last / p["entry"] - 1) * 100, 2) if p["entry"] else 0
    st["day_pnl"] = round(st["equity"] - st["day_start_equity"], 2)
    st["total_pnl"] = round(st["equity"] - st["init"], 2)
    st["station_meta"] = [{"id": s[0], "zh": s[1], "en": s[2], "tag": s[3]}
                          for s in desk.STATIONS]
    # 现在跑的是哪一版参数：不带这个，页面上的分数就无法追到"哪套权重算出来的"
    p = desk.params()
    st["params"] = {"version": p.get("version"), "source": p.get("source"),
                    "updated_at": p.get("updated_at"),
                    "scan": p.get("scan"), "shadow": p.get("shadow")}
    # 自校准那条腿的状态（样本进度 + 上次评估结论），界面上要能看见
    try:
        import calibrate as cal
        st["calibration"] = cal.status()
    except Exception as e:
        st["calibration"] = {"ok": False, "why": "校准器未就绪：%s" % e}

    # 闭环链路条：采集 → 裁决 → 执行 → 记账 → 回写 → 经验。
    # 为什么要它：闭环的证据原来散在审计、几个接口和标签文件里，页面上看不见就等于没有。
    # 哪一段是 0、哪一段被跳过，就在哪一段断 —— 这条把断点直接摆出来。
    cyc = None
    try:
        if os.path.exists(desk.AUDIT_FILE):
            with open(desk.AUDIT_FILE, "r", encoding="utf-8") as f:
                for line in f.readlines()[-200:][::-1]:
                    line = line.strip()
                    if not line:
                        continue
                    r = json.loads(line)
                    if r.get("action") == "CYCLE_SUMMARY":
                        cyc = r
                        break
    except Exception:
        cyc = None
    leads_all = st.get("leads") or []
    c = st.get("calibration") or {}
    st["loop"] = {
        "cycle": st.get("cycle"), "leads": len(leads_all),
        "cands": (cyc or {}).get("cands"), "vetted": (cyc or {}).get("vetted"),
        "skipped": (cyc or {}).get("skipped") or {},
        "staged": sum(1 for l in leads_all if l.get("state") == "STAGED"),
        "positions": len(st.get("positions") or []),
        "labels": _loop_labels(),
    }
    return st


class Handler(BaseHTTPRequestHandler):
    server_version = "CyberDesk/1.0"

    def log_message(self, fmt, *args):
        pass

    def _send(self, status, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _file(self, name, ctype):
        fp = os.path.join(BASE_DIR, name)
        if not os.path.exists(fp):
            return self._send(404, {"error": name + " 缺失"})
        with open(fp, "rb") as f:
            return self._send(200, f.read().decode("utf-8"), ctype)

    def _bin(self, name, ctype):
        fp = os.path.join(BASE_DIR, "assets", os.path.basename(name))
        if not os.path.exists(fp):
            return self._send(404, {"error": name + " 缺失"})
        with open(fp, "rb") as f:
            raw = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        # 页面本身必须不缓存：带 max-age 时改完 HTML 浏览器还在跑旧版，
        # 前端改动要等一小时才算数（排查时会误以为是后端没生效）。
        self.send_header("Cache-Control", "no-store" if "html" in ctype else "max-age=300")
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        """前端所有写操作用 POST；参数走 query string。
        读数回传要带 body（页面行多，塞 query 会超长），所以先把 body 读出来。
        不读 body 就转发，会让请求看起来"发出去了"，而服务端压根没拿到内容。"""
        try:
            n = int(self.headers.get("Content-Length") or 0)
            self._body = self.rfile.read(n) if n > 0 else b""
        except Exception:
            self._body = b""
        return self.do_GET()

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

        if path in ("/", "/index.html"):
            return self._file("index.html", "text/html; charset=utf-8")
        if path in ("/desk", "/desk.html"):
            return self._file("desk.html", "text/html; charset=utf-8")
        if path == "/echarts.min.js":
            return self._file("echarts.min.js", "application/javascript; charset=utf-8")
        if path.startswith("/assets/"):
            name = path[len("/assets/"):]
            ctype = "image/png" if name.endswith(".png") else "application/octet-stream"
            return self._bin(name, ctype)

        try:
            # ---------- 数据侧 ----------
            if path == "/api/quote":
                return self._send(200, df.get_quote(qs.get("code", [""])[0]))
            if path == "/api/minute":
                return self._send(200, df.get_minute(
                    qs.get("code", [""])[0], int(qs.get("days", ["1"])[0])))
            if path == "/api/kline":
                return self._send(200, df.get_kline(
                    qs.get("code", [""])[0], qs.get("period", ["day"])[0]))
            # 因子板：纯函数算出来的证据（不做决策）
            if path == "/api/stock/factors":
                return self._send(200, facts.compute(qs.get("code", [""])[0]))
            # 仓位算术：三约束取最小 + 双边费用 + 回本涨幅（不写账本）
            if path == "/api/stock/plan":
                st = desk.load_state()
                return self._send(200, plan.compute(
                    qs.get("code", [""])[0], st["equity"], st["cash"]))

            # ---------- 可调参数：查看 / 生效 / 回滚 ----------
            # 自进化要改的权重全在 desk_params.json 里；改之前自动归档旧版本，
            # 所以"改坏了"永远能退回去 —— 没有这个，自动调参就是在裸奔。
            if path == "/api/desk/params":
                return self._send(200, {"current": desk.params(),
                                        "versions": desk.params_versions()})
            if path == "/api/desk/params/apply":
                patch = {}
                raw = qs.get("patch", [""])[0]
                if raw:
                    patch = json.loads(raw)
                src = qs.get("source", ["manual"])[0]
                note = qs.get("note", [""])[0]
                return self._send(200, desk.params_apply(patch, src, note))
            if path == "/api/desk/params/rollback":
                return self._send(200, desk.params_rollback(int(qs.get("to", ["0"])[0])))
            # 校准器：默认只评估（样本不够就什么都不改），apply=1 才真生效
            if path == "/api/desk/calibrate":
                import calibrate as cal
                apply = qs.get("apply", ["0"])[0] in ("1", "true", "yes")
                return self._send(200, cal.run(apply=apply))
            # ---------- 交易室 ----------
            if path == "/api/desk/state":
                return self._send(200, _state_payload())
            # 结果标签（只读文件，不联网、不触发回填）：这是"自我校准"的证据面
            if path == "/api/desk/labels":
                import labels as lab
                return self._send(200, lab.summary())
            if path == "/api/desk/run":
                st = desk.load_state()
                desk.run_cycle(st, "manual")
                return self._send(200, _state_payload())
            if path == "/api/desk/approve":
                desk.approve(qs.get("lead", [""])[0])
                return self._send(200, _state_payload())
            # 批准前的预览（只读）：股数 / 成本 / 止损 / 占用现金 / 风险占比 / 警告
            if path == "/api/desk/preview":
                return self._send(200, desk.preview_order(qs.get("lead", [""])[0]))
            # 读数回喂：把蜘蛛从页面上读到的行传上来，服务端重算 factors 逐项比对。
            # 这是一条真链路（页面 → 后端 → 批准闸门），不是展示用的回执。
            if path == "/api/desk/readback":
                raw = getattr(self, "_body", b"") or b""
                try:
                    items = json.loads(raw.decode("utf-8")) if raw else []
                except Exception:
                    items = []
                return self._send(200, desk.report_readback(qs.get("code", [""])[0], items))
            if path == "/api/desk/reject":
                desk.reject(qs.get("lead", [""])[0], qs.get("why", ["MANUAL"])[0])
                return self._send(200, _state_payload())
            if path == "/api/desk/close":
                desk.close_position(qs.get("code", [""])[0])
                return self._send(200, _state_payload())
            if path == "/api/desk/halt":
                desk.halt("STOP_ALL")
                return self._send(200, _state_payload())
            if path == "/api/desk/resume":
                desk.resume()
                return self._send(200, _state_payload())
            if path == "/api/desk/reset":
                desk.reset()
                return self._send(200, _state_payload())
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:
            return self._send(502, {"error": "上游数据异常：%s" % e})

        return self._send(404, {"error": "not found"})


def _in_trading_hours():
    from datetime import datetime
    d = datetime.now()
    if d.weekday() >= 5:
        return False
    hm = d.hour * 100 + d.minute
    return (930 <= hm <= 1130) or (1300 <= hm <= 1500)


def _auto_loop():
    time.sleep(45)
    while True:
        if _in_trading_hours():
            try:
                desk.run_cycle(desk.load_state(), "auto")
            except Exception as e:
                print("[auto] 跳过一轮：%s" % e)
            time.sleep(AUTO_INTERVAL)
        else:
            time.sleep(60)


if __name__ == "__main__":
    threading.Thread(target=_auto_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("赛博交易室已启动：")
    print("  行情终端  http://127.0.0.1:%d/" % PORT)
    print("  交易室    http://127.0.0.1:%d/desk" % PORT)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
