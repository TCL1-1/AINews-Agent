from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AgentRun, DigestJob, ToolCall, User


def test_job_template_hides_tool_call_timeline() -> None:
    template = (Path(__file__).parents[1] / "app" / "templates" / "job.html").read_text(
        encoding="utf-8"
    )

    hidden_content = (
        "工具调用时间线",
        "尚无工具调用记录",
        "run.tool_call_count",
        "run.tool_calls",
        "call.tool_name",
        "call.duration_ms",
        "call.result_summary",
    )

    assert "任务状态" in template
    for content in hidden_content:
        assert content not in template


def test_tool_calls_remain_persisted_for_backend_auditing() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine, expire_on_commit=False) as db:
        user = User(email="audit@example.com", password_hash="not-used")
        db.add(user)
        db.flush()
        job = DigestJob(user_id=user.id, idempotency_key="audit-job")
        db.add(job)
        db.flush()
        run = AgentRun(
            job_id=job.id,
            user_id=user.id,
            model_name="test-model",
            tool_call_count=2,
        )
        db.add(run)
        db.flush()
        db.add_all(
            [
                ToolCall(
                    run_id=run.id,
                    sequence_number=2,
                    tool_name="fetch_article",
                    status="SUCCEEDED",
                    duration_ms=24,
                ),
                ToolCall(
                    run_id=run.id,
                    sequence_number=1,
                    tool_name="search_news",
                    status="SUCCEEDED",
                    duration_ms=12,
                ),
            ]
        )
        db.commit()
        db.expire_all()

        stored_run = db.get(AgentRun, run.id)
        stored_calls = db.scalars(
            select(ToolCall).where(ToolCall.run_id == run.id).order_by(ToolCall.sequence_number)
        ).all()

        assert stored_run is not None
        assert stored_run.tool_call_count == 2
        assert [call.tool_name for call in stored_calls] == [
            "search_news",
            "fetch_article",
        ]
        assert [call.sequence_number for call in stored_run.tool_calls] == [1, 2]
