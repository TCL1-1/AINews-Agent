from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(512))
    role: Mapped[str] = mapped_column(String(32), default="user")
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    preference: Mapped[PreferenceProfile | None] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False
    )
    digests: Mapped[list[Digest]] = relationship(back_populates="user")


class PreferenceProfile(TimestampMixin, Base):
    __tablename__ = "preference_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True, index=True)
    topics: Mapped[list[str]] = mapped_column(
        JSON, default=lambda: ["LLM", "AI Agent", "半导体", "芯片", "自动驾驶"]
    )
    keywords: Mapped[list[str]] = mapped_column(JSON, default=list)
    excluded_keywords: Mapped[list[str]] = mapped_column(JSON, default=list)
    languages: Mapped[list[str]] = mapped_column(JSON, default=lambda: ["zh", "en"])
    source_preferences: Mapped[list[str]] = mapped_column(JSON, default=list)
    x_accounts: Mapped[list[str]] = mapped_column(JSON, default=list)
    max_items: Mapped[int] = mapped_column(Integer, default=10)
    coverage_hours: Mapped[int] = mapped_column(Integer, default=24)
    schedule_time: Mapped[time] = mapped_column(Time, default=time(8, 0))
    weekend_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)

    user: Mapped[User] = relationship(back_populates="preference")


class SourceConfig(TimestampMixin, Base):
    __tablename__ = "source_configs"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    provider_type: Mapped[str] = mapped_column(String(32), default="rss")
    base_url: Mapped[str] = mapped_column(String(2048))
    language: Mapped[str] = mapped_column(String(16), default="en")
    category: Mapped[str] = mapped_column(String(64), default="general")
    trust_level: Mapped[int] = mapped_column(Integer, default=3)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Article(TimestampMixin, Base):
    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(primary_key=True)
    canonical_url: Mapped[str] = mapped_column(String(2048), unique=True, index=True)
    original_url: Mapped[str] = mapped_column(String(2048))
    title: Mapped[str] = mapped_column(String(1024))
    normalized_title: Mapped[str] = mapped_column(String(1024), index=True)
    title_hash: Mapped[str] = mapped_column(String(64), index=True)
    source_name: Mapped[str] = mapped_column(String(256))
    author: Mapped[str | None] = mapped_column(String(256), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    language: Mapped[str] = mapped_column(String(16), default="unknown")
    content_excerpt: Mapped[str] = mapped_column(Text, default="")
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fetch_status: Mapped[str] = mapped_column(String(32), default="candidate")


class DigestJob(TimestampMixin, Base):
    __tablename__ = "digest_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    job_type: Mapped[str] = mapped_column(String(32), default="manual")
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True)
    status: Mapped[str] = mapped_column(String(32), default="PENDING", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("digest_jobs.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="RUNNING")
    model_provider: Mapped[str] = mapped_column(String(64), default="deepseek")
    model_name: Mapped[str] = mapped_column(String(128))
    prompt_version: Mapped[str] = mapped_column(String(32), default="v1")
    toolset_version: Mapped[str] = mapped_column(String(32), default="v1")
    preference_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    model_call_count: Mapped[int] = mapped_column(Integer, default=0)
    tool_call_count: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    stop_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    tool_calls: Mapped[list[ToolCall]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="ToolCall.sequence_number"
    )


class ToolCall(Base):
    __tablename__ = "tool_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    sequence_number: Mapped[int] = mapped_column(Integer)
    tool_name: Mapped[str] = mapped_column(String(128))
    tool_version: Mapped[str] = mapped_column(String(32), default="v1")
    arguments_redacted: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result_summary: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="RUNNING")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    run: Mapped[AgentRun] = relationship(back_populates="tool_calls")


class Digest(TimestampMixin, Base):
    __tablename__ = "digests"
    __table_args__ = (UniqueConstraint("user_id", "digest_date", "version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("agent_runs.id"), unique=True)
    digest_date: Mapped[date] = mapped_column(Date, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(32), default="SAVED")
    title: Mapped[str] = mapped_column(String(512))
    overview: Mapped[str] = mapped_column(Text)
    coverage_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    coverage_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    warnings: Mapped[list[str]] = mapped_column(JSON, default=list)

    user: Mapped[User] = relationship(back_populates="digests")
    items: Mapped[list[DigestItem]] = relationship(
        back_populates="digest", cascade="all, delete-orphan", order_by="DigestItem.rank"
    )
    deliveries: Mapped[list[Delivery]] = relationship(
        back_populates="digest", cascade="all, delete-orphan"
    )


class DigestItem(Base):
    __tablename__ = "digest_items"
    __table_args__ = (UniqueConstraint("digest_id", "article_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    digest_id: Mapped[int] = mapped_column(ForeignKey("digests.id"), index=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"), index=True)
    rank: Mapped[int] = mapped_column(Integer)
    generated_title: Mapped[str] = mapped_column(String(1024))
    generated_summary: Mapped[str] = mapped_column(Text)
    recommendation_reason: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(64))
    event_status: Mapped[str] = mapped_column(String(32), default="已发生")
    keywords: Mapped[list[str]] = mapped_column(JSON, default=list)
    relevance_score: Mapped[float] = mapped_column(Float, default=0.0)
    evidence_reference: Mapped[str] = mapped_column(Text, default="")

    digest: Mapped[Digest] = relationship(back_populates="items")
    article: Mapped[Article] = relationship()


class DeliveryChannel(TimestampMixin, Base):
    __tablename__ = "delivery_channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    channel_type: Mapped[str] = mapped_column(String(32), default="in_app")
    display_name: Mapped[str] = mapped_column(String(128), default="站内简报")
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Delivery(Base):
    __tablename__ = "deliveries"

    id: Mapped[int] = mapped_column(primary_key=True)
    digest_id: Mapped[int] = mapped_column(ForeignKey("digests.id"), index=True)
    channel_id: Mapped[int | None] = mapped_column(
        ForeignKey("delivery_channels.id"), nullable=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True)
    channel_type: Mapped[str] = mapped_column(String(32), default="in_app")
    status: Mapped[str] = mapped_column(String(32), default="PENDING")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    provider_message_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    digest: Mapped[Digest] = relationship(back_populates="deliveries")


class UserFeedback(Base):
    __tablename__ = "user_feedback"
    __table_args__ = (UniqueConstraint("user_id", "article_id", "feedback_type"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"), index=True)
    digest_id: Mapped[int | None] = mapped_column(ForeignKey("digests.id"), nullable=True)
    feedback_type: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
