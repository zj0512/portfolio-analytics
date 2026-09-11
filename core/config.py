# -*- coding: utf-8 -*-
"""全局配置: 路径、标的映射、默认参数。"""
import os

# 项目根目录 = core/ 的上一级
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(BASE, "portfolio.db")              # 净值/指数库
DZ_DB = os.path.join(BASE, "portfolio_duizhang.db")  # 对账单/持仓库
STATIC = os.path.join(BASE, "static")

# 标的映射
FUNDS = {
    "159682": "创业五零", "159593": "A50指数", "515180": "100红利", "563020": "低波红利",
    "511090": "30年国债", "511130": "国债30年", "159649": "国开债", "511360": "短融ETF",
    "159937": "黄金9999", "518880": "黄金ETF", "513100": "纳指ETF", "513650": "标普ETF",
    "159516": "半导设备",
}
CORE = list(FUNDS.keys())

# 单只基金图表默认标的: 创业50
DEFAULT_FUND = "159682"

# 基准指数: 上证指数
INDEX_CODE = "000001"
INDEX_NAME = "上证指数"

PORT = 5050
