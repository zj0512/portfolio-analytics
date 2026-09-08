# -*- coding: utf-8 -*-
"""投资组合分析工具 · 本地 Web 应用 (Flask + SQLite)
架构可演进: 数据库存数据, 后端算日年化/XIRR, 前端 ECharts 展示 + 输入窗口。
"""
import os, json, sqlite3, datetime, bisect
from collections import OrderedDict
from flask import Flask, jsonify, request, send_from_directory

BASE = r"C:\Users\zc\.openclaw\workspace"
DB = os.path.join(BASE, "portfolio.db")          # 净值库
DZ_DB = os.path.join(BASE, "portfolio_duizhang.db")  # 对账单库
STATIC = os.path.join(BASE, "portfolio_app", "static")

# 标的映射
FUNDS = {
 "159682":"创业五零","159593":"A50指数","515180":"100红利","563020":"低波红利",
 "511090":"30年国债","511130":"国债30年","159649":"国开债","511360":"短融ETF",
 "159937":"黄金9999","518880":"黄金ETF","513100":"纳指ETF","513650":"标普ETF",
 "159516":"半导设备"
}
CORE = list(FUNDS.keys())

app = Flask(__name__, static_folder=STATIC, static_url_path="/static")

# ---------- 数据加载(启动时缓存, 后续可刷新) ----------
_nav_sorted = None     # {code: [(fsrq,dwjz),...]}
_daily_pos = None      # {YYYY-MM-DD: {code:qty}}
_flow = None           # [(YYYY-MM-DD, amt), ...]

def load_nav():
    global _nav_sorted
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    s = {}
    for code in CORE:
        rows = cur.execute("SELECT fsrq, dwjz FROM fund_nav WHERE fund_code=? ORDER BY fsrq", (code,)).fetchall()
        s[code] = rows
    conn.close()
    _nav_sorted = s
    return s

def load_flow():
    """从对账单读取核心标的现金流(发生金额负=买, 正=卖/分红), 统一日期。"""
    global _flow
    conn = sqlite3.connect(DZ_DB)
    cur = conn.cursor()
    flows = []
    for d, occur in cur.execute(
        "SELECT trade_date, occur_amount FROM duizhang_record WHERE sec_code IN (%s)"
        % ",".join("?"*len(CORE)), CORE):
        try: v = float(occur) if occur else 0
        except: continue
        if v == 0: continue
        dd = "%s-%s-%s" % (d[0:4], d[4:6], d[6:8])
        flows.append((dd, v))
    conn.close()
    flows.sort()
    _flow = flows
    return flows

def get_nav(code, fsrq):
    rows = _nav_sorted[code]
    keys = [r[0] for r in rows]
    idx = bisect.bisect_right(keys, fsrq) - 1
    return rows[idx][1] if idx >= 0 else None

def xirr(cfs, end_date, end_val, min_days=30):
    """资金加权年化. 时间太短(<min_days天)返回None避免失真。"""
    if len(cfs) == 0: return None
    allcf = cfs + [(end_date, end_val)]
    t0 = min(c[0] for c in allcf)
    d0 = datetime.date(int(t0[0:4]), int(t0[5:7]), int(t0[8:10]))
    dN = datetime.date(int(end_date[0:4]), int(end_date[5:7]), int(end_date[8:10]))
    if (dN - d0).days < min_days:
        return None
    def npv(r):
        s = 0.0
        for ds, amt in allcf:
            d = datetime.date(int(ds[0:4]), int(ds[5:7]), int(ds[8:10]))
            yrs = (d - d0).days / 365.0
            s += amt / ((1 + r) ** yrs)
        return s
    lo, hi = -0.99, 10.0
    for _ in range(400):
        mid = (lo + hi) / 2
        if npv(mid) > 0: lo = mid
        else: hi = mid
    return (lo + hi) / 2 * 100

def compute_daily():
    """计算每日: 日期/持仓数量/总市值/XIRR。"""
    load_nav()
    flows = load_flow()
    # 重建每日持仓
    pos = {c: 0.0 for c in CORE}
    daily_pos = OrderedDict()
    flow_by_date = {}
    for d, v in flows:
        flow_by_date[d] = flow_by_date.get(d, 0.0) + v
        if v < 0:
            pass
    # 需要对每笔知道数量和代码来重建持仓 → 用数据库重读
    conn = sqlite3.connect(DZ_DB)
    cur = conn.cursor()
    recs = []
    for d, code, price, qty, occur in cur.execute(
        "SELECT trade_date, sec_code, price, qty, occur_amount FROM duizhang_record WHERE sec_code IN (%s)"
        % ",".join("?"*len(CORE)), CORE):
        try:
            p = float(price) if price else 0.0
            q = float(qty) if qty else 0.0
            o = float(occur) if occur else 0.0
        except: continue
        recs.append((d, code, p, q, o))
    conn.close()
    recs.sort(key=lambda x: (x[0], x[1]))
    pos = {c: 0.0 for c in CORE}
    dates = []
    for d, code, p, q, o in recs:
        if o == 0: continue
        if o < 0: pos[code] += q
        else:
            if p > 0 and q > 0: pos[code] -= q
        dd = "%s-%s-%s" % (d[0:4], d[4:6], d[6:8])
        dates.append(dd)
    dates = sorted(set(dates))
    # 逐日市值 + XIRR
    out = []
    flow_cfs = [(d, v) for d, v in flows]
    for dd in dates:
        # 该日的持仓 = 重放所有 <= dd 的交易
        p = {c: 0.0 for c in CORE}
        for d, code, pv, q, o in recs:
            day = "%s-%s-%s" % (d[0:4], d[4:6], d[6:8])
            if day > dd: break
            if o == 0: continue
            if o < 0: p[code] += q
            else:
                if pv > 0 and q > 0: p[code] -= q
        total = 0.0
        for code in CORE:
            if p[code] == 0: continue
            nv = get_nav(code, dd)
            if nv is not None: total += p[code] * nv
        cf = [(d, v) for d, v in flow_cfs if d <= dd]
        yr = xirr(cf, dd, total) if total > 0 else None
        out.append({"d": dd, "mv": round(total, 2), "xirr": round(yr, 2) if yr is not None else None,
                    "pos": {c: int(p[c]) for c in CORE}})
    return out

# ---------- API ----------
@app.route("/")
def index():
    return send_from_directory(STATIC, "index.html")

@app.route("/api/daily")
def api_daily():
    data = compute_daily()
    return jsonify({"funds": FUNDS, "daily": data})

@app.route("/api/nav")
def api_nav():
    """返回某只基金的净值序列(用于单基金图表)。"""
    code = request.args.get("code", "")
    if code not in CORE:
        return jsonify({"ok": False, "error": "bad code"})
    load_nav()
    rows = _nav_sorted[code]
    series = [{"d": r[0], "v": r[1]} for r in rows]
    return jsonify({"ok": True, "series": series})

def compute_fund_daily(code):
    """单只基金的个人年化曲线(XIRR, 基于你的现金流+该基金期末市值)。"""
    load_nav()
    conn = sqlite3.connect(DZ_DB)
    cur = conn.cursor()
    recs = cur.execute("SELECT trade_date, price, qty, occur_amount FROM duizhang_record WHERE sec_code=? ORDER BY trade_date", (code,)).fetchall()
    conn.close()
    # 现金流(负=买, 正=卖出), 用于XIRR
    flows = []
    trades = []
    for d, price, qty, occur in recs:
        try:
            p = float(price) if price else 0.0
            q = float(qty) if qty else 0.0
            o = float(occur) if occur else 0.0
        except:
            continue
        if o == 0:
            continue
        dd = "%s-%s-%s" % (d[0:4], d[4:6], d[6:8])
        flows.append((dd, o))
        trades.append((dd, p, q, o))
    if not flows:
        return {"ok": False, "error": "无交易记录"}
    # 逐日: 到某日为止在该基金的持仓数量 × 当日净值 = 市值
    dates = sorted(set(f[0] for f in flows))
    series = []
    for dd in dates:
        # 重放到dd日的持仓
        pos = 0.0
        for d, p, q, o in trades:
            if d > dd:
                break
            if o < 0:
                pos += q
            else:
                if p > 0 and q > 0:
                    pos -= q
        nv = get_nav(code, dd)
        if nv is None or pos <= 0:
            continue
        mv = pos * nv
        cfs = [(d, v) for d, v in flows if d <= dd]
        yr = xirr(cfs, dd, mv, min_days=15)
        series.append({"d": dd, "mv": round(mv, 2), "xirr": round(yr, 2) if yr is not None else None})
    return {"ok": True, "code": code, "name": FUNDS.get(code, code), "series": series}

@app.route("/api/fund_xirr")
def api_fund_xirr():
    """单只基金的个人年化(XIRR)曲线。"""
    code = request.args.get("code", "")
    if code not in CORE:
        return jsonify({"ok": False, "error": "bad code"})
    return jsonify(compute_fund_daily(code))

@app.route("/api/holding", methods=["GET"])
def api_holding_list():
    """列出已录入的持仓变化记录。"""
    conn = sqlite3.connect(DZ_DB)
    cur = conn.cursor()
    cur.execute("SELECT id, hdate, sec_code, action, qty, price FROM holdings ORDER BY hdate")
    rows = [{"id": r[0], "date": r[1], "code": r[2], "action": r[3], "qty": r[4], "price": r[5]} for r in cur.fetchall()]
    conn.close()
    return jsonify({"ok": True, "holdings": rows})

@app.route("/api/holding", methods=["POST"])
def api_holding():
    """录入持仓变化(输入窗口). body: {date, code, action, qty, price?}
    写入 holdings 表, 持久化。"""
    body = request.get_json(force=True)
    date = body.get("date", "")
    code = body.get("code", "")
    action = body.get("action", "")
    qty = body.get("qty")
    price = body.get("price")
    if not date or code not in CORE or not qty:
        return jsonify({"ok": False, "error": "缺失字段或代码非法"})
    conn = sqlite3.connect(DZ_DB)
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS holdings (id INTEGER PRIMARY KEY AUTOINCREMENT, hdate TEXT, sec_code TEXT, action TEXT, qty REAL, price REAL)")
    cur.execute("INSERT INTO holdings (hdate, sec_code, action, qty, price) VALUES (?,?,?,?,?)",
                (date, code, action, qty, price))
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return jsonify({"ok": True, "id": new_id, "received": body})

if __name__ == "__main__":
    print("启动投资组合分析工具: http://0.0.0.0:5050 (局域网可访问)")
    app.run(host="0.0.0.0", port=5050, debug=False)
