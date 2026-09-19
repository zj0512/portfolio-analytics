# -*- coding: utf-8 -*-
"""Web 层: Flask 路由, 只做参数校验和调用 core, 不含业务逻辑。"""
from flask import Flask, jsonify, request, send_from_directory
from flask_compress import Compress

from core import calcs, db, fetchers, sim_alloc
from core import config
from core.config import STATIC, FUNDS, DEFAULT_FUND, PORT
from core.dates import today

app = Flask(__name__, static_folder=STATIC, static_url_path="/static")
Compress(app)
app.config["COMPRESS_MIN_SIZE"] = 500      # 小千500字节不压
app.config["COMPRESS_STREAMS"] = True        # 静态文件(send_from_directory流式)也压缩


@app.route("/")
def index():
    return send_from_directory(STATIC, "index.html")


@app.route("/api/daily")
def api_daily():
    """codes: 逗号分隔的基金代码, 缺省=全部; gran: day/week/month/quarter/year。"""
    codes = request.args.get("codes", "")
    gran = request.args.get("gran", "day")
    if gran not in ("day", "week", "month", "quarter", "year"):
        gran = "day"
    sel = [c for c in codes.split(",") if c in FUNDS] if codes else None
    return jsonify({"funds": FUNDS, "default_fund": DEFAULT_FUND,
                    "daily": calcs.compute_daily_cached(sel, gran)})


@app.route("/api/positions")
def api_positions():
    """最新持仓与价格: 每只基金当前持仓数量 + 最新净值。"""
    daily = calcs.compute_daily_cached()
    if not daily:
        return jsonify({"ok": False, "error": "无数据"})
    last = daily[-1]
    last_d = last["d"]
    rows = []
    for code in FUNDS:
        navs = db.nav_all(code)
        prices = db.price_all(code)
        price = float(prices[-1][1]) if prices else None
        nav = float(navs[-1][1]) if navs else None
        pos = (last.get("pos") or {}).get(code, 0)
        # 曲线末日之后录入的持仓变化(如今日买入)也计入当前持仓
        for hd, haction, hqty in db.holdings_after(code, last_d):
            try:
                hq = float(hqty) or 0.0
            except (TypeError, ValueError):
                continue
            pos = pos + hq if haction == "BUY" else pos - hq
        # 交易价缺失时回退净值(与 calcs 市值规则一致)
        price_used, price_date = price, (prices[-1][0] if prices else None)
        if price_used is None and nav is not None:
            price_used, price_date = nav, (navs[-1][0] if navs else None)
        rows.append({"code": code, "name": FUNDS[code], "pos": pos,
                     "price": price_used, "price_date": price_date,
                     "nav": nav, "nav_date": navs[-1][0] if navs else None})
    return jsonify({"ok": True, "date": last["d"], "positions": rows})


@app.route("/api/benchmark")
def api_benchmark():
    gran = request.args.get("gran", "day")
    if gran not in ("day", "week", "month", "quarter", "year"):
        gran = "day"
    return jsonify(calcs.benchmark_cached(gran))


@app.route("/api/period_return")
def api_period_return():
    """周期收益率: codes 同 daily; gran: day/week/month/quarter/year。"""
    codes = request.args.get("codes", "")
    gran = request.args.get("gran", "day")
    if gran not in ("day", "week", "month", "quarter", "year"):
        gran = "day"
    sel = [c for c in codes.split(",") if c in FUNDS] if codes else None
    out = calcs.compute_period_return(sel, gran)
    out["ok"] = True
    return jsonify(out)


@app.route("/api/nav")
def api_nav():
    code = request.args.get("code", "")
    if code not in FUNDS:
        return jsonify({"ok": False, "error": "bad code"})
    series = [{"d": d, "v": v} for d, v in db.nav_all(code)]
    return jsonify({"ok": True, "series": series})


@app.route("/api/fund_xirr")
def api_fund_xirr():
    code = request.args.get("code", "")
    gran = request.args.get("gran", "day")
    if gran not in ("day", "week", "month", "quarter", "year"):
        gran = "day"
    if code not in FUNDS:
        return jsonify({"ok": False, "error": "bad code"})
    return jsonify(calcs.compute_fund_daily(code, gran))


@app.route("/api/holding", methods=["GET"])
def api_holding_list():
    return jsonify({"ok": True, "holdings": db.holdings_list()})


@app.route("/api/holding", methods=["POST"])
def api_holding_add():
    body = request.get_json(force=True)
    date = body.get("date", "")
    code = (body.get("code", "") or "").strip()
    name = (body.get("name") or "").strip()   # 新标的可选名称
    action = body.get("action", "")
    qty = body.get("qty")
    price = body.get("price")
    if not date or not qty:
        return jsonify({"ok": False, "error": "缺失字段或代码非法"})
    if code not in FUNDS:
        # 新标的: 自动注册, 全模块立即可见
        ok, err = config.add_fund(code, name)
        if not ok:
            return jsonify({"ok": False, "error": err or "代码非法"})
        try:
            fetchers.sync_on_startup()
        except Exception:
            pass
    new_id = db.holding_add(date, code, action, qty, price)
    calcs.cache_clear_all()
    return jsonify({"ok": True, "id": new_id, "received": body,
                    "name": FUNDS.get(code, code)})


@app.route("/api/sim_alloc", methods=["GET"])
def api_sim_alloc_get():
    return jsonify({"ok": True, "alloc": db.sim_alloc_get()})


@app.route("/api/sim_alloc", methods=["POST"])
def api_sim_alloc_post():
    """保存并计算模拟: body {alloc:{code:weight,...}}。权重实时覆盖存库。"""
    body = request.get_json(force=True)
    alloc = body.get("alloc")
    if not isinstance(alloc, dict):
        return jsonify({"ok": False, "error": "alloc 必须是 {code:weight} 对象"})
    result = sim_alloc.compute_sim(alloc)
    if result.get("ok"):
        db.sim_alloc_set(alloc)  # 每次修改实时覆盖
    return jsonify(result)


@app.route("/api/sim_targets", methods=["GET"])
def api_sim_targets_get():
    """拟持仓目标值 {code:number}。"""
    return jsonify({"ok": True, "targets": db.sim_targets_get() or {}})


@app.route("/api/sim_targets", methods=["POST"])
def api_sim_targets_post():
    body = request.get_json(force=True)
    targets = body.get("targets")
    if not isinstance(targets, dict):
        return jsonify({"ok": False, "error": "targets 必须是 {code:number} 对象"})
    clean = {}
    for k, v in targets.items():
        if k in FUNDS:
            try:
                clean[k] = float(v)
            except (TypeError, ValueError):
                pass
    db.sim_targets_set(clean)
    return jsonify({"ok": True, "saved": clean})


@app.route("/api/sync", methods=["POST"])
def api_sync():
    """手动触发一次全量补同步。"""
    result = fetchers.sync_on_startup()
    return jsonify({"ok": True, "result": result})


def run():
    print("启动投资组合分析工具: http://0.0.0.0:%d (局域网可访问)" % PORT)
    print("定时任务已加载: 工作日15:02 自动更新净值; 启动时自动补同步")
    app.run(host="0.0.0.0", port=PORT, debug=False)
