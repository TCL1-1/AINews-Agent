from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import Base
from app.models import AgentRun, DigestJob, PreferenceProfile, User
from app.tools.digest import push_digest, save_digest
from app.tools.registry import ToolContext


def test_save_and_push_are_idempotent(tmp_path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    with Session(engine, expire_on_commit=False) as db:
        user = User(email="test@example.com", password_hash="not-used")
        db.add(user)
        db.flush()
        db.add(PreferenceProfile(user_id=user.id, max_items=5))
        job = DigestJob(user_id=user.id, idempotency_key="test-job")
        db.add(job)
        db.flush()
        run = AgentRun(job_id=job.id, user_id=user.id, model_name="test")
        db.add(run)
        db.commit()
        context = ToolContext(
            db=db,
            user_id=user.id,
            run_id=run.id,
            workspace=tmp_path,
            settings=Settings(_env_file=None, deepseek_api_key="test"),
        )
        payload = {
            "title": "测试简报",
            "digest_date": now.date().isoformat(),
            "coverage_start": now.isoformat(),
            "coverage_end": now.isoformat(),
            "overview": "这是用于验证保存、版本和推送幂等逻辑的完整测试简报概览。",
            "items": [
                {
                    "original_title": "Official event",
                    "generated_title": "已经确认的未来发布会",
                    "original_url": "https://example.com/event?utm_source=test",
                    "canonical_url": "https://example.com/event",
                    "source_name": "Example Official",
                    "published_at": now.isoformat(),
                    "summary": "官方已经确认将在未来举行产品发布会，具体时间以官方页面为准。",
                    "recommendation_reason": "这是用户关注领域的重要未来事件。",
                    "category": "即将发生",
                    "event_status": "即将发生",
                    "keywords": ["发布会"],
                    "relevance_score": 90,
                    "evidence_reference": "官方活动页面",
                }
            ],
            "warnings": [],
        }
        saved = save_digest(context, payload)
        repeated_save = save_digest(context, payload)
        assert saved["digest_id"] == repeated_save["digest_id"]
        first = push_digest(context, saved["digest_id"], "in_app")
        second = push_digest(context, saved["digest_id"], "in_app")
        assert first["delivery_id"] == second["delivery_id"]
        assert second["already_sent"] is True
