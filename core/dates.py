# -*- coding: utf-8 -*-
"""日期工具。库里净值日期统一为 YYYY-MM-DD。"""
import datetime


def norm_date(d):
    """将日期统一为 YYYY-MM-DD。兼容 20240930 和 2026-09-08 两种输入。"""
    d = (d or "").strip()
    if len(d) == 8 and d.isdigit():
        return "%s-%s-%s" % (d[0:4], d[4:6], d[6:8])
    if len(d) >= 10:
        return d[0:10]
    return d


def today():
    return datetime.date.today().strftime("%Y-%m-%d")


def now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def days_ago(n):
    return (datetime.date.today() - datetime.timedelta(days=n)).strftime("%Y-%m-%d")
