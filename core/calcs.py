# -*- coding: utf-8 -*-
"""计算层: XIRR、整体组合曲线、基准对比、单基金曲线。"""
import bisect
import datetime
import threading
import time
from collections import defaultdict

from . import db
from .config import CORE, FUNDS, INDEX_NAME

# compute_daily 结果短 TTL 缓存: 按基金选择集分桶,
# 净值每日只更新一次, 30s 缓存足够新鲜且免重复计算
_cache = {"lock": threading.Lock(), "map": {}}
_CACHE_TTL = 300.0  # 净值每日更新一次, 5分钟缓存足够新鲜


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


def _gran_key(d, g):
    """交易日 → 周期分组键: day/week/month/quarter/year。"""
    if g == "day":
        return d
    if g == "week":
        dt = datetime.date(int(d[0:4]), int(d[5:7]), int(d[8:10]))
        dt -= datetime.timedelta(days=dt.weekday())   # 对齐周一
        return "W" + dt.isoformat()
    y, m = d[0:4], int(d[5:7])
    if g == "month":
        return y + "-" + d[5:7]
    if g == "quarter":
        return "%sQ%d" % (y, (m - 1) // 3 + 1)
    return y


def _gran_rollup(daily, gran):
    """滚动累计: 取每周期最后一个点(累计XIRR语义与日线一致), 附周期标签。"""
    groups, order = {}, []
    for p in daily:
        k = _gran_key(p["d"], gran)
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(p)
    out = []
    for k in order:
        p = dict(groups[k][-1])
        p["label"] = k[1:] if k.startswith("W") else k
        out.append(p)
    return out


def _period_return_series(daily, flows, gran):
    """周期收益率: 该周期实际涨跌%(不折年, 市值法)。
    收益 = 期末市值 - 期初市值 + 周期内现金流之和(负=投入);
    收益率 = 收益 / 期初市值。日粒度=单日收益率。首点无期初基准返回 None。"""
    flow_by_date = defaultdict(float)
    for dd, v in flows:
        flow_by_date[dd] += v   # 负=买入投入, 正=卖出回流
    if gran == "day":
        out = []
        prev_mv = None
        for p in daily:
            ret = None
            gain = None
            if prev_mv and prev_mv > 0:
                gain = p["mv"] - prev_mv + flow_by_date.get(p["d"], 0.0)
                ret = round(gain / prev_mv * 100, 2)
            out.append({"d": p["d"], "label": p["d"], "ret": ret, "gain": None if gain is None else round(gain, 2)})
            prev_mv = p["mv"]
        return out
    groups, order = {}, []
    for p in daily:
        k = _gran_key(p["d"], gran)
        if k not in groups:
            groups[k] = []
            order.append(k)
        groups[k].append(p)
    out = []
    prev_mv = 0.0
    for k in order:
        pts = groups[k]
        d1, dk = pts[0]["d"], pts[-1]["d"]
        mv_end = pts[-1]["mv"]
        sflow = sum(v for dd, v in flow_by_date.items() if d1 <= dd <= dk)
        ret = None
        gain = None
        if prev_mv > 0:
            gain = mv_end - prev_mv + sflow
            ret = round(gain / prev_mv * 100, 2)
        out.append({"d": dk, "label": k[1:] if k.startswith("W") else k, "ret": ret, "gain": None if gain is None else round(gain, 2)})
        prev_mv = mv_end
    return out


def compute_period_return(codes=None, gran="day"):
    """整体组合+上证基准的周期收益率。"""
    daily = compute_daily(codes)            # 日粒度累计序列
    core = [c for c in (codes or CORE) if c in CORE] or list(CORE)
    flows = []
    for c in core:
        f, _ = db.fund_flows_trades(c)
        flows.extend(f)
    flows.sort()
    series = _period_return_series(daily, flows, gran)
    bench = []
    idx = db.index_all()
    if idx:
        bd = _period_return_series(
            [{"d": d, "mv": c} for d, c in idx], [], gran)
        bench = bd
    return {"series": series, "bench": bench}


def compute_daily(codes=None, gran="day"):
    """整体组合: 每日 {d, mv, xirr, pos}。增量维护持仓, 曲线延伸到净值库最新日。
    codes: 参与计算的基金代码列表, None=全部。"""
    core = [c for c in (codes or CORE) if c in CORE]
    if not core:
        core = list(CORE)
    # 只保留所选基金的现金流与交易(逐基金重取, 含手动录入合并)
    flows = []
    for c in core:
        f, _ = db.fund_flows_trades(c)
        flows.extend(f)
    flows.sort()
    recs_all = db.core_trades()
    recs = [(d, c, p, q, o) for d, c, p, q, o in recs_all if c in core]
    nav = {c: db.nav_all(c) for c in core}
    price = {c: db.price_all(c) for c in core}
    all_dates = db.all_trade_dates()

    # 手动录入的持仓变化并入交易重放
    hold = db.holdings_list()
    for h in hold:
        if h["code"] not in core:   # 未选中的基金不参与重放
            continue
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

    cursors = {c: _NavCursor(nav[c]) for c in core}
    pcursors = {c: _NavCursor(price[c]) for c in core}
    pos = {c: 0.0 for c in core}
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
        for code in core:
            if pos[code] == 0:
                continue
            # 估值优先用交易价格(最新成交价), 无价格记录的日期回退净值
            pv = pcursors[code].at(dd)
            nv = pv if pv is not None else cursors[code].at(dd)
            if nv is not None:
                total += pos[code] * nv
        yr = xirr(flow_cf, dd, total, prev_rate=prev_yr) if total > 0 else None
        if yr is not None:
            prev_yr = yr / 100.0
        out.append({"d": dd, "mv": round(total, 2),
                    "xirr": round(yr, 2) if yr is not None else None,
                    "pos": {c: int(pos[c]) for c in core if pos[c] != 0}})
    if gran and gran != "day":
        return _gran_rollup(out, gran)   # 滚动累计: 取周期末点, 语义与日线一致
    return out


def cache_clear_all():
    """清空全部 TTL 缓存(录入持仓/新增标的后调用)。"""
    with _cache["lock"]:
        _cache["map"].clear()


def compute_daily_cached(codes=None, gran="day"):
    """compute_daily 的 30s TTL 缓存封装, 按基金集合+周期分桶。"""
    key = (frozenset(codes) if codes else frozenset(CORE), gran)
    now = time.time()
    with _cache["lock"]:
        ent = _cache["map"].get(key)
        if ent is not None and now - ent[1] < _CACHE_TTL:
            return ent[0]
    val = compute_daily(codes, gran)
    with _cache["lock"]:
        _cache["map"][key] = (val, time.time())
    return val


def compute_benchmark(gran="day"):
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
        bench.append({"d": dd, "mv": round(mv, 2),
                      "xirr": round(yr, 2) if yr is not None else None,
                      "pct": round((close / base_close - 1) * 100, 2)})
    if gran and gran != "day":
        pct_by_date = {p["d"]: p.get("pct") for p in bench}
        agg = _gran_rollup(bench, gran)
        for p in agg:
            p["pct"] = pct_by_date.get(p["d"])
        bench = agg
    return {"ok": True, "index_name": INDEX_NAME, "bench_xirr": bench}


def benchmark_cached(gran="day"):
    """compute_benchmark 的 TTL 缓存(按周期分桶)。"""
    with _cache["lock"]:
        ent = _cache["map"].get(("__bench__", gran))
        if ent is not None and time.time() - ent[1] < _CACHE_TTL:
            return ent[0]
    val = compute_benchmark(gran)
    with _cache["lock"]:
        _cache["map"][("__bench__", gran)] = (val, time.time())
    return val


def compute_fund_daily(code, gran="day"):
    """单基金个人年化曲线(XIRR, 基于该基金现金流+期末市值)。"""
    flows, trades = db.fund_flows_trades(code)
    nav_rows = db.nav_all(code)
    price_rows = db.price_all(code)
    if not flows:
        return {"ok": False, "error": "无交易记录"}
    first_trade = min(f[0] for f in flows)
    # 日期序列取净值与成交价库的并集(QDII净值滞后时仍能用最新成交价出点)
    dates = [d for d in db.all_trade_dates() if d >= first_trade]

    trade_by_date = defaultdict(list)
    for d, p, q, o in trades:
        trade_by_date[d].append((p, q, o))
    flow_by_date = defaultdict(list)
    for d, v in flows:
        flow_by_date[d].append(v)

    cursor = _NavCursor(nav_rows)
    pcursor = _NavCursor(price_rows)
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
        nv = pcursor.at(dd) if (price_rows and price_rows[0][0] <= dd) else None
        if nv is None:
            nv = cursor.at(dd)
        if nv is None:
            continue
        # 估值优先用交易价格, 无价格记录的日期回退净值
        mv = pos * nv
        yr = xirr(flow_cf, dd, mv, min_days=15) if mv > 0 else None
        series.append({"d": dd, "mv": round(mv, 2), "pos": round(pos, 2),
                       "xirr": round(yr, 2) if yr is not None else None})
    if gran and gran != "day":
        series = _gran_rollup(series, gran)
    return {"ok": True, "code": code, "name": FUNDS.get(code, code), "series": series}
