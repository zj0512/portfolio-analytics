# -*- coding: utf-8 -*-
"""计算层: XIRR、整体组合曲线、基准对比、单基金曲线。"""
import bisect
import datetime
import threading
import time
from collections import defaultdict

from . import db
from .config import CORE, FUNDS, INDEX_NAME

# compute_daily 结果短 TTL 缓存: 页面首屏 /api/daily 与 /api/positions 各调一次,
# 净值每日只更新一次, 30s 缓存足够新鲜且免重复计算
_cache = {"lock": threading.Lock(), "t": 0.0, "val": None}
_CACHE_TTL = 30.0


def xirr(cfs, end_date, end_val, min_days=30, prev_rate=None):
    """资金加权年化%. cfs=[(date,amt)] 负=投入; end_date 时终值 end_val。
    时间太短(<min_days天)返回 None 避免失真。二分求解。"""
    if not cfs:
        return None
    allcf = list(cfs) + [(end_date, end_val)]
    t0 = min(c[0] for c in allcf)

    def yoff(ds):
        d = datetime.date(int(ds[0:4]), int(ds[5:7]), int(ds[8:10]))
        return (d - datetime.date(int(t0[0:4]), int(t0[5:7]), int(t0[8:10]))).days / 365.0

    dN = datetime.date(int(end_date[0:4]), int(end_date[5:7]), int(end_date[8:10]))
    d0 = datetime.date(int(t0[0:4]), int(t0[5:7]), int(t0[8:10]))
    if (dN - d0).days < min_days:
        return None
    amts = [(amt, yoff(ds)) for ds, amt in allcf]

    def npv(r):
        return sum(amt / ((1 + r) ** yrs) for amt, yrs in amts)

    lo, hi = -0.99, 10.0
    # 上一日解作初值先採一次, 能显著缩小区间(逐日序列变化很小)
    if prev_rate is not None and -0.98 < prev_rate < 9.9:
        if npv(prev_rate) > 0:
            lo = prev_rate
        else:
            hi = prev_rate
    # 收敛即停(NPV对r单调递减), 通常≲30次
    for _ in range(60):
        mid = (lo + hi) / 2
        v = npv(mid)
        if v > 0:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-7:
            break
    return (lo + hi) / 2 * 100


class _NavCursor:
    """有序净值序列的向前指针, O(1) 均摊取 <=date 的最新净值。"""

    def __init__(self, rows):
        self.rows = rows
        self.p = 0

    def at(self, dd):
        rows, p = self.rows, self.p
        while p + 1 < len(rows) and rows[p + 1][0] <= dd:
            p += 1
        self.p = p
        if p >= 0 and rows[p][0] <= dd:
            return rows[p][1]
        return None


def compute_daily():
    """整体组合: 每日 {d, mv, xirr, pos}。增量维护持仓, 曲线延伸到净值库最新日。"""
    flows = db.core_flows()
    recs = db.core_trades()
    nav = {c: db.nav_all(c) for c in CORE}
    all_dates = db.all_trade_dates()

    # 手动录入的持仓变化并入交易重放
    hold = db.holdings_list()
    for h in hold:
        try:
            hq = float(h["qty"] or 0)
            hp = float(h["price"] or 0)
        except (TypeError, ValueError):
            continue
        if hq == 0:
            continue
        est = hp if hp > 0 else 0.0
        if h["action"] == "BUY":
            recs.append((h["date"], h["code"], est, hq, -hq * est if est else -1.0))
        elif h["action"] == "SELL":
            recs.append((h["date"], h["code"], est, hq, hq * est if est else 1.0))
    recs.sort(key=lambda x: (x[0], x[1]))

    first_trade = recs[0][0] if recs else None
    dates = [d for d in all_dates if (not first_trade or d >= first_trade)]

    trade_by_date = defaultdict(list)
    for d, code, p, q, o in recs:
        trade_by_date[d].append((code, p, q, o))
    flow_by_date = defaultdict(list)
    for d, v in flows:
        flow_by_date[d].append(v)

    cursors = {c: _NavCursor(nav[c]) for c in CORE}
    pos = {c: 0.0 for c in CORE}
    flow_cf = []
    out = []
    prev_yr = None  # 上日解(小数), 作下一日二分初值
    for dd in dates:
        for code, p, q, o in trade_by_date.get(dd, []):
            if o < 0:
                pos[code] += q
            else:
                if p > 0 and q > 0:
                    pos[code] -= q
        flow_cf.extend((dd, v) for v in flow_by_date.get(dd, []))
        total = 0.0
        for code in CORE:
            if pos[code] == 0:
                continue
            nv = cursors[code].at(dd)
            if nv is not None:
                total += pos[code] * nv
        yr = xirr(flow_cf, dd, total, prev_rate=prev_yr) if total > 0 else None
        if yr is not None:
            prev_yr = yr / 100.0
        out.append({"d": dd, "mv": round(total, 2),
                    "xirr": round(yr, 2) if yr is not None else None,
                    "pos": {c: int(pos[c]) for c in CORE if pos[c] != 0}})
    return out


def compute_daily_cached():
    """compute_daily 的 30s TTL 缓存封装。"""
    with _cache["lock"]:
        if _cache["val"] is not None and time.time() - _cache["t"] < _CACHE_TTL:
            return _cache["val"]
    val = compute_daily()
    with _cache["lock"]:
        _cache["val"], _cache["t"] = val, time.time()
    return val


def compute_benchmark():
    """上证指数基准: 同现金流虚拟买入指数的 XIRR% + 累计涨跌%。"""
    flows = db.core_flows()
    if not flows:
        return {"ok": False, "error": "无现金流"}
    idx = db.index_all()
    if not idx:
        return {"ok": False, "error": "无指数数据"}
    idx_dates = [r[0] for r in idx]
    idx_close = [r[1] for r in idx]
    first_trade = min(d for d, _ in flows)
    base_i = next((i for i, d in enumerate(idx_dates) if d >= first_trade), 0)
    base_close = idx_close[base_i]

    def idx_at(fsrq):
        i = bisect.bisect_right(idx_dates, fsrq) - 1
        return idx_close[i] if i >= 0 else None

    flow_by_date = defaultdict(float)
    for d, v in flows:
        flow_by_date[d] += v

    shares = 0.0
    bench = []
    prev_yr = None
    cfs = []  # 增量维护已发生的现金流, 避免每日重建 O(N²)
    fi, nflows = 0, len(flows)
    sorted_flows = sorted(flows)
    for i in range(base_i, len(idx_dates)):
        dd, close = idx_dates[i], idx_close[i]
        while fi < nflows and sorted_flows[fi][0] <= dd:
            cfs.append(sorted_flows[fi]); fi += 1
        amt = flow_by_date.get(dd, 0.0)
        if amt != 0:
            shares -= amt / close
        mv = shares * close
        yr = xirr(cfs, dd, mv, min_days=30, prev_rate=prev_yr) if mv > 0 else None
        if yr is not None:
            prev_yr = yr / 100.0
        bench.append({"d": dd,
                      "xirr": round(yr, 2) if yr is not None else None,
                      "pct": round((close / base_close - 1) * 100, 2)})
    return {"ok": True, "index_name": INDEX_NAME, "bench_xirr": bench}


def compute_fund_daily(code):
    """单基金个人年化曲线(XIRR, 基于该基金现金流+期末市值)。"""
    flows, trades = db.fund_flows_trades(code)
    nav_rows = db.nav_all(code)
    if not flows:
        return {"ok": False, "error": "无交易记录"}
    first_trade = min(f[0] for f in flows)
    dates = [d for d, _ in nav_rows if d >= first_trade]

    trade_by_date = defaultdict(list)
    for d, p, q, o in trades:
        trade_by_date[d].append((p, q, o))
    flow_by_date = defaultdict(list)
    for d, v in flows:
        flow_by_date[d].append(v)

    cursor = _NavCursor(nav_rows)
    pos = 0.0
    flow_cf = []
    series = []
    for dd in dates:
        for p, q, o in trade_by_date.get(dd, []):
            if o < 0:
                pos += q
            else:
                if p > 0 and q > 0:
                    pos -= q
        flow_cf.extend((dd, v) for v in flow_by_date.get(dd, []))
        nv = cursor.at(dd)
        if nv is None:
            continue
        mv = pos * nv
        yr = xirr(flow_cf, dd, mv, min_days=15) if mv > 0 else None
        series.append({"d": dd, "mv": round(mv, 2), "pos": round(pos, 2),
                       "xirr": round(yr, 2) if yr is not None else None})
    return {"ok": True, "code": code, "name": FUNDS.get(code, code), "series": series}
