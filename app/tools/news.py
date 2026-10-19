from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import math
import re
import socket
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from time import perf_counter
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

import feedparser
import httpx
from bs4 import BeautifulSoup
from sqlalchemy import or_, select

from app.models import Article, Digest, DigestItem, SourceConfig
from app.normalization import canonicalize_url
from app.tools.registry import ToolContext

DEFAULT_FEEDS = [
    {
        "name": "OpenAI News",
        "url": "https://openai.com/news/rss.xml",
        "language": "en",
        "category": "LLM 与 AI Agent",
    },
    {
        "name": "NVIDIA Blog",
        "url": "https://blogs.nvidia.com/feed/",
        "language": "en",
        "category": "半导体与芯片",
    },
    {
        "name": "arXiv cs.AI",
        "url": "https://export.arxiv.org/rss/cs.AI",
        "language": "en",
        "category": "研究与开源",
    },
    {
        "name": "MIT Technology Review",
        "url": "https://www.technologyreview.com/feed/",
        "language": "en",
        "category": "产业、资本与政策",
    },
    {
        "name": "TechCrunch AI",
        "url": "https://techcrunch.com/category/artificial-intelligence/feed/",
        "language": "en",
        "category": "LLM 与 AI Agent",
    },
]

USER_AGENT = "AINewsAgent/0.1 (+local personal digest)"

# A 级来源可以作为单一官方证据；B 级来源适合与官方材料或另一家独立媒体交叉验证。
OFFICIAL_DOMAINS = {
    "anthropic.com",
    "amd.com",
    "arxiv.org",
    "baidu.com",
    "bigmodel.cn",
    "blog.google",
    "chatglm.cn",
    "deepseek.com",
    "deepmind.google",
    "huggingface.co",
    "intel.com",
    "kimi.com",
    "meta.com",
    "microsoft.com",
    "minimax.io",
    "minimaxi.com",
    "miit.gov.cn",
    "moonshot.cn",
    "ndrc.gov.cn",
    "nhtsa.gov",
    "nvidia.com",
    "openai.com",
    "qualcomm.com",
    "sec.gov",
    "tesla.com",
    "tsmc.com",
    "waymo.com",
    "zhipuai.cn",
}
REPUTABLE_NEWS_DOMAINS = {
    "apnews.com",
    "bbc.com",
    "bloomberg.com",
    "cnbc.com",
    "ft.com",
    "ieee.org",
    "reuters.com",
    "technologyreview.com",
    "techcrunch.com",
    "theverge.com",
    "wsj.com",
}
PLATFORM_DOMAINS = {
    "x": ["x.com", "twitter.com"],
    "reddit": ["reddit.com"],
    "github": ["github.com"],
    "chatgpt": ["chatgpt.com"],
    "youtube": ["youtube.com", "youtu.be"],
}
PLATFORM_TRUST_LEVELS = {
    "x": 2,
    "reddit": 2,
    "github": 3,
    "chatgpt": 1,
    "youtube": 2,
}
ALLOWED_SEARCH_API_HOSTS = {
    "api.gdeltproject.org",
    "api.github.com",
    "html.duckduckgo.com",
    "www.reddit.com",
    "www.bing.com",
    "api.twitterapi.io",
}
QUERY_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9.+#-]*|[\u4e00-\u9fff]{2,}", re.IGNORECASE)
PROXY_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")


def _entry_datetime(entry: Any) -> datetime | None:
    for field in ("published", "updated", "created"):
        raw = entry.get(field)
        if raw:
            try:
                value = parsedate_to_datetime(raw)
                return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
            except (TypeError, ValueError, OverflowError):
                continue
    return None


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _parse_gdelt_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    for pattern in ("%Y%m%dT%H%M%SZ", "%Y%m%d%H%M%S"):
        try:
            return datetime.strptime(value, pattern).replace(tzinfo=UTC)
        except ValueError:
            continue
    try:
        return _parse_iso(value)
    except ValueError:
        return None


def _query_terms(query: str) -> list[str]:
    return list(dict.fromkeys(match.group(0).lower() for match in QUERY_TOKEN_RE.finditer(query)))


def _domain_matches(domain: str, choices: set[str]) -> bool:
    normalized = domain.lower().removeprefix("www.").split(":", 1)[0]
    return any(normalized == item or normalized.endswith(f".{item}") for item in choices)


def _source_quality(domain: str, configured_trust: int | None = None) -> tuple[str, float]:
    if _domain_matches(domain, OFFICIAL_DOMAINS):
        return "A", 100.0
    if _domain_matches(domain, REPUTABLE_NEWS_DOMAINS):
        return "B", 85.0
    if configured_trust is not None:
        score = max(20.0, min(100.0, configured_trust * 20.0))
        tier = "A" if score >= 90 else "B" if score >= 70 else "C" if score >= 50 else "D"
        return tier, score
    return "C", 55.0


def _platform_for_domain(domain: str) -> str | None:
    for platform, platform_domains in PLATFORM_DOMAINS.items():
        if _domain_matches(domain, set(platform_domains)):
            return platform
    return None


def _score_candidate(
    item: dict[str, Any],
    terms: list[str],
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    haystack = f"{item.get('title', '')} {item.get('snippet', '')}".lower()
    matched = sum(1 for term in terms if term in haystack)
    relevance = 100.0 if not terms else 100.0 * matched / len(terms)

    published_at = item.get("published_at")
    freshness = 45.0
    if published_at:
        try:
            published = _parse_iso(str(published_at))
            window_seconds = max((end - start).total_seconds(), 1.0)
            age_seconds = max((end - published).total_seconds(), 0.0)
            freshness = max(0.0, 100.0 * (1.0 - age_seconds / window_seconds))
        except ValueError:
            pass

    tier, trust = _source_quality(
        str(item.get("source_domain", item.get("source", ""))), item.get("configured_trust")
    )
    completeness = 40.0
    if item.get("title"):
        completeness += 20.0
    if item.get("snippet"):
        completeness += 20.0
    if item.get("published_at"):
        completeness += 20.0
    preference_match = relevance
    final_score = (
        0.30 * relevance
        + 0.25 * freshness
        + 0.20 * trust
        + 0.15 * completeness
        + 0.10 * preference_match
    )
    item["source_tier"] = tier
    item["score"] = round(final_score, 2)
    item["score_breakdown"] = {
        "topic_relevance": round(relevance, 2),
        "freshness": round(freshness, 2),
        "source_credibility": round(trust, 2),
        "information_completeness": round(completeness, 2),
        "preference_match": round(preference_match, 2),
    }
    return item


def _official_domains_for_query(query: str) -> list[str]:
    lowered = query.lower()
    selected: list[str] = []
    groups = {
        (
            "llm",
            "model",
            "agent",
            "openai",
            "anthropic",
            "deepseek",
            "glm",
            "kimi",
            "minimax",
            "大模型",
            "智能体",
            "月之暗面",
            "智谱",
        ): [
            "openai.com",
            "anthropic.com",
            "deepseek.com",
            "zhipuai.cn",
            "bigmodel.cn",
            "chatglm.cn",
            "moonshot.cn",
            "kimi.com",
            "minimax.io",
            "minimaxi.com",
            "deepmind.google",
            "microsoft.com",
            "meta.com",
            "huggingface.co",
        ],
        ("chip", "semiconductor", "gpu", "nvidia", "芯片", "半导体", "英伟达"): [
            "nvidia.com",
            "amd.com",
            "intel.com",
            "qualcomm.com",
            "tsmc.com",
        ],
        ("policy", "regulation", "产业政策", "监管", "政策"): [
            "miit.gov.cn",
            "ndrc.gov.cn",
            "sec.gov",
        ],
        ("autonomous", "robotaxi", "driving", "自动驾驶", "无人驾驶"): [
            "waymo.com",
            "tesla.com",
            "nhtsa.gov",
        ],
    }
    for needles, domains in groups.items():
        if any(needle in lowered for needle in needles):
            selected.extend(domains)
    if not selected:
        selected = [
            "openai.com",
            "anthropic.com",
            "deepseek.com",
            "zhipuai.cn",
            "moonshot.cn",
            "minimax.io",
            "deepmind.google",
            "nvidia.com",
        ]
    return list(dict.fromkeys(selected))[:14]


def _platform_domains(platforms: list[str] | None) -> list[str]:
    selected_platforms = platforms or list(PLATFORM_DOMAINS)
    domains: list[str] = []
    for platform in selected_platforms:
        if platform not in PLATFORM_DOMAINS:
            raise ValueError(f"unsupported platform: {platform}")
        domains.extend(PLATFORM_DOMAINS[platform])
    return list(dict.fromkeys(domains))


def _platform_search_query(query: str) -> str:
    terms = _query_terms(query)[:8]
    if len(terms) <= 1:
        return query.strip()
    return f"({' OR '.join(terms)})"


def _official_search_query(query: str) -> str:
    return f"{query.strip()} (news OR announcement OR release OR blog OR 新闻 OR 发布 OR 更新)"


def _decode_bing_result_url(url: str) -> str:
    parsed = urlsplit(url)
    if not _domain_matches(parsed.netloc, {"bing.com"}):
        return url
    encoded = parse_qs(parsed.query).get("u", [""])[0]
    if not encoded.startswith("a1"):
        return url
    payload = encoded[2:]
    payload += "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return url
    return decoded if urlsplit(decoded).scheme in {"http", "https"} else url


def _validate_search_endpoint(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in ALLOWED_SEARCH_API_HOSTS
        or parsed.port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("web search endpoint is not an allowed HTTPS provider")


async def _search_github(
    client: httpx.AsyncClient,
    endpoint: str,
    query: str,
    start: datetime,
    end: datetime,
    limit: int,
) -> list[dict[str, Any]]:
    _validate_search_endpoint(endpoint)
    github_query = " ".join(_query_terms(query)[:6])
    github_query = f"{github_query} pushed:>={start.date().isoformat()}"
    response = await client.get(
        endpoint,
        params={
            "q": github_query,
            "sort": "updated",
            "order": "desc",
            "per_page": min(limit, 30),
        },
        headers={"Accept": "application/vnd.github+json"},
    )
    response.raise_for_status()
    payload = response.json()
    items = payload.get("items", []) if isinstance(payload, dict) else []
    candidates: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        link = str(item.get("html_url", "")).strip()
        full_name = str(item.get("full_name", "")).strip()
        pushed_at = _parse_gdelt_datetime(str(item.get("pushed_at", "")))
        if not link or not full_name or (pushed_at and not (start <= pushed_at <= end)):
            continue
        description = str(item.get("description") or "").strip()
        candidates.append(
            {
                "title": f"GitHub 项目更新：{full_name}",
                "url": canonicalize_url(link),
                "source": "github.com",
                "source_domain": "github.com",
                "published_at": pushed_at.isoformat() if pushed_at else None,
                "snippet": description[:1000],
                "language": "unknown",
                "source_category": "研究与开源",
                "provider": "github_api",
                "platform": "github",
                "configured_trust": PLATFORM_TRUST_LEVELS["github"],
                "popularity": {"stars": int(item.get("stargazers_count") or 0)},
            }
        )
    return candidates


async def _search_reddit(
    client: httpx.AsyncClient,
    endpoint: str,
    query: str,
    start: datetime,
    end: datetime,
    limit: int,
) -> list[dict[str, Any]]:
    _validate_search_endpoint(endpoint)
    window_days = max(1, math.ceil((end - start).total_seconds() / 86400))
    period = "day" if window_days <= 2 else "week" if window_days <= 7 else "month"
    response = await client.get(
        endpoint,
        params={
            "q": _platform_search_query(query),
            "sort": "new",
            "t": period,
            "limit": min(limit, 50),
        },
    )
    response.raise_for_status()
    parsed = feedparser.parse(response.content)
    candidates: list[dict[str, Any]] = []
    for entry in parsed.entries:
        title = str(entry.get("title", "")).strip()
        link = str(entry.get("link", "")).strip()
        published_at = _entry_datetime(entry)
        if not title or not link or (published_at and not (start <= published_at <= end)):
            continue
        summary_html = str(entry.get("summary", entry.get("description", "")))
        summary = BeautifulSoup(summary_html, "html.parser").get_text(" ", strip=True)
        candidates.append(
            {
                "title": title,
                "url": canonicalize_url(link),
                "source": "reddit.com",
                "source_domain": "reddit.com",
                "published_at": published_at.isoformat() if published_at else None,
                "snippet": summary[:1000],
                "language": "unknown",
                "source_category": "社区平台",
                "provider": "reddit_search_rss",
                "platform": "reddit",
                "configured_trust": PLATFORM_TRUST_LEVELS["reddit"],
            }
        )
    return candidates


def _parse_twitterapi_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return _parse_iso(value)
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.strptime(value, "%a %b %d %H:%M:%S %z %Y")
        except ValueError:
            return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _twitterapi_query(query: str, start: datetime, end: datetime) -> str:
    until = (end + timedelta(days=1)).date().isoformat()
    return (
        f"{_platform_search_query(query)} "
        f"since:{start.date().isoformat()} until:{until} -filter:replies"
    )


def _twitterapi_candidate(
    tweet: dict[str, Any], start: datetime, end: datetime
) -> dict[str, Any] | None:
    tweet_id = str(tweet.get("id") or tweet.get("tweetId") or "").strip()
    text = str(tweet.get("text") or tweet.get("fullText") or "").strip()
    if not tweet_id or not text:
        return None
    author_value = tweet.get("author")
    author: dict[str, Any] = author_value if isinstance(author_value, dict) else {}
    username = (
        str(author.get("userName") or author.get("username") or tweet.get("userName") or "")
        .strip()
        .lstrip("@")
    )
    link = str(
        tweet.get("url")
        or tweet.get("twitterUrl")
        or tweet.get("tweet_url")
        or (
            f"https://x.com/{username}/status/{tweet_id}"
            if username
            else f"https://x.com/i/web/status/{tweet_id}"
        )
    ).strip()
    published_at = _parse_twitterapi_datetime(
        str(tweet.get("createdAt") or tweet.get("created_at") or tweet.get("date") or "")
    )
    if published_at and not (start <= published_at <= end):
        return None
    compact_text = " ".join(text.split())
    author_label = f"@{username}" if username else "X"

    def count_value(*names: str) -> int:
        for name in names:
            try:
                return int(tweet.get(name) or 0)
            except (TypeError, ValueError):
                continue
        return 0

    return {
        "title": f"{author_label}：{compact_text[:140]}",
        "url": canonicalize_url(link),
        "source": author_label,
        "source_domain": "x.com",
        "published_at": published_at.isoformat() if published_at else None,
        "date_unverified": published_at is None,
        "snippet": compact_text[:1000],
        "language": str(tweet.get("lang") or "unknown"),
        "source_category": "社区平台",
        "provider": "twitterapi_io",
        "platform": "x",
        "configured_trust": PLATFORM_TRUST_LEVELS["x"],
        "popularity": {
            "likes": count_value("likeCount", "favorite_count"),
            "reposts": count_value("retweetCount", "retweet_count"),
            "replies": count_value("replyCount", "reply_count"),
            "views": count_value("viewCount", "view_count"),
        },
    }


async def _search_twitterapi(
    client: httpx.AsyncClient,
    endpoint: str,
    api_key: str,
    query: str,
    start: datetime,
    end: datetime,
    limit: int,
) -> list[dict[str, Any]]:
    _validate_search_endpoint(endpoint)
    response = await client.get(
        endpoint,
        params={
            "query": _twitterapi_query(query, start, end),
            "queryType": "Latest",
        },
        headers={"X-API-Key": api_key},
    )
    response.raise_for_status()
    payload = response.json()
    tweets = payload.get("tweets", []) if isinstance(payload, dict) else []
    candidates: list[dict[str, Any]] = []
    for tweet in tweets:
        if not isinstance(tweet, dict):
            continue
        candidate = _twitterapi_candidate(tweet, start, end)
        if candidate is not None:
            candidates.append(candidate)
        if len(candidates) >= limit:
            break
    return candidates


def _configured_feeds(context: ToolContext) -> list[dict[str, Any]]:
    rows = context.db.scalars(
        select(SourceConfig).where(
            SourceConfig.enabled.is_(True), SourceConfig.provider_type == "rss"
        )
    ).all()
    if not rows:
        return DEFAULT_FEEDS
    return [
        {
            "name": row.name,
            "url": row.base_url,
            "language": row.language,
            "category": row.category,
            "trust_level": row.trust_level,
        }
        for row in rows
    ]


@dataclass(slots=True)
class _ProviderResult:
    name: str
    candidates: list[dict[str, Any]]
    errors: list[str]
    duration_ms: int

    def metric(self) -> dict[str, Any]:
        status = "succeeded"
        if self.errors:
            status = "partial" if self.candidates else "failed"
        return {
            "provider": self.name,
            "status": status,
            "duration_ms": self.duration_ms,
            "candidate_count": len(self.candidates),
            "error_count": len(self.errors),
        }


async def _candidate_operation(
    operation: Awaitable[list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], list[str]]:
    return await operation, []


async def _timed_provider(
    name: str,
    operation: Awaitable[tuple[list[dict[str, Any]], list[str]]],
) -> _ProviderResult:
    started = perf_counter()
    try:
        candidates, errors = await operation
    except (httpx.HTTPError, OSError, ValueError, TypeError) as exc:
        candidates = []
        errors = [f"{name}: {type(exc).__name__}: {str(exc)[:160]}"]
    return _ProviderResult(
        name=name,
        candidates=candidates,
        errors=errors,
        duration_ms=int((perf_counter() - started) * 1000),
    )


async def _search_rss_feed(
    client: httpx.AsyncClient,
    feed: dict[str, Any],
    terms: list[str],
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    response = await client.get(feed["url"])
    response.raise_for_status()
    parsed = feedparser.parse(response.content)
    candidates: list[dict[str, Any]] = []
    for entry in parsed.entries:
        title = str(entry.get("title", "")).strip()
        link = str(entry.get("link", "")).strip()
        summary_html = str(entry.get("summary", entry.get("description", "")))
        summary = BeautifulSoup(summary_html, "html.parser").get_text(" ", strip=True)
        published_at = _entry_datetime(entry)
        haystack = f"{title} {summary}".lower()
        matches = sum(term in haystack for term in terms)
        required_matches = max(1, math.ceil(len(terms) * 0.4))
        if matches < required_matches:
            continue
        if published_at and not (start <= published_at <= end):
            continue
        if not title or not link:
            continue
        feed_domain = urlsplit(feed["url"]).netloc
        candidates.append(
            {
                "title": title,
                "url": canonicalize_url(link),
                "source": feed["name"],
                "source_domain": feed_domain,
                "published_at": published_at.isoformat() if published_at else None,
                "snippet": summary[:1000],
                "language": feed["language"],
                "source_category": feed["category"],
                "provider": "rss",
                "configured_trust": int(feed.get("trust_level", 4)),
            }
        )
    return candidates


async def _search_gdelt_provider(
    client: httpx.AsyncClient,
    endpoint: str,
    query: str,
    start: datetime,
    end: datetime,
    requested_domains: list[str],
    safe_domains: list[str],
    language: str | None,
    mode: str,
    limit: int,
) -> list[dict[str, Any]]:
    _validate_search_endpoint(endpoint)
    gdelt_query = query.strip()
    if safe_domains:
        domain_query = " OR ".join(f"domain:{domain}" for domain in safe_domains[:8])
        gdelt_query = f"{gdelt_query} ({domain_query})"
    language_names = {"en": "english", "zh": "chinese"}
    if language in language_names:
        gdelt_query = f"{gdelt_query} sourcelang:{language_names[language]}"
    response = await client.get(
        endpoint,
        params={
            "query": gdelt_query,
            "mode": "ArtList",
            "format": "json",
            "maxrecords": min(max(limit * 3, 25), 250),
            "sort": "DateDesc",
            "startdatetime": start.strftime("%Y%m%d%H%M%S"),
            "enddatetime": end.strftime("%Y%m%d%H%M%S"),
        },
    )
    response.raise_for_status()
    payload = response.json()
    articles = payload.get("articles", []) if isinstance(payload, dict) else []
    candidates: list[dict[str, Any]] = []
    for article in articles:
        if not isinstance(article, dict):
            continue
        title = BeautifulSoup(str(article.get("title", "")), "html.parser").get_text(
            " ", strip=True
        )
        link = str(article.get("url", "")).strip()
        domain = str(article.get("domain") or urlsplit(link).netloc)
        published_at = _parse_gdelt_datetime(str(article.get("seendate", "")))
        if not title or not link:
            continue
        if published_at and not (start <= published_at <= end):
            continue
        if mode == "official" and not _domain_matches(domain, set(requested_domains)):
            continue
        candidates.append(
            {
                "title": title,
                "url": canonicalize_url(link),
                "source": domain,
                "source_domain": domain,
                "published_at": published_at.isoformat() if published_at else None,
                "snippet": title,
                "language": str(article.get("language", language or "unknown")),
                "source_category": (
                    "官方来源" if _domain_matches(domain, OFFICIAL_DOMAINS) else "联网新闻"
                ),
                "provider": "gdelt_web_search",
                "source_country": article.get("sourcecountry"),
            }
        )
    return candidates


async def _search_duckduckgo_provider(
    client: httpx.AsyncClient,
    endpoint: str,
    query: str,
    safe_domains: list[str],
    language: str | None,
    mode: str,
    limit: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    _validate_search_endpoint(endpoint)
    if mode == "platform":
        search_query_text = _platform_search_query(query)
    elif mode == "official":
        search_query_text = _official_search_query(query)
    else:
        search_query_text = query.strip()
    ddg_queries = [search_query_text]
    if mode in {"official", "platform"} and safe_domains:
        ddg_queries = [f"{search_query_text} site:{domain}" for domain in safe_domains]
    elif safe_domains:
        site_query = " OR ".join(f"site:{domain}" for domain in safe_domains)
        ddg_queries = [f"{search_query_text} ({site_query})"]

    semaphore = asyncio.Semaphore(4)

    async def fetch_search_page(ddg_query: str) -> httpx.Response:
        async with semaphore:
            return await client.get(endpoint, params={"q": ddg_query, "kl": "wt-wt"})

    responses: list[Any] = await asyncio.gather(
        *(fetch_search_page(ddg_query) for ddg_query in ddg_queries),
        return_exceptions=True,
    )
    candidates: list[dict[str, Any]] = []
    errors: list[str] = []
    per_query_limit = (
        max(2, math.ceil(limit / len(ddg_queries))) if len(ddg_queries) > 1 else limit * 2
    )
    for response in responses:
        if isinstance(response, BaseException):
            errors.append(f"web/duckduckgo: {type(response).__name__}: {str(response)[:120]}")
            continue
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            errors.append(f"web/duckduckgo: {type(exc).__name__}: {str(exc)[:120]}")
            continue
        soup = BeautifulSoup(response.text, "html.parser")
        query_result_count = 0
        for result_node in soup.select(".result"):
            anchor = result_node.select_one("a.result__a")
            if anchor is None:
                continue
            title = anchor.get_text(" ", strip=True)
            link = str(anchor.get("href", "")).strip()
            if link.startswith("//"):
                link = f"https:{link}"
            redirect = urlsplit(link)
            if _domain_matches(redirect.netloc, {"duckduckgo.com"}):
                target = parse_qs(redirect.query).get("uddg", [])
                if target:
                    link = target[0]
            parsed_link = urlsplit(link)
            domain = parsed_link.netloc.lower().removeprefix("www.")
            if parsed_link.scheme not in {"http", "https"} or not domain:
                continue
            if safe_domains and not _domain_matches(domain, set(safe_domains)):
                continue
            platform_name = _platform_for_domain(domain)
            snippet_node = result_node.select_one(".result__snippet")
            snippet = snippet_node.get_text(" ", strip=True) if snippet_node is not None else ""
            candidates.append(
                {
                    "title": title,
                    "url": canonicalize_url(link),
                    "source": domain,
                    "source_domain": domain,
                    "published_at": None,
                    "date_unverified": True,
                    "snippet": snippet[:1000],
                    "language": language or "unknown",
                    "source_category": (
                        "社区平台"
                        if platform_name
                        else "官方来源"
                        if _domain_matches(domain, OFFICIAL_DOMAINS)
                        else "联网网页"
                    ),
                    "provider": (
                        "duckduckgo_platform_search" if platform_name else "duckduckgo_web_search"
                    ),
                    "platform": platform_name,
                    "configured_trust": (
                        PLATFORM_TRUST_LEVELS[platform_name] if platform_name else None
                    ),
                }
            )
            query_result_count += 1
            if query_result_count >= per_query_limit:
                break
    return candidates, errors


async def _search_bing_provider(
    endpoint: str,
    query: str,
    safe_domains: list[str],
    language: str | None,
    limit: int,
    timeout: httpx.Timeout,
) -> tuple[list[dict[str, Any]], list[str]]:
    _validate_search_endpoint(endpoint)
    search_query_text = _platform_search_query(query)
    bing_queries = [f"{search_query_text} site:{domain}" for domain in safe_domains]
    candidates: list[dict[str, Any]] = []
    errors: list[str] = []
    async with httpx.AsyncClient(
        timeout=timeout, headers={"User-Agent": "Mozilla/5.0"}
    ) as bing_client:
        semaphore = asyncio.Semaphore(4)

        async def fetch_bing_page(bing_query: str) -> httpx.Response:
            async with semaphore:
                return await bing_client.get(endpoint, params={"q": bing_query, "count": 15})

        responses: list[Any] = await asyncio.gather(
            *(fetch_bing_page(query_text) for query_text in bing_queries),
            return_exceptions=True,
        )
    per_query_limit = max(2, math.ceil(limit / max(len(bing_queries), 1)))
    for response in responses:
        if isinstance(response, BaseException):
            errors.append(f"web/bing: {type(response).__name__}: {str(response)[:120]}")
            continue
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            errors.append(f"web/bing: {type(exc).__name__}: {str(exc)[:120]}")
            continue
        response.encoding = "utf-8"
        soup = BeautifulSoup(response.text, "html.parser")
        query_result_count = 0
        for result_node in soup.select("li.b_algo"):
            anchor = result_node.select_one("h2 a")
            if anchor is None:
                continue
            title = anchor.get_text(" ", strip=True)
            link = _decode_bing_result_url(str(anchor.get("href", "")).strip())
            parsed_link = urlsplit(link)
            domain = parsed_link.netloc.lower().removeprefix("www.")
            platform_name = _platform_for_domain(domain)
            if parsed_link.scheme not in {"http", "https"} or not domain or platform_name is None:
                continue
            snippet_node = result_node.select_one(".b_caption p")
            snippet = snippet_node.get_text(" ", strip=True) if snippet_node is not None else ""
            candidates.append(
                {
                    "title": title,
                    "url": canonicalize_url(link),
                    "source": domain,
                    "source_domain": domain,
                    "published_at": None,
                    "date_unverified": True,
                    "snippet": snippet[:1000],
                    "language": language or "unknown",
                    "source_category": "社区平台",
                    "provider": "bing_platform_search",
                    "platform": platform_name,
                    "configured_trust": PLATFORM_TRUST_LEVELS[platform_name],
                }
            )
            query_result_count += 1
            if query_result_count >= per_query_limit:
                break
    return candidates, errors


async def search_news(
    context: ToolContext,
    query: str,
    start_time: str,
    end_time: str,
    sources: list[str] | None = None,
    domains: list[str] | None = None,
    platforms: list[str] | None = None,
    language: str | None = None,
    mode: str = "hybrid",
    limit: int = 20,
) -> dict[str, Any]:
    start = _parse_iso(start_time)
    end = _parse_iso(end_time)
    if start >= end:
        raise ValueError("start_time must be earlier than end_time")
    if mode not in {"hybrid", "rss", "web", "official", "platform"}:
        raise ValueError("mode must be one of: hybrid, rss, web, official, platform")
    terms = _query_terms(query)
    if not terms:
        raise ValueError("query must contain searchable words")
    wanted_sources = {source.lower() for source in (sources or [])}
    feeds = [
        feed
        for feed in _configured_feeds(context)
        if (not wanted_sources or feed["name"].lower() in wanted_sources)
        and (not language or feed["language"] == language)
    ]
    candidates: list[dict[str, Any]] = []
    errors: list[str] = []
    provider_metrics: list[dict[str, Any]] = []
    timeout = httpx.Timeout(15.0, connect=8.0)
    async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": USER_AGENT}) as client:
        searches: list[Awaitable[_ProviderResult]] = []
        searches_consumed = False
        if mode in {"hybrid", "rss"}:
            searches.extend(
                _timed_provider(
                    f"rss/{feed['name']}",
                    _candidate_operation(_search_rss_feed(client, feed, terms, start, end)),
                )
                for feed in feeds
            )

        if mode in {"hybrid", "web", "official", "platform"}:
            provider = context.settings.web_search_provider
            if provider not in {"auto", "gdelt", "duckduckgo"}:
                errors.append(f"web: unsupported provider {provider!r}")
            else:
                requested_domains = list(domains or [])
                if mode == "official" and not requested_domains:
                    requested_domains = _official_domains_for_query(query)
                if mode == "platform" and not requested_domains:
                    requested_domains = _platform_domains(platforms)
                safe_domains = [
                    domain.lower().removeprefix("www.")
                    for domain in requested_domains[:14]
                    if re.fullmatch(r"[a-z0-9.-]+", domain.lower().removeprefix("www."))
                ]
                selected_platforms = platforms or list(PLATFORM_DOMAINS)
                if mode == "platform":
                    if "x" in selected_platforms and context.settings.twitterapi_api_key:
                        searches.append(
                            _timed_provider(
                                "platform/twitterapi_io",
                                _candidate_operation(
                                    _search_twitterapi(
                                        client,
                                        context.settings.twitterapi_search_url,
                                        context.settings.twitterapi_api_key,
                                        query,
                                        start,
                                        end,
                                        limit,
                                    )
                                ),
                            )
                        )
                    if "github" in selected_platforms:
                        searches.append(
                            _timed_provider(
                                "platform/github_api",
                                _candidate_operation(
                                    _search_github(
                                        client,
                                        context.settings.github_search_url,
                                        query,
                                        start,
                                        end,
                                        limit,
                                    )
                                ),
                            )
                        )
                    if "reddit" in selected_platforms:
                        searches.append(
                            _timed_provider(
                                "platform/reddit_search_rss",
                                _candidate_operation(
                                    _search_reddit(
                                        client,
                                        context.settings.reddit_search_url,
                                        query,
                                        start,
                                        end,
                                        limit,
                                    )
                                ),
                            )
                        )

                if provider in {"auto", "gdelt"} and mode != "platform":
                    searches.append(
                        _timed_provider(
                            "web/gdelt",
                            _candidate_operation(
                                _search_gdelt_provider(
                                    client,
                                    context.settings.gdelt_doc_api_url,
                                    query,
                                    start,
                                    end,
                                    requested_domains,
                                    safe_domains,
                                    language,
                                    mode,
                                    limit,
                                )
                            ),
                        )
                    )

                if provider in {"auto", "duckduckgo"} or mode == "platform":
                    searches.append(
                        _timed_provider(
                            "web/duckduckgo",
                            _search_duckduckgo_provider(
                                client,
                                context.settings.duckduckgo_search_url,
                                query,
                                safe_domains,
                                language,
                                mode,
                                limit,
                            ),
                        )
                    )

                initial_results = await asyncio.gather(*searches)
                searches_consumed = True
                for provider_result in initial_results:
                    candidates.extend(provider_result.candidates)
                    errors.extend(provider_result.errors)
                    provider_metrics.append(provider_result.metric())

                if mode == "platform" and len(candidates) < limit:
                    bing_result = await _timed_provider(
                        "web/bing",
                        _search_bing_provider(
                            context.settings.bing_search_url,
                            query,
                            safe_domains,
                            language,
                            limit,
                            timeout,
                        ),
                    )
                    candidates.extend(bing_result.candidates)
                    errors.extend(bing_result.errors)
                    provider_metrics.append(bing_result.metric())

        if searches and not searches_consumed:
            remaining_results = await asyncio.gather(*searches)
            for provider_result in remaining_results:
                candidates.extend(provider_result.candidates)
                errors.extend(provider_result.errors)
                provider_metrics.append(provider_result.metric())

    unique: dict[str, dict[str, Any]] = {}
    for raw_item in candidates:
        item = _score_candidate(raw_item, terms, start, end)
        existing = unique.get(item["url"])
        if existing is None or item["score"] > existing["score"]:
            unique[item["url"]] = item
    ranked = sorted(
        unique.values(),
        key=lambda candidate: (candidate["score"], candidate["published_at"] or ""),
        reverse=True,
    )
    return {
        "query": query,
        "mode": mode,
        "count": min(len(ranked), limit),
        "total_unique_candidates": len(ranked),
        "candidates": ranked[:limit],
        "source_errors": errors,
        "provider_metrics": provider_metrics,
        "verification_note": (
            "搜索结果只是发现线索；A级官方来源可直接引用，其他来源的重要事实应通过"
            "fetch_article 和至少一个独立来源交叉核验。"
        ),
    }


async def _validate_public_url(url: str, allow_proxy_fake_ip: bool = False) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("only public HTTP(S) URLs are allowed")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    records = await asyncio.to_thread(socket.getaddrinfo, parsed.hostname, port)
    for record in records:
        address = ipaddress.ip_address(record[4][0])
        proxy_fake_ip = allow_proxy_fake_ip and address in PROXY_FAKE_IP_NETWORK
        if not address.is_global and not proxy_fake_ip:
            raise ValueError("URL resolves to a non-public address")


async def fetch_article(context: ToolContext, url: str, max_chars: int = 20000) -> dict[str, Any]:
    current_url = url
    timeout = httpx.Timeout(20.0, connect=8.0)
    async with httpx.AsyncClient(
        timeout=timeout, headers={"User-Agent": USER_AGENT}, follow_redirects=False
    ) as client:
        for _ in range(4):
            await _validate_public_url(
                current_url,
                allow_proxy_fake_ip=context.settings.allow_proxy_fake_ip,
            )
            response = await client.get(current_url)
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise ValueError("redirect response has no location")
                current_url = urljoin(current_url, location)
                continue
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            if "html" not in content_type:
                raise ValueError("URL did not return HTML")
            if len(response.content) > 5_000_000:
                raise ValueError("article response is too large")
            break
        else:
            raise ValueError("too many redirects")

    soup = BeautifulSoup(response.text, "html.parser")
    og_title_meta = soup.find("meta", attrs={"property": "og:title"})
    description_meta = (
        soup.find("meta", attrs={"property": "og:description"})
        or soup.find("meta", attrs={"name": "description"})
        or soup.find("meta", attrs={"name": "twitter:description"})
    )
    description = str(description_meta.get("content", "")).strip() if description_meta else ""
    author_meta = soup.find("meta", attrs={"name": "author"})
    published_meta = (
        soup.find("meta", attrs={"property": "article:published_time"})
        or soup.find("meta", attrs={"name": "date"})
        or soup.find("meta", attrs={"itemprop": "datePublished"})
    )
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form", "noscript"]):
        tag.decompose()
    canonical_tag = soup.find("link", rel=lambda value: value and "canonical" in value)
    canonical = current_url
    if canonical_tag and canonical_tag.get("href"):
        canonical = urljoin(current_url, str(canonical_tag["href"]))
    title = (
        str(og_title_meta.get("content", "")).strip()
        if og_title_meta
        else soup.title.get_text(" ", strip=True)
        if soup.title
        else ""
    )
    heading = soup.find("h1")
    if heading:
        title = heading.get_text(" ", strip=True) or title
    article = soup.find("article") or soup.find("main") or soup.body
    content = article.get_text("\n", strip=True) if article else ""
    lines = [line.strip() for line in content.splitlines() if len(line.strip()) > 20]
    cleaned = "\n".join(lines)[:max_chars]
    fetch_status = "success"
    if len(cleaned) < 200 and description:
        cleaned = f"{title}\n{description}"[:max_chars]
        fetch_status = "metadata_only"
    if len(cleaned) < 80:
        raise ValueError("article body and metadata are too short")
    published_at = None
    if published_meta and published_meta.get("content"):
        try:
            published_at = _parse_iso(str(published_meta["content"]))
        except ValueError:
            pass
    return {
        "url": url,
        "canonical_url": canonicalize_url(canonical),
        "title": title[:1000],
        "author": str(author_meta.get("content"))[:256] if author_meta else None,
        "source": urlsplit(current_url).netloc,
        "published_at": published_at.isoformat() if published_at else None,
        "content": cleaned,
        "content_hash": hashlib.sha256(cleaned.encode("utf-8")).hexdigest(),
        "fetch_status": fetch_status,
    }


def query_news_history(
    context: ToolContext,
    urls: list[str] | None = None,
    title_hashes: list[str] | None = None,
    keywords: list[str] | None = None,
    days: int = 30,
) -> dict[str, Any]:
    cutoff = datetime.now(UTC).timestamp() - days * 86400
    cutoff_dt = datetime.fromtimestamp(cutoff, tz=UTC)
    query = (
        select(Article, Digest)
        .join(DigestItem, DigestItem.article_id == Article.id)
        .join(Digest, Digest.id == DigestItem.digest_id)
        .where(Digest.user_id == context.user_id, Digest.created_at >= cutoff_dt)
    )
    filters = []
    if urls:
        filters.append(Article.canonical_url.in_([canonicalize_url(url) for url in urls]))
    if title_hashes:
        filters.append(Article.title_hash.in_(title_hashes))
    if keywords:
        filters.extend(Article.normalized_title.ilike(f"%{word.lower()}%") for word in keywords)
    if filters:
        query = query.where(or_(*filters))
    rows = context.db.execute(query.limit(100)).all()
    return {
        "days": days,
        "matches": [
            {
                "title": article.title,
                "canonical_url": article.canonical_url,
                "title_hash": article.title_hash,
                "digest_date": digest.digest_date.isoformat(),
                "digest_id": digest.id,
            }
            for article, digest in rows
        ],
    }
