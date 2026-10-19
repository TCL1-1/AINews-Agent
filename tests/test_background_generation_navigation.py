from pathlib import Path

from fastapi import BackgroundTasks
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.main as main_app
from app.database import Base
from app.main import get_active_job
from app.models import DigestJob, User


def test_get_active_job_returns_latest_pending_or_running_job() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine, expire_on_commit=False) as db:
        user = User(email="background@example.com", password_hash="not-used")
        db.add(user)
        db.flush()
        db.add(
            DigestJob(
                user_id=user.id,
                idempotency_key="finished-job",
                status="SUCCEEDED",
            )
        )
        older = DigestJob(
            user_id=user.id,
            idempotency_key="older-running-job",
            status="RUNNING",
        )
        db.add(older)
        db.flush()
        latest = DigestJob(
            user_id=user.id,
            idempotency_key="latest-pending-job",
            status="PENDING",
        )
        db.add(latest)
        db.commit()

        active = get_active_job(db, user.id)

        assert active is not None
        assert active.id == latest.id


def test_run_now_returns_existing_job_without_scheduling_duplicate(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine, expire_on_commit=False) as db:
        user = User(email="resume@example.com", password_hash="not-used")
        db.add(user)
        db.flush()
        running = DigestJob(
            user_id=user.id,
            idempotency_key="current-running-job",
            status="RUNNING",
        )
        db.add(running)
        db.commit()

        monkeypatch.setattr(main_app, "check_csrf", lambda request, submitted: None)
        monkeypatch.setattr(main_app, "current_user", lambda request, session: user)
        monkeypatch.setattr(
            main_app,
            "create_job",
            lambda user_id: (_ for _ in ()).throw(AssertionError("duplicate job created")),
        )
        background = BackgroundTasks()

        response = main_app.run_now(
            request=object(),
            background=background,
            csrf="test-token",
            db=db,
        )

        assert response.status_code == 303
        assert response.headers["location"] == f"/jobs/{running.id}"
        assert background.tasks == []


def test_generation_pages_allow_navigation_and_resume_current_job() -> None:
    template_dir = Path(__file__).parents[1] / "app" / "templates"
    dashboard = (template_dir / "dashboard.html").read_text(encoding="utf-8")
    job = (template_dir / "job.html").read_text(encoding="utf-8")

    assert "{% if active_job %}返回生成页面{% else %}立即生成简报{% endif %}" in dashboard
    assert 'action="/run-now"' in dashboard
    assert "Agent 正在后台执行" in job
    assert "你可以切换到其他页面，任务不会中断" in job
