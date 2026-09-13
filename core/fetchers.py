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


def sync_prices():
    """抓全部基金最新成交价写入 fund_price 表(与净值同频定时调用, 逐日累积)。
    返回写入条数。"""
    data = fetch_prices(list(FUNDS.keys()))
    n = 0
    if data:
        conn = sqlite3.connect(DB)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS fund_price (fund_code TEXT, fsrq TEXT,"
            " price REAL, PRIMARY KEY(fund_code, fsrq))")
        conn.executemany(
            "INSERT OR REPLACE INTO fund_price (fund_code, fsrq, price) VALUES (?,?,?)",
            [(c, d, p) for c, (d, p) in data.items()])
        conn.commit()
        conn.close()
        n = len(data)
    return n


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
        log.info("启动同步完成: %s", total)
    except Exception as e:
        log.error("启动同步异常: %s", e)
    return total
