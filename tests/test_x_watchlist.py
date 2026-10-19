import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.jobs import preference_snapshot
from app.models import AgentRun, DigestJob, PreferenceProfile, ToolCall, User
from app.schemas import DigestDraft
from app.tools import build_tool_registry
from app.tools.digest import _enforce_x_watchlist
from app.tools.x_accounts import (
    build_x_account_query,
    normalize_x_handles,
    search_x_accounts,
)


def test_normalize_x_handles_accepts_names_and_profile_urls() -> None:
    assert normalize_x_handles(["@sama", "https://x.com/karpathy", "AndrewYNg", "@SAMA"]) == [
        "sama",
        "karpathy",
        "AndrewYNg",
    ]


def test_normalize_x_handles_rejects_more_than_three_accounts() -> None:
    with pytest.raises(ValueError, match="最多只能配置 3 个"):
        normalize_x_handles(["one", "two", "three", "four"])


def test_x_account_query_uses_twitterapi_advanced_search_format() -> None:
    start = datetime(2026, 10, 18, 6, 0, tzinfo=UTC)
    end = datetime(2026, 10, 19, 6, 0, tzinfo=UTC)

    query = build_x_account_query("@sama", start, end)

    assert query == (
        f"from:sama since_time:{int(start.timestamp())} until_time:{int(end.timestamp())} "
        "-filter:replies -filter:retweets"
    )


def test_x_watchlist_tool_schema_limits_accounts_to_three() -> None:
    tool = next(
        item
        for item in build_tool_registry().api_tools()
        if item["function"]["name"] == "search_x_accounts"
    )

    handles = tool["function"]["parameters"]["properties"]["handles"]
    assert handles["minItems"] == 1
    assert handles["maxItems"] == 3
    assert handles["uniqueItems"] is True


def test_preferences_page_has_three_x_account_inputs() -> None:
    template = (Path(__file__).parents[1] / "app" / "templates" / "preferences.html").read_text(
        encoding="utf-8"
    )

    assert template.count('name="x_account_') == 3
    assert "X 账号关注" in template


def test_preference_snapshot_contains_x_accounts() -> None:
    user = User(
        email="snapshot@example.com",
        password_hash="not-used",
        timezone="Asia/Shanghai",
    )
    preference = PreferenceProfile(user_id=1, x_accounts=["sama", "karpathy"])

    assert preference_snapshot(user, preference)["x_accounts"] == ["sama", "karpathy"]


class _FakeResponse:
    def __init__(self, handle: str) -> None:
        self.handle = handle

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return {
            "tweets": [
                {
                    "id": f"tweet-{self.handle}",
                    "text": f"New AI Agent update from {self.handle}",
                    "createdAt": "2026-10-19T05:00:00Z",
                    "author": {"userName": self.handle},
                    "likeCount": 42,
                }
            ]
        }


class _FakeClient:
    calls: list[dict[str, object]] = []

    def __init__(self, **_kwargs: object) -> None:
        type(self).calls = []

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def get(
        self,
        _url: str,
        *,
        params: dict[str, object],
        headers: dict[str, str],
    ) -> _FakeResponse:
        type(self).calls.append({"params": params, "headers": headers})
        query = str(params["query"])
        handle = query.split("from:", 1)[1].split(" ", 1)[0]
        return _FakeResponse(handle)


@pytest.mark.asyncio
async def test_search_x_accounts_queries_each_handle_and_returns_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.tools.x_accounts.httpx.AsyncClient", _FakeClient)
    context = SimpleNamespace(
        settings=SimpleNamespace(
            twitterapi_api_key="secret",
            twitterapi_search_url="https://api.twitterapi.io/twitter/tweet/advanced_search",
        )
    )

    result = await search_x_accounts(
        context,  # type: ignore[arg-type]
        handles=["sama", "karpathy", "AndrewYNg"],
        start_time="2026-10-18T06:00:00Z",
        end_time="2026-10-19T06:00:00Z",
        limit_per_account=1,
    )

    assert result["provider"] == "twitterapi_io"
    assert result["provider_status"] == "succeeded"
    assert result["requested_handles"] == ["sama", "karpathy", "AndrewYNg"]
    assert result["handles_with_results"] == ["sama", "karpathy", "AndrewYNg"]
    assert len(result["candidates"]) == 3
    assert all("X-API-Key" in call["headers"] for call in _FakeClient.calls)
    assert all("cursor" in call["params"] for call in _FakeClient.calls)
    assert "secret" not in json.dumps(result)


def _digest_payload(url: str = "https://example.com/news") -> dict[str, object]:
    return {
        "title": "X 关注账号简报",
        "digest_date": "2026-10-19",
        "coverage_start": "2026-10-18T06:00:00Z",
        "coverage_end": "2026-10-19T06:00:00Z",
        "overview": "这是用于验证 X 关注账号检索与简报强制收录策略的测试概览。",
        "items": [
            {
                "original_title": "A new AI Agent update",
                "generated_title": "关注账号发布新的 AI Agent 动态",
                "original_url": url,
                "canonical_url": url,
                "source_name": "@sama" if "x.com" in url else "Example",
                "published_at": "2026-10-19T05:00:00Z",
                "summary": "关注账号在覆盖时间内发布了一条与 AI Agent 有关的新动态。",
                "recommendation_reason": "来自用户主动关注的账号，具有较高的个性化价值。",
                "category": "LLM 与 AI Agent",
                "event_status": "待进一步核实",
                "keywords": ["AI Agent"],
                "relevance_score": 80,
                "evidence_reference": "TwitterAPI.io 返回的 X 原帖。",
            }
        ],
        "warnings": [],
    }


def test_digest_requires_search_and_content_for_x_accounts() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        user = User(email="digest@example.com", password_hash="not-used")
        db.add(user)
        db.flush()
        job = DigestJob(user_id=user.id, idempotency_key="x-watchlist-job")
        db.add(job)
        db.flush()
        run = AgentRun(
            job_id=job.id,
            user_id=user.id,
            model_name="test-model",
            preference_snapshot={"x_accounts": ["sama"]},
        )
        db.add(run)
        db.commit()
        context = SimpleNamespace(db=db, run_id=run.id)

        with pytest.raises(ValueError, match="尚未检索"):
            _enforce_x_watchlist(
                context,  # type: ignore[arg-type]
                DigestDraft.model_validate(_digest_payload()),
            )

        db.add(
            ToolCall(
                run_id=run.id,
                sequence_number=1,
                tool_name="search_x_accounts",
                status="SUCCEEDED",
                result_summary=json.dumps(
                    {
                        "requested_handles": ["sama"],
                        "handles_with_results": ["sama"],
                    }
                ),
            )
        )
        db.commit()

        with pytest.raises(ValueError, match="尚未纳入简报"):
            _enforce_x_watchlist(
                context,  # type: ignore[arg-type]
                DigestDraft.model_validate(_digest_payload()),
            )

        _enforce_x_watchlist(
            context,  # type: ignore[arg-type]
            DigestDraft.model_validate(_digest_payload("https://x.com/sama/status/123456")),
        )
