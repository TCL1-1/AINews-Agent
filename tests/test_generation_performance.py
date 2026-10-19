import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.agent.runtime import AgentRuntime, _audit_result_summary
from app.config import Settings
from app.database import Base
from app.models import AgentRun, DigestJob, ToolCall, User
from app.tools.news import search_news
from app.tools.registry import ToolContext


class _EmptyScalars:
    def all(self) -> list[object]:
        return []


class _FakeDb:
    def scalars(self, _query: object) -> _EmptyScalars:
        return _EmptyScalars()


class _SearchResponse:
    def __init__(self, url: str) -> None:
        self.url = url
        self.content = b"<rss><channel></channel></rss>"
        self.encoding = "utf-8"

    @property
    def text(self) -> str:
        if "duckduckgo" not in self.url:
            return ""
        return """
        <div class="result">
          <a class="result__a" href="https://openai.com/news/example">OpenAI AI update</a>
          <div class="result__snippet">Official AI product update</div>
        </div>
        """

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        if "gdelt" not in self.url:
            return {}
        return {
            "articles": [
                {
                    "title": "NVIDIA announces an AI chip",
                    "url": "https://nvidia.com/news/ai-chip",
                    "domain": "nvidia.com",
                    "seendate": "20261016T010000Z",
                    "language": "English",
                }
            ]
        }


class _ConcurrentSearchClient:
    active = 0
    max_active = 0

    def __init__(self, **_kwargs: object) -> None:
        type(self).active = 0
        type(self).max_active = 0

    async def __aenter__(self) -> "_ConcurrentSearchClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def get(self, url: str, **_kwargs: object) -> _SearchResponse:
        type(self).active += 1
        type(self).max_active = max(type(self).max_active, type(self).active)
        await asyncio.sleep(0.01)
        type(self).active -= 1
        return _SearchResponse(url)


@pytest.mark.asyncio
async def test_rss_sources_run_concurrently_and_report_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.tools.news.httpx.AsyncClient", _ConcurrentSearchClient)
    context = SimpleNamespace(db=_FakeDb(), settings=SimpleNamespace())

    result = await search_news(
        context,  # type: ignore[arg-type]
        query="AI Agent",
        start_time="2026-10-15T00:00:00Z",
        end_time="2026-10-17T00:00:00Z",
        mode="rss",
    )

    assert _ConcurrentSearchClient.max_active == 5
    assert len(result["provider_metrics"]) == 5
    assert all(metric["provider"].startswith("rss/") for metric in result["provider_metrics"])


@pytest.mark.asyncio
async def test_auto_web_providers_run_concurrently_and_merge_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.tools.news.httpx.AsyncClient", _ConcurrentSearchClient)
    context = SimpleNamespace(
        db=_FakeDb(),
        settings=SimpleNamespace(
            web_search_provider="auto",
            gdelt_doc_api_url="https://api.gdeltproject.org/api/v2/doc/doc",
            duckduckgo_search_url="https://html.duckduckgo.com/html/",
        ),
    )

    result = await search_news(
        context,  # type: ignore[arg-type]
        query="AI chip",
        start_time="2026-10-15T00:00:00Z",
        end_time="2026-10-17T00:00:00Z",
        mode="web",
    )

    assert _ConcurrentSearchClient.max_active == 2
    assert {metric["provider"] for metric in result["provider_metrics"]} == {
        "web/gdelt",
        "web/duckduckgo",
    }
    assert {candidate["provider"] for candidate in result["candidates"]} == {
        "gdelt_web_search",
        "duckduckgo_web_search",
    }


def test_audit_summary_stays_valid_json_and_preserves_watchlist_fields() -> None:
    result = {
        "provider": "twitterapi_io",
        "provider_status": "succeeded",
        "requested_handles": ["OpenAI", "AnthropicAI"],
        "handles_with_results": ["OpenAI"],
        "handles_without_results": ["AnthropicAI"],
        "count": 100,
        "candidates": [{"text": "x" * 1000} for _ in range(100)],
    }

    summary = _audit_result_summary("search_x_accounts", result)
    decoded = json.loads(summary)

    assert len(summary) <= 2000
    assert decoded["requested_handles"] == ["OpenAI", "AnthropicAI"]
    assert decoded["handles_with_results"] == ["OpenAI"]
    assert "candidates" not in decoded


class _FetchRegistry:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0

    def api_tools(self) -> list[dict[str, Any]]:
        return []

    async def execute(
        self, name: str, arguments: dict[str, Any], _context: ToolContext
    ) -> dict[str, Any]:
        assert name == "fetch_article"
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.02)
        self.active -= 1
        return {
            "url": arguments["url"],
            "canonical_url": arguments["url"],
            "title": "Verified article",
            "source": "example.com",
            "content": "verified " * 100,
            "content_hash": "hash",
            "fetch_status": "success",
        }


class _RuntimeMessage:
    def __init__(self, tool_calls: list[object], content: str | None = None) -> None:
        self.tool_calls = tool_calls
        self.content = content

    def model_dump(self, **_kwargs: object) -> dict[str, Any]:
        return {"role": "assistant", "content": self.content or ""}


class _RuntimeProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, _messages: object, _tools: object) -> object:
        self.calls += 1
        if self.calls == 1:
            tool_calls = [
                SimpleNamespace(
                    id=f"fetch-{index}",
                    function=SimpleNamespace(
                        name="fetch_article",
                        arguments=json.dumps({"url": f"https://example.com/{index}"}),
                    ),
                )
                for index in range(3)
            ]
            message = _RuntimeMessage(tool_calls)
        else:
            message = _RuntimeMessage([], "done")
        return SimpleNamespace(usage=None, choices=[SimpleNamespace(message=message)])


@pytest.mark.asyncio
async def test_runtime_batches_consecutive_article_fetches_with_limit(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = Settings(
        deepseek_api_key="test-key",
        agent_max_rounds=2,
        agent_fetch_concurrency=2,
    )
    registry = _FetchRegistry()
    with Session(engine, expire_on_commit=False) as db:
        user = User(email="performance@example.com", password_hash="not-used")
        db.add(user)
        db.flush()
        job = DigestJob(user_id=user.id, idempotency_key="performance-job")
        db.add(job)
        db.flush()
        run = AgentRun(
            job_id=job.id,
            user_id=user.id,
            model_name="test-model",
            preference_snapshot={},
        )
        db.add(run)
        db.commit()
        context = ToolContext(
            db=db,
            user_id=user.id,
            run_id=run.id,
            workspace=tmp_path,
            settings=settings,
        )
        runtime = AgentRuntime(
            settings,
            db,
            run,
            registry,  # type: ignore[arg-type]
            context,
        )
        runtime.provider = _RuntimeProvider()  # type: ignore[assignment]

        with pytest.raises(RuntimeError, match="before calling save_digest"):
            await runtime.run({}, run.started_at, run.started_at.replace(year=2027))

        calls = db.scalars(select(ToolCall).order_by(ToolCall.sequence_number)).all()
        assert registry.max_active == 2
        assert run.tool_call_count == 3
        assert [call.status for call in calls] == ["SUCCEEDED"] * 3
        assert [call.sequence_number for call in calls] == [1, 2, 3]
