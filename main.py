# -*- coding: utf-8 -*-
"""投资组合分析工具 · 入口
启动流程:
1. 后台线程执行启动同步(补齐停机期间缺口)
2. 拉起定时任务(工作日15:02)
3. 启动 Flask Web 服务
"""
import atexit
import logging
import threading

from core.jobs import start_scheduler, stop_scheduler
from core import fetchers
from web.app import run

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("portfolio.main")

if __name__ == "__main__":
    # 1) 启动补同步(后台线程, 不阻塞 Web 启动)
    t = threading.Thread(target=fetchers.sync_on_startup, daemon=True, name="startup-sync")
    t.start()
    log.info("启动同步线程已拉起(后台补齐停机期间净值缺口)")

    # 2) 定时任务
    scheduler = start_scheduler()
    atexit.register(stop_scheduler, scheduler)

    # 3) Web 服务
    run()
