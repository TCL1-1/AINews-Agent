from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field, HttpUrl, field_validator

ALLOWED_CATEGORIES = {
    "今日必读",
    "LLM 与 AI Agent",
    "半导体与芯片",
    "自动驾驶与机器人",
    "研究与开源",
    "产业、资本与政策",
    "即将发生",
    "持续关注",
}
ALLOWED_EVENT_STATUS = {"已发生", "即将发生", "持续进展", "官方确认", "待进一步核实"}


class NewsCandidate(BaseModel):
    title: str
    url: HttpUrl
    source: str
    published_at: datetime | None = None
    snippet: str = ""
    language: str = "unknown"


class ArticleContent(BaseModel):
    url: HttpUrl
    canonical_url: HttpUrl
    title: str
    author: str | None = None
    source: str
    published_at: datetime | None = None
    content: str
    content_hash: str
    fetch_status: str


class DigestItemDraft(BaseModel):
    original_title: str
    generated_title: str
    original_url: HttpUrl
    canonical_url: HttpUrl
    source_name: str
    published_at: datetime | None = None
    summary: str = Field(min_length=20, max_length=1500)
    recommendation_reason: str = Field(min_length=5, max_length=600)
    category: str
    event_status: str
    keywords: list[str] = Field(default_factory=list, max_length=12)
    relevance_score: float = Field(ge=0, le=100)
    evidence_reference: str = Field(default="", max_length=1000)

    @field_validator("category")
    @classmethod
    def validate_category(cls, value: str) -> str:
        if value not in ALLOWED_CATEGORIES:
            raise ValueError(f"unsupported category: {value}")
        return value

    @field_validator("event_status")
    @classmethod
    def validate_event_status(cls, value: str) -> str:
        if value not in ALLOWED_EVENT_STATUS:
            raise ValueError(f"unsupported event status: {value}")
        return value


class DigestDraft(BaseModel):
    title: str = Field(min_length=2, max_length=300)
    digest_date: date
    coverage_start: datetime
    coverage_end: datetime
    overview: str = Field(min_length=20, max_length=3000)
    items: list[DigestItemDraft] = Field(min_length=1, max_length=30)
    warnings: list[str] = Field(default_factory=list)

    @field_validator("items")
    @classmethod
    def unique_urls(cls, items: list[DigestItemDraft]) -> list[DigestItemDraft]:
        urls = [str(item.canonical_url) for item in items]
        if len(urls) != len(set(urls)):
            raise ValueError("digest contains duplicate canonical URLs")
        return items

