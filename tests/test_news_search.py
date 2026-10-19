import base64
from types import SimpleNamespace

import pytest

from app.tools.news import (
    PROXY_FAKE_IP_NETWORK,
    _decode_bing_result_url,
    _official_domains_for_query,
    _platform_domains,
    _platform_search_query,
    _query_terms,
    _score_candidate,
    _twitterapi_candidate,
    _twitterapi_query,
    _validate_public_url,
    _validate_search_endpoint,
    search_news,
)


class _EmptyScalars:
    def all(self) -> list[object]:
        return []


class _FakeDb:
    def scalars(self, _query: object) -> _EmptyScalars:
        return _EmptyScalars()


class _FakeResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return {
            "articles": [
                {
                    "title": "NVIDIA announces a new AI chip platform",
                    "url": "https://nvidia.com/news/new-ai-chip?utm_source=test",
                    "domain": "nvidia.com",
                    "seendate": "20261016T010000Z",
                    "language": "English",
                    "sourcecountry": "United States",
                }
            ]
        }


class _FakeClient:
    def __init__(self, **_kwargs: object) -> None:
        pass

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def get(self, _url: str, **_kwargs: object) -> _FakeResponse:
        return _FakeResponse()


def test_query_terms_supports_chinese_and_english() -> None:
    assert _query_terms("LLM，半导体 AI Agent") == ["llm", "半导体", "ai", "agent"]


def test_model_query_covers_requested_company_domains() -> None:
    domains = set(_official_domains_for_query("LLM Agent DeepSeek GLM Kimi MiniMax"))
    assert {
        "anthropic.com",
        "openai.com",
        "deepseek.com",
        "zhipuai.cn",
        "moonshot.cn",
        "minimax.io",
    } <= domains


def test_platform_domains_cover_requested_public_platforms() -> None:
    domains = set(_platform_domains(None))
    assert {"x.com", "reddit.com", "github.com", "chatgpt.com", "youtube.com"} <= domains


def test_platform_query_expands_topics_with_or() -> None:
    assert _platform_search_query("LLM Agent GPU") == "(llm OR agent OR gpu)"


def test_bing_redirect_decodes_to_original_platform_url() -> None:
    target = "https://github.com/example/ai-agent"
    encoded = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
    redirect = f"https://www.bing.com/ck/a?u=a1{encoded}"
    assert _decode_bing_result_url(redirect) == target


def test_search_endpoint_only_allows_configured_https_provider() -> None:
    _validate_search_endpoint("https://api.gdeltproject.org/api/v2/doc/doc")
    _validate_search_endpoint("https://api.twitterapi.io/twitter/tweet/advanced_search")
    with pytest.raises(ValueError, match="allowed HTTPS"):
        _validate_search_endpoint("http://127.0.0.1/search")


def test_proxy_fake_ip_range_is_narrow_and_does_not_include_private_networks() -> None:
    import ipaddress

    assert ipaddress.ip_address("198.18.0.50") in PROXY_FAKE_IP_NETWORK
    assert ipaddress.ip_address("127.0.0.1") not in PROXY_FAKE_IP_NETWORK
    assert ipaddress.ip_address("192.168.1.1") not in PROXY_FAKE_IP_NETWORK


@pytest.mark.asyncio
async def test_public_url_validation_requires_explicit_fake_ip_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.tools.news.socket.getaddrinfo",
        lambda *_args: [(2, 1, 6, "", ("198.18.0.50", 443))],
    )

    with pytest.raises(ValueError, match="non-public"):
        await _validate_public_url("https://openai.com")
    await _validate_public_url("https://openai.com", allow_proxy_fake_ip=True)


def test_twitterapi_query_adds_time_window_and_latest_safe_terms() -> None:
    from app.tools.news import _parse_iso

    query = _twitterapi_query(
        "LLM Agent",
        _parse_iso("2026-10-15T00:00:00+00:00"),
        _parse_iso("2026-10-17T00:00:00+00:00"),
    )
    assert query.startswith("(llm OR agent)")
    assert "since:2026-10-15" in query
    assert "until:2026-10-18" in query


def test_twitterapi_candidate_maps_public_post_without_exposing_credentials() -> None:
    from app.tools.news import _parse_iso

    candidate = _twitterapi_candidate(
        {
            "id": "123456",
            "text": "A new open-source AI agent framework is available.",
            "createdAt": "2026-10-16T08:00:00Z",
            "author": {"userName": "OpenSourceAI"},
            "likeCount": 42,
        },
        _parse_iso("2026-10-15T00:00:00+00:00"),
        _parse_iso("2026-10-17T00:00:00+00:00"),
    )
    assert candidate is not None
    assert candidate["provider"] == "twitterapi_io"
    assert candidate["url"] == "https://x.com/OpenSourceAI/status/123456"
    assert candidate["configured_trust"] == 2
    assert "api_key" not in candidate


def test_official_candidate_scores_above_unknown_source() -> None:
    start = "2026-10-15T00:00:00+00:00"
    end = "2026-10-17T00:00:00+00:00"
    common = {
        "title": "NVIDIA launches AI chip",
        "snippet": "NVIDIA launches a new AI chip platform",
        "published_at": "2026-10-16T00:00:00+00:00",
    }
    from app.tools.news import _parse_iso

    official = _score_candidate(
        {**common, "source_domain": "nvidia.com"},
        ["nvidia", "ai", "chip"],
        _parse_iso(start),
        _parse_iso(end),
    )
    unknown = _score_candidate(
        {**common, "source_domain": "example.net"},
        ["nvidia", "ai", "chip"],
        _parse_iso(start),
        _parse_iso(end),
    )
    assert official["source_tier"] == "A"
    assert official["score"] > unknown["score"]


def test_social_platform_candidate_is_low_trust() -> None:
    from app.tools.news import _parse_iso

    scored = _score_candidate(
        {
            "title": "Unverified model rumor",
            "snippet": "A social post about an AI model",
            "source_domain": "x.com",
            "configured_trust": 2,
        },
        ["model"],
        _parse_iso("2026-10-15T00:00:00+00:00"),
        _parse_iso("2026-10-17T00:00:00+00:00"),
    )
    assert scored["source_tier"] == "D"


@pytest.mark.asyncio
async def test_web_mode_returns_ranked_gdelt_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _allow_public_url(_url: str) -> None:
        return None

    monkeypatch.setattr("app.tools.news.httpx.AsyncClient", _FakeClient)
    monkeypatch.setattr("app.tools.news._validate_public_url", _allow_public_url)
    context = SimpleNamespace(
        db=_FakeDb(),
        settings=SimpleNamespace(
            web_search_provider="gdelt",
            gdelt_doc_api_url="https://api.gdeltproject.org/api/v2/doc/doc",
        ),
    )

    result = await search_news(
        context,  # type: ignore[arg-type]
        query="NVIDIA AI chip",
        start_time="2026-10-15T00:00:00+00:00",
        end_time="2026-10-17T00:00:00+00:00",
        mode="web",
    )

    assert result["count"] == 1
    assert result["candidates"][0]["provider"] == "gdelt_web_search"
    assert result["candidates"][0]["source_tier"] == "A"
    assert result["candidates"][0]["url"] == "https://nvidia.com/news/new-ai-chip"
