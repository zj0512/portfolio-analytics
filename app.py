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

def norm_date(d):
    """将日期统一为 YYYY-MM-DD。兼容 20240930 和 2026-09-08 两种输入。"""
    d = (d or "").strip()
    if len(d) == 8 and d.isdigit():
        return "%s-%s-%s" % (d[0:4], d[4:6], d[6:8])
    if len(d) >= 10:
        return d[0:10]
    return d

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
        dd = norm_date(d)
        flows.append((dd, v))
    # 合并 holdings 表的现金流(买入=负支出, 卖出=正收入) 用于 XIRR
    for hd, hcode, haction, hqty, hprice in cur.execute(
        "SELECT hdate, sec_code, action, qty, price FROM holdings WHERE sec_code IN (%s)"
        % ",".join("?"*len(CORE)), CORE):
        try:
            hq = float(hqty) if hqty else 0.0
            hp = float(hprice) if hprice else 0.0
        except: continue
        if hq == 0: continue
        dd = norm_date(hd)
        if haction == "BUY":
            flows.append((dd, -(hq * hp) if hp > 0 else -1.0))
        elif haction == "SELL":
            flows.append((dd, (hq * hp) if hp > 0 else 1.0))
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
    """资金加权年化. 时间太短(<min_days天)返回None避免失真。
    预计算每笔现金流的年份偏移(相对t0), 避免每次npv重复转日期, 大幅提速。"""
    if len(cfs) == 0: return None
    allcf = cfs + [(end_date, end_val)]
    t0 = min(c[0] for c in allcf)
    d0 = datetime.date(int(t0[0:4]), int(t0[5:7]), int(t0[8:10]))
    dN = datetime.date(int(end_date[0:4]), int(end_date[5:7]), int(end_date[8:10]))
    if (dN - d0).days < min_days:
        return None
    # 预计算年份偏移
    def yoff(ds):
        d = datetime.date(int(ds[0:4]), int(ds[5:7]), int(ds[8:10]))
        return (d - d0).days / 365.0
    amts = [(amt, yoff(ds)) for ds, amt in allcf]
    def npv(r):
        s = 0.0
        for amt, yrs in amts:
            s += amt / ((1 + r) ** yrs)
        return s
    lo, hi = -0.99, 10.0
    # 稀疏采样直到间隔足够小(约40次迭代足够, 原400次浪费)
    for _ in range(60):
        mid = (lo + hi) / 2
        if npv(mid) > 0: lo = mid
        else: hi = mid
    return (lo + hi) / 2 * 100

def compute_daily():
    """计算每日: 日期/持仓数量/总市值/XIRR。"""
    load_nav()
    flows = load_flow()
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
        recs.append((norm_date(d), code, p, q, o))
    # 合并 holdings 表: 用户录入的仓位变化(买入/卖出)也参与持仓重建
    for hd, hcode, haction, hqty, hprice in cur.execute(
        "SELECT hdate, sec_code, action, qty, price FROM holdings WHERE sec_code IN (%s)"
        % ",".join("?"*len(CORE)), CORE):
        try:
            hq = float(hqty) if hqty else 0.0
            hp = float(hprice) if hprice else 0.0
        except: continue
        if hq == 0: continue
        hd_fmt = norm_date(hd)
        if haction == "BUY":
            est_price = hp if hp > 0 else 0.0
            recs.append((hd_fmt, hcode, 0.0, hq, -hq * est_price if est_price else -1.0))
        elif haction == "SELL":
            est_price = hp if hp > 0 else 0.0
            recs.append((hd_fmt, hcode, est_price, hq, hq * est_price if est_price else 1.0))
    conn.close()
    recs.sort(key=lambda x: (x[0], x[1]))
    # 日期序列延伸到净值库最新交易日: 从首笔交易日起, 取所有净值日
    # 这样没交易的日子, 持仓不变但市值随净值每天更新, 曲线延伸到最新
    nav_conn = sqlite3.connect(DB)
    nav_cur = nav_conn.cursor()
    all_dates = [r[0] for r in nav_cur.execute("SELECT DISTINCT fsrq FROM fund_nav ORDER BY fsrq")]
    nav_conn.close()
    if recs:
        first_trade = recs[0][0]
        dates = [d for d in all_dates if d >= first_trade]
    else:
        dates = all_dates
    # 逐日市值 + XIRR (优化: 增量维护持仓, 预建净值索引, 避免O(n^2)重放)
    # 建立 交易日->当日晚到变更 的索引
    from collections import defaultdict
    trade_by_date = defaultdict(list)
    for d, code, pv, q, o in recs:
        trade_by_date[d].append((code, pv, q, o))
    # 预建净值: 每个代码的 (fsrq->dwjz) 有序列表, 用指针向前推进而非每次二分
    nav_idx = {c: _nav_sorted[c] for c in CORE}
    nav_ptr = {c: 0 for c in CORE}  # 指向 <= 当前日期 的最后一个

    def nav_for(code, dd):
        rows = nav_idx[code]
        p = nav_ptr[code]
        # 向前推进指针到 <= dd 的最后一个
        while p + 1 < len(rows) and rows[p + 1][0] <= dd:
            p += 1
        nav_ptr[code] = p
        return rows[p][1] if p >= 0 and rows[p][0] <= dd else None

    out = []
    pos = {c: 0.0 for c in CORE}
    flow_cf = []      # 截至当日的现金流(已发生的)
    # 现金流按日期分组(从flows)
    flow_by_date = defaultdict(list)
    for d, v in flows:
        flow_by_date[d].append(v)
    for dd in dates:
        # 应用该日期的交易变更(增量)
        for code, pv, q, o in trade_by_date.get(dd, []):
            if o < 0:
                pos[code] += q
            else:
                if pv > 0 and q > 0:
                    pos[code] -= q
        # 该日现金流并入
        flow_cf.extend((dd, v) for v in flow_by_date.get(dd, []))
        # 用当前持仓 + 当日净值算总市值
        total = 0.0
        for code in CORE:
            if pos[code] == 0: continue
            nv = nav_for(code, dd)
            if nv is not None:
                total += pos[code] * nv
        # xirr 只有从首笔现金流后有足够天数才计算
        yr = xirr(flow_cf, dd, total) if total > 0 else None
        out.append({"d": dd, "mv": round(total, 2), "xirr": round(yr, 2) if yr is not None else None,
                    "pos": {c: int(pos[c]) for c in CORE}})
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
        dd = norm_date(d)
        flows.append((dd, o))
        trades.append((dd, p, q, o))
    if not flows:
        return {"ok": False, "error": "无交易记录"}
    # 日期序列: 从首笔交易日起, 延伸到净值库最新交易日(持仓不变, 市值随净值更新)
    nav_conn = sqlite3.connect(DB)
    nav_cur = nav_conn.cursor()
    all_dates = [r[0] for r in nav_cur.execute("SELECT fsrq FROM fund_nav WHERE fund_code=? ORDER BY fsrq", (code,))]
    nav_conn.close()
    first_trade = min(f[0] for f in flows)
    dates = [d for d in all_dates if d >= first_trade]
    # 增量维护持仓 + 指针推进净值
    from collections import defaultdict
    trade_by_date = defaultdict(list)
    for d, p, q, o in trades:
        trade_by_date[d].append((p, q, o))
    nav_rows = _nav_sorted[code]
    nav_ptr = 0
    def nav_for(dd):
        nonlocal nav_ptr
        while nav_ptr + 1 < len(nav_rows) and nav_rows[nav_ptr + 1][0] <= dd:
            nav_ptr += 1
        if nav_ptr >= 0 and nav_rows[nav_ptr][0] <= dd:
            return nav_rows[nav_ptr][1]
        return None
    pos = 0.0
    flow_cf = []
    flow_by_date = defaultdict(list)
    for d, v in flows:
        flow_by_date[d].append(v)
    series = []
    for dd in dates:
        # 应用该日该基金交易(增量)
        for p, q, o in trade_by_date.get(dd, []):
            if o < 0:
                pos += q
            else:
                if p > 0 and q > 0:
                    pos -= q
        flow_cf.extend((dd, v) for v in flow_by_date.get(dd, []))
        nv = nav_for(dd)
        if nv is None:
            continue
        mv = pos * nv
        yr = xirr(flow_cf, dd, mv, min_days=15) if mv > 0 else None
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
    import atexit
    from scheduler import start_scheduler, stop_scheduler
    scheduler = start_scheduler()   # 启动工具时拉起定时任务(随工具启停, 零token)
    atexit.register(stop_scheduler, scheduler)  # 工具退出时停止
    print("启动投资组合分析工具: http://0.0.0.0:5050 (局域网可访问)")
    print("定时任务已加载: 工作日15:02 自动更新净值")
    app.run(host="0.0.0.0", port=5050, debug=False)
