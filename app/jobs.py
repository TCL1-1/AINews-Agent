from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.agent import AgentRuntime
from app.config import get_settings
from app.database import SessionLocal
from app.models import AgentRun, DigestJob, PreferenceProfile, User
from app.tools import ToolContext, build_tool_registry


def preference_snapshot(user: User, preference: PreferenceProfile | None) -> dict[str, Any]:
    settings = get_settings()
    return {
        "topics": preference.topics if preference else ["LLM", "半导体", "芯片", "自动驾驶"],
        "keywords": preference.keywords if preference else [],
        "excluded_keywords": preference.excluded_keywords if preference else [],
        "languages": preference.languages if preference else ["zh", "en"],
        "source_preferences": preference.source_preferences if preference else [],
        "x_accounts": preference.x_accounts if preference else [],
        "max_items": preference.max_items if preference else settings.default_max_items,
        "coverage_hours": (
            preference.coverage_hours if preference else settings.default_coverage_hours
        ),
        "timezone": user.timezone,
    }


def create_job(user_id: int, job_type: str = "manual", key: str | None = None) -> DigestJob:
    db = SessionLocal()
    try:
        job = DigestJob(
            user_id=user_id,
            job_type=job_type,
            scheduled_for=datetime.now(UTC),
            idempotency_key=key or f"manual:{user_id}:{uuid.uuid4().hex}",
            status="PENDING",
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(DigestJob).where(DigestJob.idempotency_key == key))
        if existing is None:
            raise
        return existing
    finally:
        db.close()


async def run_job(job_id: int) -> int:
    settings = get_settings()
    db = SessionLocal()
    run: AgentRun | None = None
    job = db.get(DigestJob, job_id)
    if job is None:
        db.close()
        raise ValueError("job not found")
    if job.status in {"RUNNING", "SUCCEEDED", "CANCELLED"}:
        db.close()
        return job.id
    try:
        user = db.get(User, job.user_id)
        if user is None or not user.is_active:
            raise ValueError("user is missing or disabled")
        preference = db.scalar(
            select(PreferenceProfile).where(PreferenceProfile.user_id == user.id)
        )
        snapshot = preference_snapshot(user, preference)
        job.status = "RUNNING"
        job.started_at = datetime.now(UTC)
        job.attempt_count += 1
        run = AgentRun(
            job_id=job.id,
            user_id=user.id,
            status="RUNNING",
            model_name=settings.deepseek_model,
            preference_snapshot=snapshot,
        )
        db.add(run)
        db.commit()
        db.refresh(run)

        workspace = settings.runtime_dir / f"run-{run.id}"
        workspace.mkdir(parents=True, exist_ok=True)
        context = ToolContext(
            db=db,
            user_id=user.id,
            run_id=run.id,
            workspace=Path(workspace),
            settings=settings,
        )
        coverage_end = datetime.now(UTC)
        coverage_start = coverage_end - timedelta(hours=int(snapshot["coverage_hours"]))
        runtime = AgentRuntime(settings, db, run, build_tool_registry(), context)
        outcome = await runtime.run(snapshot, coverage_start, coverage_end)

        run.status = "SUCCEEDED"
        run.stop_reason = outcome.stop_reason
        run.finished_at = datetime.now(UTC)
        job.status = "SUCCEEDED"
        job.finished_at = datetime.now(UTC)
        db.commit()
        return outcome.digest_id
    except Exception as exc:
        db.rollback()
        job = db.get(DigestJob, job_id)
        if job:
            job.status = "FAILED"
            job.error_code = type(exc).__name__
            job.error_message = str(exc)[:2000]
            job.finished_at = datetime.now(UTC)
        if run:
            run = db.get(AgentRun, run.id)
            if run:
                run.status = "FAILED"
                run.error_message = str(exc)[:2000]
                run.stop_reason = "error"
                run.finished_at = datetime.now(UTC)
        db.commit()
        raise
    finally:
        db.close()


def run_job_sync(job_id: int) -> int:
    return asyncio.run(run_job(job_id))


def dispatch_due_jobs() -> None:
    now_utc = datetime.now(UTC)
    db = SessionLocal()
    try:
        preferences = db.scalars(
            select(PreferenceProfile).where(PreferenceProfile.enabled.is_(True))
        ).all()
        due: list[int] = []
        for preference in preferences:
            user = db.get(User, preference.user_id)
            if user is None or not user.is_active:
                continue
            local_now = now_utc.astimezone(ZoneInfo(user.timezone))
            if not preference.weekend_enabled and local_now.weekday() >= 5:
                continue
            if (
                local_now.hour == preference.schedule_time.hour
                and local_now.minute == preference.schedule_time.minute
            ):
                key = f"scheduled:{user.id}:{local_now.date().isoformat()}"
                job = create_job(user.id, "scheduled", key)
                if job.status == "PENDING":
                    due.append(job.id)
    finally:
        db.close()
    for job_id in due:
        try:
            run_job_sync(job_id)
        except Exception:
            continue

