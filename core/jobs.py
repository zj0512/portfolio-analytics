# -*- coding: utf-8 -*-
"""定时任务: APScheduler 内嵌调度, 随工具启停, 零 token。
- 工作日 15:02 自动同步净值/指数
"""
import logging

from apscheduler.schedulers.background import BackgroundScheduler

from . import fetchers
from . import monitors
from .config import FUNDS

log = logging.getLogger("portfolio.scheduler")


def _daily_sync_job():
    """定时任务: 拉取全部基金+指数最新净值。"""
    try:
        for code in FUNDS:
            fetchers.sync_fund(code)
        fetchers.sync_index()
        try:
            fetchers.sync_prices()
        except Exception as e:
            log.error("成交价同步失败: %s", e)
        log.info("定时净值/成交价更新完成")
    except Exception as e:
        log.error("定时净值更新失败: %s", e)


def _monitor_job():
    """定时任务: 投资分析监控评估并落库。"""
    try:
        res = monitors.run_all()
        s = monitors.summary(res)
        log.info("监控评估完成: %s", s)
    except Exception as e:
        log.error("监控评估失败: %s", e)


def register_jobs(scheduler):
    scheduler.add_job(
        _daily_sync_job,
        trigger="cron",
        day_of_week="mon-fri",
        hour=15,
        minute=2,
        id="daily_nav_update",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    # 晚间补跑: 基金净值通常 19~22 点才在东财发布, 15:02 抓不到当天
    scheduler.add_job(
        _daily_sync_job,
        trigger="cron",
        day_of_week="mon-fri",
        hour=21,
        minute=2,
        id="daily_nav_update_evening",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    log.info("已注册定时任务: 工作日 21:02 晚间补跑净值更新")
    log.info("已注册定时任务: 工作日15:02 更新净值")
    # 投资分析监控: 21:30 净值/价格同步后评估
    scheduler.add_job(
        _monitor_job,
        trigger="cron",
        day_of_week="mon-fri",
        hour=21,
        minute=30,
        id="monitor_daily",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    log.info("已注册定时任务: 工作日21:30 投资分析监控评估")
    # 成交价与净值同频更新(15:02/21:02), 逐日累积入 fund_price 表
    # (已并入 _daily_sync_job)


def start_scheduler():
    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    register_jobs(scheduler)
    scheduler.start()
    log.info("调度器已启动")
    return scheduler


def stop_scheduler(scheduler):
    if scheduler:
        try:
            scheduler.shutdown(wait=False)
            log.info("调度器已停止")
        except Exception as e:
            log.error("停止调度器失败: %s", e)
