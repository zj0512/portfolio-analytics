# -*- coding: utf-8 -*-
"""比例调整模拟: 用户设定各基金目标权重, 按每日再平衡回放历史净值,
输出模拟组合的市值与年化曲线, 并与实际组合对比。

设计说明: 全量净值重放计算量随 交易日×基金数 增长, 放后端 Python 做
(复用 db/xirr 基础设施), 前端只负责输入与画图。
演进方向: 数据量再大时可加结果缓存/增量计算。
"""
import bisect
import datetime

from . import db
from .config import FUNDS

START_CAP = 100000.0  # 虚拟本金 10 万, 只看比率


def _norm_weights(alloc):
    weights = {}
    for code, w in (alloc or {}).items():
        if code not in FUNDS:
            return None, "未知基金代码: %s" % code
        try:
            wv = float(w)
        except (TypeError, ValueError):
            return None, "权重非法: %s" % code
        if wv > 0:
            weights[code] = wv
    if not weights:
        return None, "至少需要一个正权重基金"
    total = sum(weights.values())
    return {c: w / total for c, w in weights.items()}, None


def _ann_pct(base_mv, mv, days):
    """复利年化%, 间隔太短(<15天)返回 None。"""
    if base_mv <= 0 or mv <= 0 or days < 15:
        return None
    return round(((mv / base_mv) ** (365.0 / days) - 1) * 100, 2)


def compute_sim(alloc):
    """alloc: {code: weight}, 权重自动归一。每日收盘再平衡回放。
    返回 {ok, series:[{d,mv,xirr}], actual:[{d,pct_xirr}], summary}。"""
    weights, err = _norm_weights(alloc)
    if err:
        return {"ok": False, "error": err}

    navs = {c: db.nav_all(c) for c in weights}
    for c, rows in navs.items():
        if not rows:
            return {"ok": False, "error": "基金 %s 无净值数据" % c}
        if len(rows) < 2:
            return {"ok": False, "error": "基金 %s 净值数据不足" % c}

    # 起点 = 最晚开始有净值的基金的首日之后, 保证所有基金都能估值
    start = max(rows[0][0] for rows in navs.values())
    dates = [d for d in db.all_trade_dates() if d >= start]
    if len(dates) < 2:
        return {"ok": False, "error": "共同交易日不足"}

    # 每基金预建 (dates, navs) 平行数组 + 指针, O(N) 回放
    keys = {c: [r[0] for r in navs[c]] for c in weights}
    vals = {c: [r[1] for r in navs[c]] for c in weights}
    ptr = {c: bisect.bisect_right(keys[c], dates[0]) - 1 for c in weights}
    for c in weights:
        if ptr[c] < 0:
            ptr[c] = 0

    base_date = datetime.date.fromisoformat(dates[0])
    prev_vals = {c: START_CAP * w for c, w in weights.items()}
    prev_nav = {c: vals[c][ptr[c]] for c in weights}
    series = []
    for dd in dates:
        dd_date = datetime.date.fromisoformat(dd)
        total = 0.0
        for c in weights:
            k = keys[c]
            while ptr[c] + 1 < len(k) and k[ptr[c] + 1] <= dd:
                ptr[c] += 1
            nv = vals[c][ptr[c]]
            # 用最近两次净值的涨幅近似区间收益(停牌日沿用)
            prev_vals[c] *= nv / prev_nav[c] if prev_nav[c] > 0 else 1.0
            prev_nav[c] = nv
            total += prev_vals[c]
        # 每日再平衡回目标权重
        prev_vals = {c: total * w for c, w in weights.items()}
        days = (dd_date - base_date).days
        series.append({"d": dd, "mv": round(total, 2),
                       "xirr": _ann_pct(START_CAP, total, days)})

    # 实际组合同期对比(以起点后首个正市值为基)
    actual_all = db_act_daily()
    actual = [p for p in actual_all if p["d"] >= dates[0]]
    a0 = next((p["mv"] for p in actual if p["mv"] > 0), None)
    actual_out = []
    last_act = None
    for p in actual:
        v = None
        if a0 and p["mv"] > 0:
            days = (datetime.date.fromisoformat(p["d"]) - base_date).days
            v = _ann_pct(a0, p["mv"], days)
        actual_out.append({"d": p["d"], "pct_xirr": v})
        if p["mv"] > 0:
            last_act = v

    last = series[-1] if series else {}
    summary = {
        "sim_last_xirr": last.get("xirr"),
        "sim_last_mv": last.get("mv"),
        "actual_last_xirr": last_act,
        "weights": {c: round(w * 100, 1) for c, w in weights.items()},
        "start": dates[0], "end": dates[-1] if dates else None,
    }
    return {"ok": True, "series": series, "actual": actual_out, "summary": summary}


def db_act_daily():
    from .calcs import compute_daily
    return compute_daily()
