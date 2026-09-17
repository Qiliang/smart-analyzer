"""兼容入口：独立进程跑调度时仍可用。

推荐直接 ``uv run fastapi run``，UI 与 BackgroundScheduler 同进程。
"""

from __future__ import annotations

import time

from smart_analyzer.jobs import get_job_manager

if __name__ == "__main__":
    mgr = get_job_manager()
    mgr.start()
    print("scheduler started (embedded BackgroundScheduler)", flush=True)
    for item in mgr.list_schedules():
        print(
            f"  - {item['name']} {item['cron_label']} next={item.get('next_run_time')}",
            flush=True,
        )
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        mgr.shutdown()
