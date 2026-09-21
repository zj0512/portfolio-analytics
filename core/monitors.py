# -*- coding: utf-8 -*-
"""投资分析监控: 注册表驱动的监控项体系。
每个监控项 = 注册条目(取数函数 + 评估函数 + 默认参数)。
新增指标只需 @register 一条, 定时任务/API/页面自动收录。
- 快照表 monitor_snapshot 保留历史(每日一条), 供迷你走势与"持续性"判断
- 配置表 monitor_config 支持启停/阈值调整/手动取值/自定义手动指标
"""
import json
import re
import urllib.request
from datetime import datetime, timedelta
from . import db as dbm
from .config import DB, DZ_DB
from .dates import today

# ---------- 注册表 ----------

MONITORS = {}          # id -> 定义dict
ORDER = []             # 渲染顺序


def register(mid, name, group, unit, fetch, evaluate, defaults=None, desc=""):
    MONITORS[mid] = {
        "id": mid, "name": name, "group": group, "unit": unit,
        "fetch": fetch, "evaluate": evaluate,
        "defaults": defaults or {}, "desc": desc, "custom": False,
    }
    ORDER.append(mid)


# ---------- 库表 ----------

def _conn():
    import sqlite3
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def ensure_tables():
    conn = _conn()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS monitor_snapshot ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " date TEXT, monitor_id TEXT, value REAL, status TEXT, note TEXT,"
        " UNIQUE(date, monitor_id))")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS monitor_config ("
        " monitor_id TEXT PRIMARY KEY, enabled INTEGER DEFAULT 1,"
        " params_json TEXT DEFAULT '{}')")
    conn.commit()
    conn.close()


def get_config(mid):
    conn = _conn()
    r = conn.execute("SELECT enabled, params_json FROM monitor_config WHERE monitor_id=?", (mid,)).fetchone()
    conn.close()
    if not r:
        return True, {}
    try:
        params = json.loads(r[1] or "{}")
    except (ValueError, TypeError):
        params = {}
    return bool(r[0]), params


def set_config(mid, enabled=None, params=None, merge=True):
    cur_enabled, cur_params = get_config(mid)
    if enabled is not None:
        cur_enabled = bool(enabled)
    if params is not None:
        if merge and isinstance(params, dict):
            cur_params.update(params)
        else:
            cur_params = params if isinstance(params, dict) else {}
    conn = _conn()
    conn.execute(
        "INSERT OR REPLACE INTO monitor_config (monitor_id, enabled, params_json) VALUES (?,?,?)",
        (mid, 1 if cur_enabled else 0, json.dumps(cur_params, ensure_ascii=False)))
    conn.commit()
    conn.close()


def save_snapshot(date, mid, value, status, note):
    conn = _conn()
    conn.execute(
        "INSERT OR REPLACE INTO monitor_snapshot (date, monitor_id, value, status, note)"
        " VALUES (?,?,?,?,?)", (date, mid, value, status, note))
    conn.commit()
    conn.close()


def snapshot_history(mid, days=30):
    conn = _conn()
    rows = conn.execute(
        "SELECT date, value FROM monitor_snapshot WHERE monitor_id=?"
        " AND value IS NOT NULL ORDER BY date DESC LIMIT ?", (mid, days)).fetchall()
    conn.close()
    return [{"d": d, "v": v} for d, v in reversed(rows)]


def latest_snapshot(mid):
    conn = _conn()
    r = conn.execute(
        "SELECT date, value, status, note FROM monitor_snapshot WHERE monitor_id=?"
        " AND status!='nodata' ORDER BY date DESC LIMIT 1", (mid,)).fetchone()
    conn.close()
    if not r:
        return None
    return {"date": r[0], "value": r[1], "status": r[2], "note": r[3]}


# ---------- 本地取数辅助 ----------

def _last_price(code):
    rows = dbm.price_all(code)
    if rows:
        return float(rows[-1][1]), rows[-1][0]
    navs = dbm.nav_all(code)
    if navs:
        return float(navs[-1][1]), navs[-1][0]
    return None, None


def _last_nav(code):
    navs = dbm.nav_all(code)
    return (float(navs[-1][1]), navs[-1][0]) if navs else (None, None)


def _current_pos(code):
    """当前持仓(份), 复用 positions 的口径: 曲线末日持仓 + 之后录入。"""
    from . import calcs
    daily = calcs.compute_daily_cached()
    if not daily:
        return 0.0
    last = daily[-1]
    pos = (last.get("pos") or {}).get(code, 0)
    for hd, haction, hqty in dbm.holdings_after(code, last["d"]):
        try:
            hq = float(hqty) or 0.0
        except (TypeError, ValueError):
            continue
        pos = pos + hq if haction == "BUY" else pos - hq
    return pos


# ---------- 外部取数 ----------

def _http(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore")


def fetch_us10y():
    """美债10Y(%): 多源尝试, 全失败返回 None(可由手动值兜底)。"""
    try:
        t = _http("https://qt.gtimg.cn/q=us10y", timeout=3)
        f = t.split("~")
        if len(f) > 5 and f[3] not in ("", "0.00", None):
            v = float(f[3])
            if 1.0 < v < 15.0:            # 合理性校验, 垃圾值跳过
                return v
    except Exception:
        pass
    # Yahoo ^TNX (10Y收益率×10), 走本地 socks5 代理
    try:
        ph = urllib.request.ProxyHandler(
            {"http": "socks5h://127.0.0.1:10808", "https": "socks5h://127.0.0.1:10808"})
        opener = urllib.request.build_opener(ph)
        req = urllib.request.Request(
            "https://query1.finance.yahoo.com/v8/finance/chart/%5ETNX?range=5d&interval=1d",
            headers={"User-Agent": "Mozilla/5.0"})
        d = json.loads(opener.open(req, timeout=8).read().decode("utf-8", "ignore"))
        closes = [c for c in d["chart"]["result"][0]["indicators"]["quote"][0]["close"] if c]
        if closes:
            return round(closes[-1], 3)   # ^TNX 直接就是收益率%(如4.998=4.998%)
    except Exception:
        pass
    try:
        t = _http("https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10", timeout=4)
        rows = [r for r in t.strip().split("\n")[1:] if r.split(",")[1] not in (".", "")]
        if rows:
            return float(rows[-1].split(",")[1])
    except Exception:
        pass
    return None


def fetch_a_amount():
    """沪深两市合计成交额(亿元): qt 实时行情成交额字段(收盘后为全天值, 万元)。"""
    try:
        t = _http("https://qt.gtimg.cn/q=sh000001,sz399001", timeout=4)
        total = 0.0
        ok = 0
        for part in t.split(";"):
            f = part.split("~")
            if len(f) < 40:
                continue
            try:
                amt_wan = float(f[37])          # 成交额(万元)
                if amt_wan > 0:
                    total += amt_wan / 10000.0  # -> 亿元
                    ok += 1
            except (ValueError, IndexError):
                continue
        if ok == 2 and total > 1000:            # 两市都取到且量级合理
            return round(total, 0)
    except Exception:
        pass
    return None


# ---------- 评估函数: 返回 (status, note) ----------

def backfill_us10y(days=120):
    """从 Yahoo ^TNX 回填近 N 日10Y收盘到快照表(首次使用/补缺口)。"""
    try:
        ph = urllib.request.ProxyHandler(
            {"http": "socks5h://127.0.0.1:10808", "https": "socks5h://127.0.0.1:10808"})
        opener = urllib.request.build_opener(ph)
        req = urllib.request.Request(
            "https://query1.finance.yahoo.com/v8/finance/chart/%5ETNX?range=6mo&interval=1d",
            headers={"User-Agent": "Mozilla/5.0"})
        d = json.loads(opener.open(req, timeout=10).read().decode("utf-8", "ignore"))
        r = d["chart"]["result"][0]
        ts = r["timestamp"]
        closes = r["indicators"]["quote"][0]["close"]
        import datetime
        n = 0
        for x, c in zip(ts, closes):
            if not c:
                continue
            dt = datetime.datetime.fromtimestamp(x)
            if dt.weekday() >= 5:      # 只保留交易日
                continue
            save_snapshot(dt.strftime("%Y-%m-%d"), "US10Y", round(c, 3), "backfill", "历史回填")
            n += 1
        return n
    except Exception:
        return 0


def _ev_us10y(v, p):
    """动态阈值: 近60日均值±band(默认30bp), 交易边际变化而非绝对值。"""
    band = float(p.get("band", 0.30))
    win = int(p.get("window", 60))
    hist = snapshot_history("US10Y", win)
    vals = [h["v"] for h in hist if h["v"] is not None]
    if vals and vals[-1] != v:            # 今日值尚未落库时补造入均值
        vals.append(v)
    if len(vals) < max(10, win // 3):     # 样本不足, 退回固定阈值
        hi, lo = float(p.get("warn", 5.2)), float(p.get("good", 4.8))
        if v >= hi:
            return "act_warn", "%.2f%% ≥ 固定阈值%.2f%% 红线A：加快减仓（动态样本不足%d/%d日）" % (v, hi, len(vals), win)
        if v < lo:
            return "good", "%.2f%% < 固定阈值%.2f%%：利率明显回落，暂缓减仓并复盘（动态样本不足）" % (v, lo)
        return "neutral", "%.2f%% 中性：按预设价位分批执行（动态样本积累中%d/%d日）" % (v, len(vals), win)
    mean = sum(vals[-win:]) / len(vals[-win:])
    hi, lo = mean + band, mean - band
    if v >= hi:
        return "act_warn", "%.2f%% ≥ 60日均值%.2f%%+%dbp 红线A：利率加速上行，加快减仓不等反弹价" % (v, mean, int(band * 100))
    if v < lo:
        return "good", "%.2f%% < 60日均值%.2f%%-%dbp：市场定价转向降息，暂缓减仓并重新复盘" % (v, mean, int(band * 100))
    return "neutral", "%.2f%% 位于均值%.2f%%±%dbp内：反弹属修复行情，按计划分批减仓" % (v, mean, int(band * 100))


def _ev_a_amount(v, p):
    # 与20日均值比较的相对量; 历史不足时提示
    hist = snapshot_history("A_AMOUNT", 20)
    vals = [h["v"] for h in hist if h["v"]]
    if len(vals) < 5:
        return "neutral", "%.0f亿：历史样本积累中(%d/20日)，暂无法判断放量性质" % (v, len(vals))
    avg = sum(vals) / len(vals)
    ratio = v / avg if avg else 0
    if ratio >= 1.3:
        return "good", "%.0f亿 较20日均+%.0f%%：若连续2日且资金入科创/创业板，视为增量入场" % (v, (ratio - 1) * 100)
    if ratio < 0.85:
        return "neutral", "%.0f亿 较20日均-%.0f%%：缩量，反弹乏力，启用兜底防守线" % (v, (1 - ratio) * 100)
    return "neutral", "%.0f亿 较20日均%+.0f%%：常态存量博弈" % (v, (ratio - 1) * 100)


def _premium(code, name, warn_p, good_p):
    def fetch():
        p, pd = _last_price(code)
        n, nd = _last_nav(code)
        if p and n:
            return round((p / n - 1) * 100, 1)
        return None

    def ev(v, prm):
        w = float(prm.get("warn", warn_p))
        g = float(prm.get("good", good_p))
        if v >= w:
            return "warn", "溢价%.1f%% ≥ %.0f%%：只卖不买，溢价预支收益" % (v, w)
        if v <= g:
            return "good", "溢价%.1f%% ≤ %.0f%%：恢复正常，可重新评估回补" % (v, g)
        return "neutral", "溢价%.1f%%：中性区间" % v
    return fetch, ev


def _ev_159682(v, p):
    sell_hi, sell_lo, stop = float(p.get("sell_hi", 1.65)), float(p.get("sell_lo", 1.60)), float(p.get("stop", 1.48))
    if v >= sell_lo:
        return "act_good", "%.3f 进入减仓区[%.2f,%.2f]：分批减5-8万份" % (v, sell_lo, sell_hi)
    if v <= stop:
        return "act_warn", "%.3f 跌破防守线%.2f：执行防守减仓" % (v, stop)
    return "neutral", "%.3f 区间内持有，等待反弹减仓区或防守线" % v


def _ev_159516(v, p):
    tp, stop = float(p.get("take", 0.75)), float(p.get("stop", 0.605))
    if v >= tp:
        return "act_good", "%.3f ≥ %.2f：反弹兑现区，分批止盈" % (v, tp)
    if v <= stop:
        return "act_warn", "%.3f 跌破%.3f前低：无条件清仓离场" % (v, stop)
    return "neutral", "%.3f 区间内持有：反弹至%.2f兑现，跌破%.3f离场" % (v, tp, stop)


def _ev_511090(v, p):
    target, floor = float(p.get("target", 119.5)), float(p.get("floor", 118.0))
    if v >= target:
        return "act_good", "%.3f ≥ %.2f：机会卖点，执行减半" % (v, target)
    if v <= floor:
        return "act_warn", "%.3f ≤ %.2f兜底：到位即减半，禁止死等" % (v, floor)
    return "neutral", "%.3f 观察区(%.1f~%.1f)，财政信号出现则无条件减" % (v, floor, target)


def _ev_gold_pos(v, p):
    cap = float(p.get("cap", 15.0))
    if v > cap:
        return "warn", "黄金市值%.1f万 > 目标%.0f万：继续执行降仓计划" % (v, cap)
    return "neutral", "黄金市值%.1f万 ≤ 目标%.0f万：底仓达标，持有" % (v, cap)


def _fetch_gold_mv():
    mv = 0.0
    for code in ("518880", "159937"):
        p, _ = _last_price(code)
        if p:
            mv += p * _current_pos(code)
    return round(mv / 10000.0, 1) if mv else None


# ---------- 注册监控项 ----------

register("US10Y", "美债10年期收益率", "宏观", "%", fetch_us10y, _ev_us10y,
         {"band": 0.30, "window": 60, "warn": 5.2, "good": 4.8},
         "动态阈值：≥60日均值+30bp红线A加快减仓；均值±30bp内按计划；<均值-30bp暂缓减仓。每日收盘采样")
register("A_AMOUNT", "沪深两市成交额", "宏观", "亿", fetch_a_amount, _ev_a_amount,
         {"ratio": 1.3},
         "较20日均值放大≥30%且持续2日=增量；单日脉冲=一日游；持续缩量=反弹乏力")
_p1, _e1 = _premium("513100", "纳指ETF溢价率", 8, 3)
register("PREM_513100", "纳指ETF溢价率", "持仓信号", "%", _p1, _e1,
         {"warn": 8, "good": 3}, "QDII溢价：>8%只卖不买；<3%恢复正常可评估回补")
_p2, _e2 = _premium("513650", "标普ETF溢价率", 6, 2)
register("PREM_513650", "标普ETF溢价率", "持仓信号", "%", _p2, _e2,
         {"warn": 6, "good": 2}, "同纳指溢价逻辑，阈值更紧")
register("P_159682", "创业五零关键位", "持仓信号", "元",
         lambda: _last_price("159682")[0], _ev_159682,
         {"sell_hi": 1.65, "sell_lo": 1.60, "stop": 1.48},
         "反弹至1.60-1.65减5-8万份；跌破1.48防守减仓")
register("P_159516", "半导设备关键位", "持仓信号", "元",
         lambda: _last_price("159516")[0], _ev_159516,
         {"take": 0.75, "stop": 0.605}, "反弹0.75+兑现；跌破0.605前低无条件清仓")
register("P_511090", "30年国债关键位", "持仓信号", "元",
         lambda: _last_price("511090")[0], _ev_511090,
         {"target": 119.5, "floor": 118.0},
         "≥119.5机会卖点减半；≤118.0兜底减半；财政/地产强刺激=无条件触发")
register("GOLD_MV", "黄金合计仓位", "持仓信号", "万元",
         _fetch_gold_mv, _ev_gold_pos, {"cap": 15.0},
         "两只黄金ETF合计市值，目标底仓12-15万，超出继续降仓")


# ---------- 自定义手动指标 ----------

def _manual_fetch(mid):
    def f():
        _, params = get_config(mid)
        mv, mdate = params.get("manual"), params.get("manual_date", "")
        if mv is None:
            return None
        # 手动值3天内有效
        try:
            if mdate and (today() - datetime.strptime(mdate, "%Y-%m-%d").date()).days > 3:
                return None
        except Exception:
            return None
        return float(mv)
    return f


def _manual_ev(mid, name):
    def ev(v, p):
        op, warn = p.get("op", "gt"), p.get("warn_at")
        if warn is None:
            return "neutral", "%s = %s" % (name, v)
        w = float(warn)
        hit = v > w if op == "gt" else (v < w if op == "lt" else v == w)
        return ("act_warn" if p.get("act", "warn") == "act" else "warn",
                "%s = %s %s %s：已触发" % (name, v, {"gt": ">", "lt": "<", "eq": "="}[op], w)) if hit else \
               ("neutral", "%s = %s，未触发(%s%s)" % (name, v, {">": "<", "<": ">", "=": "≠"}[{"gt": ">", "lt": "<", "eq": "="}[op]], w))
    return ev


def add_manual_monitor(mid, name, group="手动", unit="", warn_at=None, op="gt", act="warn"):
    if mid in MONITORS:
        return False
    register(mid, name, group or "手动", unit or "",
             _manual_fetch(mid), _manual_ev(mid, name),
             {"warn_at": warn_at, "op": op, "act": act}, "自定义手动指标：页面录入当日值")
    MONITORS[mid]["custom"] = True
    set_config(mid, params={"warn_at": warn_at, "op": op, "act": act})
    return True


def remove_monitor(mid):
    m = MONITORS.get(mid)
    if not m or not m.get("custom"):
        return False       # 内置指标只可停用不可删
    MONITORS.pop(mid)
    ORDER.remove(mid)
    conn = _conn()
    conn.execute("DELETE FROM monitor_config WHERE monitor_id=?", (mid,))
    conn.execute("DELETE FROM monitor_snapshot WHERE monitor_id=?", (mid,))
    conn.commit()
    conn.close()
    return True


# ---------- 每日评估主流程 ----------

def run_all(date=None):
    """逐项取数+评估+落库, 返回结果列表。"""
    ensure_tables()
    d = date or today()
    out = []
    for mid in list(ORDER):
        m = MONITORS[mid]
        enabled, params = get_config(mid)
        if not enabled:
            out.append({"id": mid, "name": m["name"], "group": m["group"],
                        "status": "off", "note": "已停用", "value": None})
            continue
        value, status, note, src = None, "nodata", "数据源未取到", "fetch"
        try:
            value = m["fetch"]()
        except Exception:
            value = None
        if value is None:
            # 手动值兜底
            mv = params.get("manual")
            mdate = params.get("manual_date", "")
            try:
                fresh = mv is not None and mdate and (datetime.strptime(d, "%Y-%m-%d") - datetime.strptime(mdate, "%Y-%m-%d")).days >= 0
            except Exception:
                fresh = False
            if fresh:
                value, src = float(mv), "manual"
        if value is not None:
            try:
                status, note = m["evaluate"](value, params)
            except Exception:
                status, note = "neutral", "评估异常，原值保留"
        save_snapshot(d, mid, value, status, note if value is not None else "数据源未取到(可手动录入)")
        out.append({"id": mid, "name": m["name"], "group": m["group"], "unit": m["unit"],
                    "value": value, "status": status, "note": note, "date": d,
                    "desc": m["desc"], "src": src if value is not None else None})
    return out


def load_today(date=None, allow_fetch=False):
    """优先返回当日已落库快照; 无则 allow_fetch=True 时现算, 否则空。"""
    d = date or today()
    conn = _conn()
    rows = conn.execute(
        "SELECT monitor_id, value, status, note FROM monitor_snapshot WHERE date=?", (d,)).fetchall()
    conn.close()
    saved = {r[0]: r for r in rows}
    if rows and not allow_fetch:
        out = []
        hit_ids = set()
        for mid in list(ORDER):
            m = MONITORS[mid]
            r = saved.get(mid)
            hit_ids.add(mid)
            if r:
                out.append({"id": mid, "name": m["name"], "group": m["group"],
                            "unit": m["unit"], "value": r[1], "status": r[2],
                            "note": r[3], "date": d, "desc": m["desc"],
                            "src": "manual" if m["custom"] else "fetch"})
            else:
                out.append({"id": mid, "name": m["name"], "group": m["group"],
                            "status": "off", "note": "已停用", "value": None})
        return out
    return run_all(d)


def summary(results):
    order = {"act_warn": 0, "act_good": 1, "warn": 2, "good": 3, "neutral": 4, "nodata": 5, "off": 6}
    worst = "neutral"
    cnt = {}
    for r in results:
        s = r.get("status", "nodata")
        cnt[s] = cnt.get(s, 0) + 1
        if order.get(s, 9) < order.get(worst, 9):
            worst = s
    return {"worst": worst, "counts": cnt}
