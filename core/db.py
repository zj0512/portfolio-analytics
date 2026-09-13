# -*- coding: utf-8 -*-
"""数据存取层: SQLite 读写, 只负责 SQL, 不含业务计算。"""
import json
import sqlite3
from .config import DB, DZ_DB, CORE, INDEX_CODE
from .dates import norm_date, now_str


def _conn(path):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ---------- 净值 / 指数 ----------
def nav_all(code):
    """某基金净值序列 [(fsrq, dwjz), ...] 升序。"""
    conn = _conn(DB)
    rows = conn.execute(
        "SELECT fsrq, dwjz FROM fund_nav WHERE fund_code=? ORDER BY fsrq", (code,)
    ).fetchall()
    conn.close()
    return rows


def index_all(index_code=INDEX_CODE):
    conn = _conn(DB)
    rows = conn.execute(
        "SELECT fsrq, close FROM index_nav WHERE index_code=? ORDER BY fsrq", (index_code,)
    ).fetchall()
    conn.close()
    return rows


def upsert_nav(code, fsrq, dwjz):
    conn = _conn(DB)
    conn.execute(
        "INSERT OR REPLACE INTO fund_nav (fund_code, fsrq, dwjz) VALUES (?,?,?)",
        (code, fsrq, dwjz),
    )
    conn.close()


def upsert_index(index_code, fsrq, close):
    conn = _conn(DB)
    conn.execute(
        "INSERT OR REPLACE INTO index_nav (index_code, fsrq, close) VALUES (?,?,?)",
        (index_code, fsrq, close),
    )
    conn.close()


def all_trade_dates():
    """净值+成交价库中所有出现过的交易日期(升序, 去重)。"""
    conn = _conn(DB)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS fund_price (fund_code TEXT, fsrq TEXT,"
        " price REAL, PRIMARY KEY(fund_code, fsrq))")
    rows = [r[0] for r in conn.execute(
        "SELECT DISTINCT fsrq FROM (SELECT DISTINCT fsrq FROM fund_nav"
        " UNION SELECT DISTINCT fsrq FROM fund_price) ORDER BY fsrq")]
    conn.close()
    return rows


# ---------- 成交价 ----------
def price_all(code):
    """某基金成交价序列 [(fsrq, price), ...] 升序。"""
    conn = _conn(DB)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS fund_price (fund_code TEXT, fsrq TEXT,"
        " price REAL, PRIMARY KEY(fund_code, fsrq))")
    rows = conn.execute(
        "SELECT fsrq, price FROM fund_price WHERE fund_code=? ORDER BY fsrq", (code,)
    ).fetchall()
    conn.close()
    return rows


def upsert_price(code, fsrq, price):
    conn = _conn(DB)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS fund_price (fund_code TEXT, fsrq TEXT,"
        " price REAL, PRIMARY KEY(fund_code, fsrq))")
    conn.execute(
        "INSERT OR REPLACE INTO fund_price (fund_code, fsrq, price) VALUES (?,?,?)",
        (code, fsrq, price))
    conn.close()


# ---------- 对账单 / 持仓 ----------
def core_trades():
    """核心标的的全部交易记录(对账单 + 手动录入), 统一格式:
    [(date, code, price, qty, occur), ...] occur<0=买入, >0=卖出。"""
    conn = _conn(DZ_DB)
    ph = ",".join("?" * len(CORE))
    recs = []
    for d, code, price, qty, occur in conn.execute(
        "SELECT trade_date, sec_code, price, qty, occur_amount FROM duizhang_record"
        " WHERE sec_code IN (%s)" % ph, CORE):
        try:
            p = float(price) if price else 0.0
            q = float(qty) if qty else 0.0
            o = float(occur) if occur else 0.0
        except (TypeError, ValueError):
            continue
        recs.append((norm_date(d), code, p, q, o))
    conn.close()
    return sorted(recs, key=lambda x: (x[0], x[1]))


def core_flows():
    """核心标的现金流 [(date, amount), ...] 金额负=买, 正=卖。
    对账单 + holdings 手动录入合并。"""
    conn = _conn(DZ_DB)
    ph = ",".join("?" * len(CORE))
    cur = conn.cursor()
    flows = []
    for d, occur in cur.execute(
        "SELECT trade_date, occur_amount FROM duizhang_record WHERE sec_code IN (%s)" % ph, CORE):
        try:
            v = float(occur) if occur else 0
        except (TypeError, ValueError):
            continue
        if v != 0:
            flows.append((norm_date(d), v))
    for hd, hcode, haction, hqty, hprice in cur.execute(
        "SELECT hdate, sec_code, action, qty, price FROM holdings WHERE sec_code IN (%s)" % ph, CORE):
        try:
            hq = float(hqty) if hqty else 0.0
            hp = float(hprice) if hprice else 0.0
        except (TypeError, ValueError):
            continue
        if hq == 0:
            continue
        dd = norm_date(hd)
        if haction == "BUY":
            flows.append((dd, -(hq * hp) if hp > 0 else -1.0))
        elif haction == "SELL":
            flows.append((dd, (hq * hp) if hp > 0 else 1.0))
    conn.close()
    flows.sort()
    return flows


def fund_flows_trades(code):
    """单只基金: 现金流 [(date, amt)] 与交易 [(date, price, qty, occur)]。
    对账单 + holdings 手动录入合并。"""
    conn = _conn(DZ_DB)
    recs = conn.execute(
        "SELECT trade_date, price, qty, occur_amount FROM duizhang_record"
        " WHERE sec_code=? ORDER BY trade_date", (code,)).fetchall()
    holds = conn.execute(
        "SELECT hdate, action, qty, price FROM holdings WHERE sec_code=? ORDER BY hdate",
        (code,)).fetchall()
    conn.close()
    flows, trades = [], []
    for d, price, qty, occur in recs:
        try:
            p = float(price) if price else 0.0
            q = float(qty) if qty else 0.0
            o = float(occur) if occur else 0.0
        except (TypeError, ValueError):
            continue
        if o == 0:
            continue
        dd = norm_date(d)
        flows.append((dd, o))
        trades.append((dd, p, q, o))
    for hd, haction, hqty, hprice in holds:
        try:
            hq = float(hqty) if hqty else 0.0
            hp = float(hprice) if hprice else 0.0
        except (TypeError, ValueError):
            continue
        if hq == 0:
            continue
        dd = norm_date(hd)
        if haction == "BUY":
            o = -(hq * hp) if hp > 0 else -1.0
        elif haction == "SELL":
            o = (hq * hp) if hp > 0 else 1.0
        else:
            continue
        flows.append((dd, o))
        trades.append((dd, hp, hq, o))
    flows.sort()
    trades.sort(key=lambda x: x[0])
    return flows, trades


def holdings_list():
    conn = _conn(DZ_DB)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS holdings (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " hdate TEXT, sec_code TEXT, action TEXT, qty REAL, price REAL)")
    rows = [{"id": r[0], "date": r[1], "code": r[2], "action": r[3], "qty": r[4], "price": r[5]}
            for r in conn.execute(
                "SELECT id, hdate, sec_code, action, qty, price FROM holdings ORDER BY hdate")]
    conn.close()
    return rows


# ---------- 模拟比例调整 ----------
def sim_alloc_get():
    conn = _conn(DB)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sim_alloc (id INTEGER PRIMARY KEY CHECK(id=1),"
        " payload TEXT, updated_at TEXT)")
    row = conn.execute("SELECT payload FROM sim_alloc WHERE id=1").fetchone()
    conn.close()
    return json.loads(row[0]) if row and row[0] else None


def sim_targets_get():
    """拟持仓目标值 {code: number}, 独立表单行存储。"""
    conn = _conn(DB)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sim_targets (id INTEGER PRIMARY KEY CHECK(id=1),"
        " payload TEXT, updated_at TEXT)")
    row = conn.execute("SELECT payload FROM sim_targets WHERE id=1").fetchone()
    conn.close()
    try:
        return json.loads(row[0]) if row and row[0] else None
    except (ValueError, TypeError):
        return None


def sim_targets_set(targets):
    conn = _conn(DB)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sim_targets (id INTEGER PRIMARY KEY CHECK(id=1),"
        " payload TEXT, updated_at TEXT)")
    conn.execute(
        "INSERT OR REPLACE INTO sim_targets (id, payload, updated_at) VALUES (1,?,?)",
        (json.dumps(targets, ensure_ascii=False), now_str()))
    conn.commit()
    conn.close()


def sim_alloc_set(payload):
    conn = _conn(DB)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sim_alloc (id INTEGER PRIMARY KEY CHECK(id=1),"
        " payload TEXT, updated_at TEXT)")
    conn.execute(
        "INSERT OR REPLACE INTO sim_alloc (id, payload, updated_at) VALUES (1,?,?)",
        (json.dumps(payload, ensure_ascii=False), now_str()))
    conn.commit()
    conn.close()


def holding_add(date, code, action, qty, price):
    conn = _conn(DZ_DB)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS holdings (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " hdate TEXT, sec_code TEXT, action TEXT, qty REAL, price REAL)")
    cur = conn.execute(
        "INSERT INTO holdings (hdate, sec_code, action, qty, price) VALUES (?,?,?,?,?)",
        (date, code, action, qty, price))
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id
