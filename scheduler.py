# -*- coding: utf-8 -*-
"""
投资组合工具 · 定时任务模块 (scheduler.py)
内嵌于工具进程, 随工具启动/停止. 注册的定时任务由 APScheduler 调度,
跑的是纯 Python 命令(抓净值等), 不调用模型, 因此零 token 消耗.

用法:
    from scheduler import start_scheduler, stop_scheduler
    scheduler = start_scheduler()   # 工具启动时调用
    ...
    stop_scheduler(scheduler)       # 工具停止时调用

新增定时任务: 在 register_jobs() 里添加 scheduler.add_job(...) 即可。
"""
import logging
from apscheduler.schedulers.background import BackgroundScheduler

log = logging.getLogger("portfolio.scheduler")

# 定时任务定义表: 集中管理, 便于扩展
# (job_id, 函数, 触发规则, 是否工作日)
def _update_nav_job():
    """抓取13只基金最新净值并更新数据库(纯脚本, 不调模型)。"""
    import subprocess, sys, os
    script = r"D:\zj\portfolio_app\update_nav.py"
    py = r"C:\Users\zc\AppData\Local\Programs\Python\Python312\python.exe"
    try:
        subprocess.run([py, script], capture_output=True, timeout=300)
        log.info("定时净值更新完成")
    except Exception as e:
        log.error("定时净值更新失败: %s", e)


def register_jobs(scheduler):
    """注册所有定时任务。新增任务在此追加。"""
    # 每工作日 15:02 更新净值
    # cron: minute=2, hour=15, day_of_week='mon-fri'
    scheduler.add_job(
        _update_nav_job,
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
    """启动调度器(工具启动时调用), 返回 scheduler 实例。"""
    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    register_jobs(scheduler)
    scheduler.start()
    log.info("调度器已启动")
    return scheduler


def stop_scheduler(scheduler):
    """停止调度器(工具停止时调用)。"""
    if scheduler:
        try:
            scheduler.shutdown(wait=False)
            log.info("调度器已停止")
        except Exception as e:
            log.error("停止调度器失败: %s", e)
