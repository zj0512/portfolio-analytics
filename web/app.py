# -*- coding: utf-8 -*-
"""Web 层: Flask 路由, 只做参数校验和调用 core, 不含业务逻辑。"""
from flask import Flask, jsonify, request, send_from_directory

from core import calcs, db, fetchers
from core.config import STATIC, FUNDS, DEFAULT_FUND, PORT
from core.dates import today

app = Flask(__name__, static_folder=STATIC, static_url_path="/static")


@app.route("/")
def index():
    return send_from_directory(STATIC, "index.html")


@app.route("/api/daily")
def api_daily():
    return jsonify({"funds": FUNDS, "default_fund": DEFAULT_FUND,
                    "daily": calcs.compute_daily()})


@app.route("/api/benchmark")
def api_benchmark():
    return jsonify(calcs.compute_benchmark())


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
    if code not in FUNDS:
        return jsonify({"ok": False, "error": "bad code"})
    return jsonify(calcs.compute_fund_daily(code))


@app.route("/api/holding", methods=["GET"])
def api_holding_list():
    return jsonify({"ok": True, "holdings": db.holdings_list()})


@app.route("/api/holding", methods=["POST"])
def api_holding_add():
    body = request.get_json(force=True)
    date = body.get("date", "")
    code = body.get("code", "")
    action = body.get("action", "")
    qty = body.get("qty")
    price = body.get("price")
    if not date or code not in FUNDS or not qty:
        return jsonify({"ok": False, "error": "缺失字段或代码非法"})
    new_id = db.holding_add(date, code, action, qty, price)
    return jsonify({"ok": True, "id": new_id, "received": body})


@app.route("/api/sync", methods=["POST"])
def api_sync():
    """手动触发一次全量补同步。"""
    result = fetchers.sync_on_startup()
    return jsonify({"ok": True, "result": result})


def run():
    print("启动投资组合分析工具: http://0.0.0.0:%d (局域网可访问)" % PORT)
    print("定时任务已加载: 工作日15:02 自动更新净值; 启动时自动补同步")
    app.run(host="0.0.0.0", port=PORT, debug=False)
