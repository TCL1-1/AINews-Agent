from __future__ import annotations

from apscheduler.schedulers.blocking import BlockingScheduler

from app.config import get_settings
from app.database import init_db
from app.jobs import dispatch_due_jobs


def main() -> None:
    settings = get_settings()
    init_db()
    scheduler = BlockingScheduler(timezone=settings.default_timezone)
    scheduler.add_job(
        dispatch_due_jobs,
        trigger="interval",
        minutes=1,
        id="dispatch-due-digests",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()


if __name__ == "__main__":
    main()
