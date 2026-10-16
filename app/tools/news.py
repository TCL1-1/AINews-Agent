from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import socket
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin, urlsplit

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


def _configured_feeds(context: ToolContext) -> list[dict[str, str]]:
    rows = context.db.scalars(
        select(SourceConfig).where(
            SourceConfig.enabled.is_(True), SourceConfig.provider_type == "rss"
        )
    ).all()
    if not rows:
        return DEFAULT_FEEDS
    return [
        {"name": row.name, "url": row.base_url, "language": row.language, "category": row.category}
        for row in rows
    ]


async def search_news(
    context: ToolContext,
    query: str,
    start_time: str,
    end_time: str,
    sources: list[str] | None = None,
    language: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    start = _parse_iso(start_time)
    end = _parse_iso(end_time)
    if start >= end:
        raise ValueError("start_time must be earlier than end_time")
    terms = [term.lower() for term in query.replace(",", " ").split() if term]
    wanted_sources = {source.lower() for source in (sources or [])}
    feeds = [
        feed
        for feed in _configured_feeds(context)
        if (not wanted_sources or feed["name"].lower() in wanted_sources)
        and (not language or feed["language"] == language)
    ]
    candidates: list[dict[str, Any]] = []
    errors: list[str] = []
    timeout = httpx.Timeout(15.0, connect=8.0)
    async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": USER_AGENT}) as client:
        for feed in feeds:
            try:
                response = await client.get(feed["url"])
                response.raise_for_status()
                parsed = feedparser.parse(response.content)
                for entry in parsed.entries:
                    title = str(entry.get("title", "")).strip()
                    link = str(entry.get("link", "")).strip()
                    summary_html = str(entry.get("summary", entry.get("description", "")))
                    summary = BeautifulSoup(summary_html, "html.parser").get_text(" ", strip=True)
                    published_at = _entry_datetime(entry)
                    haystack = f"{title} {summary}".lower()
                    if terms and not any(term in haystack for term in terms):
                        continue
                    if published_at and not (start <= published_at <= end):
                        continue
                    if not title or not link:
                        continue
                    candidates.append(
                        {
                            "title": title,
                            "url": canonicalize_url(link),
                            "source": feed["name"],
                            "published_at": published_at.isoformat() if published_at else None,
                            "snippet": summary[:1000],
                            "language": feed["language"],
                            "source_category": feed["category"],
                        }
                    )
            except (httpx.HTTPError, OSError, ValueError) as exc:
                errors.append(f"{feed['name']}: {type(exc).__name__}")

    unique: dict[str, dict[str, Any]] = {}
    for item in sorted(
        candidates,
        key=lambda candidate: candidate["published_at"] or "",
        reverse=True,
    ):
        unique.setdefault(item["url"], item)
    return {
        "query": query,
        "count": min(len(unique), limit),
        "candidates": list(unique.values())[:limit],
        "source_errors": errors,
    }


async def _validate_public_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("only public HTTP(S) URLs are allowed")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    records = await asyncio.to_thread(socket.getaddrinfo, parsed.hostname, port)
    for record in records:
        address = ipaddress.ip_address(record[4][0])
        if not address.is_global:
            raise ValueError("URL resolves to a non-public address")


async def fetch_article(
    context: ToolContext, url: str, max_chars: int = 20000
) -> dict[str, Any]:
    del context
    current_url = url
    timeout = httpx.Timeout(20.0, connect=8.0)
    async with httpx.AsyncClient(
        timeout=timeout, headers={"User-Agent": USER_AGENT}, follow_redirects=False
    ) as client:
        for _ in range(4):
            await _validate_public_url(current_url)
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
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form", "noscript"]):
        tag.decompose()
    canonical_tag = soup.find("link", rel=lambda value: value and "canonical" in value)
    canonical = current_url
    if canonical_tag and canonical_tag.get("href"):
        canonical = urljoin(current_url, str(canonical_tag["href"]))
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    heading = soup.find("h1")
    if heading:
        title = heading.get_text(" ", strip=True) or title
    article = soup.find("article") or soup.find("main") or soup.body
    content = article.get_text("\n", strip=True) if article else ""
    lines = [line.strip() for line in content.splitlines() if len(line.strip()) > 20]
    cleaned = "\n".join(lines)[:max_chars]
    if len(cleaned) < 200:
        raise ValueError("article body is too short")
    author_meta = soup.find("meta", attrs={"name": "author"})
    published_meta = soup.find(
        "meta", attrs={"property": "article:published_time"}
    ) or soup.find("meta", attrs={"name": "date"})
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
        "fetch_status": "success",
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
