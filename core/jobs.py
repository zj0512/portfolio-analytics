# -*- coding: utf-8 -*-
"""定时任务: APScheduler 内嵌调度, 随工具启停, 零 token。
- 工作日 15:02 自动同步净值/指数
"""
import logging

from apscheduler.schedulers.background import BackgroundScheduler

from . import fetchers
from .config import FUNDS

log = logging.getLogger("portfolio.scheduler")


def _daily_sync_job():
    """定时任务: 拉取全部基金+指数最新净值。"""
    try:
        for code in FUNDS:
            fetchers.sync_fund(code)
        fetchers.sync_index()
        log.info("定时净值更新完成")
    except Exception as e:
        log.error("定时净值更新失败: %s", e)


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
    log.info("已注册定时任务: 工作日15:02 更新净值")


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
