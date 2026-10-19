from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

from app.normalization import canonicalize_url

if TYPE_CHECKING:
    from app.tools.registry import ToolContext


MAX_X_ACCOUNTS = 3
X_HANDLE_RE = re.compile(r"[A-Za-z0-9_]{1,15}")
TWITTERAPI_HOST = "api.twitterapi.io"


def normalize_x_handle(value: str) -> str:
    candidate = value.strip()
    if not candidate:
        return ""
    if candidate.startswith(("https://", "http://")):
        parsed = urlsplit(candidate)
        if parsed.netloc.lower().removeprefix("www.") not in {"x.com", "twitter.com"}:
            raise ValueError("X 账号链接必须来自 x.com 或 twitter.com")
        candidate = parsed.path.strip("/").split("/", 1)[0]
    candidate = candidate.lstrip("@").strip()
    if not X_HANDLE_RE.fullmatch(candidate):
        raise ValueError("X 账号只能包含 1-15 位字母、数字或下划线")
    return candidate


def normalize_x_handles(values: list[str]) -> list[str]:
    handles: list[str] = []
    seen: set[str] = set()
    for value in values:
        handle = normalize_x_handle(value)
        key = handle.lower()
        if handle and key not in seen:
            handles.append(handle)
            seen.add(key)
    if len(handles) > MAX_X_ACCOUNTS:
        raise ValueError(f"最多只能配置 {MAX_X_ACCOUNTS} 个 X 账号")
    return handles


def _parse_datetime(value: str) -> datetime:
    normalized = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_tweet_datetime(value: object) -> datetime | None:
    if not value:
        return None
    raw = str(value).strip()
    try:
        return _parse_datetime(raw)
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        try:
            parsed = datetime.strptime(raw, "%a %b %d %H:%M:%S %z %Y")
        except ValueError:
            return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def build_x_account_query(handle: str, start: datetime, end: datetime) -> str:
    normalized = normalize_x_handle(handle)
    return (
        f"from:{normalized} since_time:{int(start.timestamp())} "
        f"until_time:{int(end.timestamp())} -filter:replies -filter:retweets"
    )


def _validate_endpoint(endpoint: str) -> None:
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "https"
        or parsed.hostname != TWITTERAPI_HOST
        or parsed.path != "/twitter/tweet/advanced_search"
        or parsed.username
        or parsed.password
    ):
        raise ValueError("TwitterAPI endpoint must be the configured HTTPS advanced-search URL")


def _tweet_candidate(
    tweet: dict[str, Any], requested_handle: str, start: datetime, end: datetime
) -> dict[str, Any] | None:
    tweet_id = str(tweet.get("id") or tweet.get("tweetId") or "").strip()
    text = str(tweet.get("text") or tweet.get("fullText") or "").strip()
    published_at = _parse_tweet_datetime(
        tweet.get("createdAt") or tweet.get("created_at") or tweet.get("date")
    )
    if not tweet_id or not text or published_at is None or not (start <= published_at <= end):
        return None

    author_value = tweet.get("author")
    author = author_value if isinstance(author_value, dict) else {}
    username = (
        str(
            author.get("userName")
            or author.get("username")
            or tweet.get("userName")
            or requested_handle
        )
        .strip()
        .lstrip("@")
    )
    compact_text = " ".join(text.split())
    url = str(tweet.get("url") or f"https://x.com/{username}/status/{tweet_id}")

    def count_value(*names: str) -> int:
        for name in names:
            try:
                return int(tweet.get(name) or 0)
            except (TypeError, ValueError):
                continue
        return 0

    return {
        "title": f"@{username}：{compact_text[:140]}",
        "url": canonicalize_url(url),
        "source": f"@{username}",
        "source_domain": "x.com",
        "published_at": published_at.isoformat(),
        "snippet": compact_text[:1000],
        "language": str(tweet.get("lang") or "unknown"),
        "source_category": "X 关注账号",
        "provider": "twitterapi_io",
        "platform": "x",
        "configured_trust": 2,
        "source_tier": "D",
        "x_handle": requested_handle,
        "watchlist_match": True,
        "popularity": {
            "likes": count_value("likeCount", "favorite_count"),
            "reposts": count_value("retweetCount", "retweet_count"),
            "replies": count_value("replyCount", "reply_count"),
            "views": count_value("viewCount", "view_count"),
        },
    }


async def search_x_accounts(
    context: ToolContext,
    handles: list[str],
    start_time: str,
    end_time: str,
    limit_per_account: int = 5,
) -> dict[str, Any]:
    normalized_handles = normalize_x_handles(handles)
    if not normalized_handles:
        raise ValueError("at least one X account is required")
    start = _parse_datetime(start_time)
    end = _parse_datetime(end_time)
    if start >= end:
        raise ValueError("start_time must be earlier than end_time")
    limit_per_account = min(max(limit_per_account, 1), 10)

    api_key = context.settings.twitterapi_api_key.strip()
    endpoint = context.settings.twitterapi_search_url
    _validate_endpoint(endpoint)
    if not api_key:
        return {
            "provider": "twitterapi_io",
            "provider_status": "unavailable",
            "requested_handles": normalized_handles,
            "handles_with_results": [],
            "handles_without_results": normalized_handles,
            "count": 0,
            "candidates": [],
            "source_errors": ["TwitterAPI API key is not configured"],
        }

    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=8.0)) as client:

        async def fetch_handle(handle: str) -> tuple[str, list[dict[str, Any]]]:
            response = await client.get(
                endpoint,
                params={
                    "query": build_x_account_query(handle, start, end),
                    "queryType": "Latest",
                    "cursor": "",
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
                candidate = _tweet_candidate(tweet, handle, start, end)
                if candidate is not None:
                    candidates.append(candidate)
                if len(candidates) >= limit_per_account:
                    break
            return handle, candidates

        results = await asyncio.gather(
            *(fetch_handle(handle) for handle in normalized_handles),
            return_exceptions=True,
        )

    candidates: list[dict[str, Any]] = []
    errors: list[str] = []
    handles_with_results: list[str] = []
    for handle, result in zip(normalized_handles, results, strict=True):
        if isinstance(result, BaseException):
            errors.append(f"@{handle}: {type(result).__name__}: {str(result)[:160]}")
            continue
        _, handle_candidates = result
        if handle_candidates:
            handles_with_results.append(handle)
            candidates.extend(handle_candidates)

    handles_without_results = [
        handle
        for handle in normalized_handles
        if handle.lower() not in {item.lower() for item in handles_with_results}
    ]
    candidates.sort(key=lambda item: item["published_at"], reverse=True)
    return {
        "provider": "twitterapi_io",
        "provider_status": "partial" if errors else "succeeded",
        "requested_handles": normalized_handles,
        "handles_with_results": handles_with_results,
        "handles_without_results": handles_without_results,
        "count": len(candidates),
        "candidates": candidates,
        "source_errors": errors,
        "verification_note": (
            "关注账号帖子是优先线索。账号本人的陈述可作为其自身观点或公告来源；"
            "涉及第三方的重要事实仍需官网或独立来源核验。"
        ),
    }
