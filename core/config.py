# -*- coding: utf-8 -*-
"""全局配置: 路径、标的映射、默认参数。
FUNDS 支持运行时扩展: 基础标的写死在此, 新标的存 portfolio.db 的 funds 表,
录入时通过 add_fund() 追加并原地更新 FUNDS/CORE, 所有模块(calcs/fetchers/jobs/web)立即可见。"""
import os
import sqlite3

# 项目根目录 = core/ 的上一级
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(BASE, "portfolio.db")              # 净值/指数库
DZ_DB = os.path.join(BASE, "portfolio_duizhang.db")  # 对账单/持仓库
STATIC = os.path.join(BASE, "static")

# 基础标的映射(内置)
_BASE_FUNDS = {
    "159682": "创业五零", "159593": "A50指数", "515180": "100红利", "563020": "低波红利",
    "511090": "30年国债", "511130": "国债30年", "159649": "国开债", "511360": "短融ETF",
    "159937": "黄金9999", "518880": "黄金ETF", "513100": "纳指ETF", "513650": "标普ETF",
    "159516": "半导设备",
}

# 运行时标的注册表: 内置 + DB 追加(顺序: 内置在前, 新增按录入顺序)
FUNDS = dict(_BASE_FUNDS)


def _now():
    import datetime
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _load_extra_funds():
    """启动时从 DB 加载追加的标的(表不存在则忽略)。"""
    try:
        conn = sqlite3.connect(DB)
        rows = conn.execute(
            "SELECT code, name FROM funds ORDER BY id").fetchall()
        conn.close()
        for code, name in rows:
            if code and code not in FUNDS:
                FUNDS[code] = name or code
    except sqlite3.Error:
        pass


_load_extra_funds()

CORE = list(FUNDS.keys())


def add_fund(code, name=None):
    """追加新标的: 写 DB + 原地更新 FUNDS/CORE。返回 (ok, error)。"""
    code = (code or "").strip()
    name = (name or "").strip()
    if not (len(code) == 6 and code.isdigit()):
        return False, "代码必须是6位数字"
    if code in FUNDS:
        return True, None          # 已存在, 幂等
    if not name:
        name = code
    conn = sqlite3.connect(DB)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS funds (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " code TEXT UNIQUE, name TEXT, added_at TEXT)")
    conn.execute("INSERT OR IGNORE INTO funds (code, name, added_at) VALUES (?,?,?)",
                 (code, name, _now()))
    conn.commit()
    conn.close()
    FUNDS[code] = name
    CORE.append(code)
    return True, None


# 单只基金图表默认标的: 创业50
DEFAULT_FUND = "159682"

# 基准指数: 上证指数
INDEX_CODE = "000001"
INDEX_NAME = "上证指数"

PORT = 5050
