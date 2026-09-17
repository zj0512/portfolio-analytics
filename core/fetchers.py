# -*- coding: utf-8 -*-
"""数据抓取与增量同步:
- 基金净值: eastmoney F10 历史净值(可指定起始日期, 用于停机补数)
- 上证指数: eastmoney K线接口
启动时调用 sync_on_startup() 补齐停机期间缺口。
"""
import json
import logging
import sqlite3
import time
import urllib.request

from . import db
from .config import DB, FUNDS, INDEX_CODE, INDEX_NAME

log = logging.getLogger("portfolio.sync")

EM_F10 = "http://api.fund.eastmoney.com/f10/lsjz?fundCode=%s&pageIndex=%s&pageSize=%s&startDate=%s&endDate=%s"
EM_KLINE = ("http://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.000001"
            "&fields1=f1,f2,f3&fields2=f51,f53&klt=101&fqt=0&beg=%s&end=20500101")
EM_KLINE_SEC = ("http://push2his.eastmoney.com/api/qt/stock/kline/get?secid=%s"
                "&fields1=f1,f2,f3&fields2=f51,f53&klt=101&fqt=0&beg=%s&end=20500101")
TX_QUOTE = "http://qt.gtimg.cn/q=sh000001"
TX_ETF_QUOTE = "http://qt.gtimg.cn/q=%s"


def _get_json(url, referer, retries=3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"Referer": referer})
            with urllib.request.urlopen(req, timeout=12) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


def fetch_fund_nav(code, start_date=None, max_pages=20):
    """抓某基金净值。start_date 为 YYYY-MM-DD, 从该日期(含)起增量;
    不传则只取最近几页。返回 [(fsrq, dwjz), ...]。"""
    rows = []
    page = 1
    while page <= max_pages:
        d = _get_json(
            EM_F10 % (code, page, 40, start_date or "", ""),
            "http://fundf10.eastmoney.com/")
        lst = d.get("Data", {}).get("LSJZList", [])
        if not lst:
            break
        done = False
        for r in lst:
            fsrq = r.get("FSRQ", "")
            if not fsrq:
                continue
            if start_date and fsrq < start_date:
                done = True
                break
            try:
                rows.append((fsrq, float(r["DWJZ"])))
            except (KeyError, ValueError, TypeError):
                continue
        if done or len(lst) < 40:
            break
        page += 1
        time.sleep(0.2)
    return rows


def fetch_index(start_date=None):
    """抓上证指数日线收盘。主源: 东财K线; 失败降级: 腾讯实时行情(仅最新一日)。
    返回 [(fsrq, close), ...]。"""
    beg = start_date.replace("-", "") if start_date else "19900101"
    try:
        d = _get_json(EM_KLINE % beg, "http://quote.eastmoney.com/")
        out = []
        for line in (d.get("data") or {}).get("klines", []):
            parts = line.split(",")
            try:
                out.append((parts[0], float(parts[1])))
            except (IndexError, ValueError):
                continue
        if out:
            return out
    except Exception as e:
        log.warning("东财K线接口失败(%s), 降级腾讯实时行情", e)
    # 降级: 腾讯实时行情, 只能拿到最新一日收盘
    req = urllib.request.Request(TX_QUOTE, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        txt = r.read().decode("gbk")
    f = txt.split("~")
    # f[30]=时间yyyymmddHHMMSS, f[3]=最新价(收盘/当前), f[4]=昨收
    ts = f[30][:8]
    if len(ts) == 8 and ts.isdigit():
        dd = "%s-%s-%s" % (ts[0:4], ts[4:6], ts[6:8])
        try:
            return [(dd, float(f[3]))]
        except (IndexError, ValueError):
            pass
    return []


def _db_latest(conn, table, code_col, code):
    return conn.execute(
        "SELECT MAX(fsrq) FROM %s WHERE %s=?" % (table, code_col), (code,)).fetchone()[0]


def sync_fund(code, start_date=None):
    """同步单只基金净值(增量), 返回新增条数。"""
    conn = sqlite3.connect(DB)
    latest = conn.execute(
        "SELECT MAX(fsrq) FROM fund_nav WHERE fund_code=?", (code,)).fetchone()[0]
    conn.close()
    # 增量起点: 库里最后一条的次日; 若指定 start_date 则取更早者(用于全量补)
    if start_date is None:
        start_date = latest  # 从最后一条开始重抓(含当日, 幂等覆盖)
    elif latest:
        start_date = min(start_date, latest)
    rows = fetch_fund_nav(code, start_date=start_date)
    if rows:
        conn = sqlite3.connect(DB)
        conn.executemany(
            "INSERT OR REPLACE INTO fund_nav (fund_code, fsrq, dwjz) VALUES (?,?,?)",
            [(code, d, v) for d, v in rows])
        conn.commit()
        conn.close()
    return len(rows)


def sync_index(start_date=None):
    """同步上证指数(增量), 返回新增条数。"""
    conn = sqlite3.connect(DB)
    latest = conn.execute(
        "SELECT MAX(fsrq) FROM index_nav WHERE index_code=?", (INDEX_CODE,)).fetchone()[0]
    conn.close()
    if start_date is None:
        start_date = latest or "2000-01-01"
    elif latest:
        start_date = min(start_date, latest)
    rows = fetch_index(start_date)
    if rows:
        conn = sqlite3.connect(DB)
        conn.executemany(
            "INSERT OR REPLACE INTO index_nav (index_code, fsrq, close) VALUES (?,?,?)",
            [(INDEX_CODE, d, v) for d, v in rows])
        conn.commit()
        conn.close()
    return len(rows)


def _mkt_prefix(code):
    """深沪市场前缀: 5开头=上海, 其余=深圳。"""
    return "sh" if code.startswith("5") else "sz"


def fetch_prices(codes):
    """腾讯实时行情批量抓基金/ETF最新成交价。返回 {code:(date, price)}。"""
    qs = ",".join(_mkt_prefix(c) + c for c in codes)
    req = urllib.request.Request(TX_ETF_QUOTE % qs, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as r:
        txt = r.read().decode("gbk", errors="ignore")
    out = {}
    for seg in txt.split(";"):
        seg = seg.strip()
        if "~" not in seg:
            continue
        f = seg.split("~")
        code = f[2] if len(f) > 2 and f[2].isdigit() and len(f[2]) == 6 else ""
        try:
            price = float(f[3])
            ts = f[30][:8]
        except (IndexError, ValueError):
            continue
        if not code or price <= 0 or len(ts) != 8 or not ts.isdigit():
            continue
        dd = "%s-%s-%s" % (ts[0:4], ts[4:6], ts[6:8])
        out[code] = (dd, price)
    return out


TX_KLINE = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?"
             "param=%s,day,,,%d,")  # 不复权, 返回真实成交价


def fetch_price_history(code, start_date, days=120):
    """拉某基金日线收盘(交易价格)历史。start_date 为 YYYY-MM-DD。
    主源: 东财K线; 降级: 腾讯K线。返回 [(fsrq, price), ...]。"""
    # 主源: 东财
    secid = ("1." if code.startswith("5") else "0.") + code
    beg = start_date.replace("-", "")
    try:
        d = _get_json(EM_KLINE_SEC % (secid, beg), "http://quote.eastmoney.com/")
        out = []
        for line in (d.get("data") or {}).get("klines", []):
            parts = line.split(",")
            try:
                out.append((parts[0], float(parts[1])))
            except (IndexError, ValueError):
                continue
        if out:
            return out
    except Exception as e:
        log.warning("东财K线(%s)失败: %s", code, e)
    # 降级: 腾讯K线(不复权)
    try:
        sym = _mkt_prefix(code) + code
        req = urllib.request.Request(TX_KLINE % (sym, days), headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read().decode("utf-8"))
        k = (d.get("data") or {}).get(sym) or {}
        arr = k.get("day") or []
        out = []
        for row in arr:
            try:
                out.append((row[0], float(row[2])))
            except (IndexError, ValueError):
                continue
        return [p for p in out if p[0] >= start_date]
    except Exception as e:
        log.warning("腾讯K线(%s)失败: %s", code, e)
        return []


def sync_prices(backfill_days=100):
    """同步全部基金交易价格入 fund_price 表(与净值同频定时调用)。
    日常增量: 只抓腾讯实时最新价入当曰; 若某只库内最新价落后超过2天
    (停机/新增标的等缺口), 仅对该只回补近 backfill_days 天K线。"""
    import datetime as _dt
    conn = sqlite3.connect(DB)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS fund_price (fund_code TEXT, fsrq TEXT,"
        " price REAL, PRIMARY KEY(fund_code, fsrq))")
    latest = {r[0]: r[1] for r in
              conn.execute("SELECT fund_code, MAX(fsrq) FROM fund_price GROUP BY fund_code")}
    conn.close()
    today = _dt.date.today()
    rows = []
    # 实时最新价(全量, 一次批量请求)
    try:
        rows = [(c, d, p) for c, (d, p) in fetch_prices(list(FUNDS.keys())).items()]
    except Exception as e:
        log.warning("腾讯实时行情失败: %s", e)
    # 仅对有缺口的基金回补K线历史(增量例外, 不是每日全量)
    for code in FUNDS:
        last = latest.get(code)
        if not last:
            need = True  # 新标的, 库内无价
        else:
            gap = (today - _dt.date(int(last[0:4]), int(last[5:7]), int(last[8:10]))).days
            need = gap > 2   # 正常得日更新不会触发; 停机/漏抓才补
        if need:
            beg = (today - _dt.timedelta(days=backfill_days)).strftime("%Y-%m-%d")
            rows.extend((code, d, p) for d, p in fetch_price_history(code, beg))
            time.sleep(0.2)
    if rows:
        conn = sqlite3.connect(DB)
        conn.executemany(
            "INSERT OR REPLACE INTO fund_price (fund_code, fsrq, price) VALUES (?,?,?)", rows)
        conn.commit()
        conn.close()
    return len(rows)


def backfill_recent_gap(days=14):
    """启动时补齐最近 N 天的价格缺口(含中间缺失日)。
    参考交易日 = 近 N 天 fund_nav/index_nav 中出现过的日期(剔除周末与非交易日);
    某基金缺失其中任一天 → 仅对该基金回补该窗口K线并补写缺失日。
    返回 {code: 补到的天数}。"""
    import datetime as _dt
    today = _dt.date.today()
    beg = (today - _dt.timedelta(days=days)).strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB)
    ref_dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT fsrq FROM fund_nav WHERE fsrq>=? "
        "UNION SELECT DISTINCT fsrq FROM index_nav WHERE fsrq>=? "
        "UNION SELECT DISTINCT fsrq FROM fund_price WHERE fsrq>=? "
        "ORDER BY 1", (beg, beg, beg))]
    ref = set(ref_dates)
    # 当天实时价尚未发布/非交易日不视为缺口: 只要求覆盖到库内全局最新日的前一天
    if ref_dates:
        cutoff = max(ref_dates)
        ref = {d for d in ref if d < cutoff}
    fixed = {}
    for code in FUNDS:
        have = {r[0] for r in conn.execute(
            "SELECT DISTINCT fsrq FROM fund_price WHERE fund_code=? AND fsrq>=?",
            (code, beg))}
        missing = sorted(ref - have)
        if not missing:
            continue
        hist = dict(fetch_price_history(code, beg, days=days + 10))
        got = 0
        for d in missing:
            if d in hist:
                conn.execute(
                    "INSERT OR REPLACE INTO fund_price (fund_code, fsrq, price) VALUES (?,?,?)",
                    (code, d, hist[d]))
                got += 1
        if got:
            fixed[code] = got
            log.info("补齐 %s 价格缺口 %d 天: %s", code, got, missing)
        time.sleep(0.2)
    conn.commit()
    conn.close()
    if fixed:
        log.info("启动缺口补齐完成: %s", fixed)
    return fixed


def sync_on_startup():
    """启动时主动同步: 补齐从库里最后日期到今天的数据(解决停机缺口)。"""
    total = {}
    try:
        for code, name in FUNDS.items():
            try:
                n = sync_fund(code)
                total[code] = n
            except Exception as e:
                log.error("基金 %s %s 同步失败: %s", code, name, e)
                total[code] = -1
        n_idx = sync_index()
        total["index"] = n_idx
        try:
            total["prices"] = sync_prices()
        except Exception as e:
            log.error("成交价同步失败: %s", e)
            total["prices"] = -1
        try:
            total["gap_fix"] = backfill_recent_gap(14)
        except Exception as e:
            log.error("近两周价格缺口补齐失败: %s", e)
            total["gap_fix"] = -1
        log.info("启动同步完成: %s", total)
    except Exception as e:
        log.error("启动同步异常: %s", e)
    return total
