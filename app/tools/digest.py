from __future__ import annotations

import smtplib
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import Any

from sqlalchemy import func, select

from app.models import (
    Article,
    Delivery,
    DeliveryChannel,
    Digest,
    DigestItem,
    PreferenceProfile,
)
from app.normalization import canonicalize_url, normalize_title, stable_hash
from app.schemas import DigestDraft
from app.tools.registry import ToolContext


def save_digest(context: ToolContext, digest: dict[str, Any]) -> dict[str, Any]:
    existing = context.db.scalar(select(Digest).where(Digest.run_id == context.run_id))
    if existing:
        return {
            "digest_id": existing.id,
            "version": existing.version,
            "status": existing.status,
            "item_count": len(existing.items),
            "already_existed": True,
        }

    draft = DigestDraft.model_validate(digest)
    preference = context.db.scalar(
        select(PreferenceProfile).where(PreferenceProfile.user_id == context.user_id)
    )
    max_items = preference.max_items if preference else context.settings.default_max_items
    if len(draft.items) > max_items:
        raise ValueError(f"digest exceeds the configured limit of {max_items} items")

    current_version = context.db.scalar(
        select(func.max(Digest.version)).where(
            Digest.user_id == context.user_id, Digest.digest_date == draft.digest_date
        )
    )
    saved = Digest(
        user_id=context.user_id,
        run_id=context.run_id,
        digest_date=draft.digest_date,
        version=(current_version or 0) + 1,
        status="SAVED",
        title=draft.title,
        overview=draft.overview,
        coverage_start=draft.coverage_start,
        coverage_end=draft.coverage_end,
        warnings=draft.warnings,
    )
    context.db.add(saved)
    context.db.flush()

    seen_urls: set[str] = set()
    for rank, item in enumerate(draft.items, start=1):
        canonical_url = canonicalize_url(str(item.canonical_url))
        if canonical_url in seen_urls:
            raise ValueError("digest contains duplicate URLs")
        seen_urls.add(canonical_url)
        article = context.db.scalar(select(Article).where(Article.canonical_url == canonical_url))
        normalized = normalize_title(item.original_title)
        if article is None:
            article = Article(
                canonical_url=canonical_url,
                original_url=str(item.original_url),
                title=item.original_title,
                normalized_title=normalized,
                title_hash=stable_hash(normalized),
                source_name=item.source_name,
                published_at=item.published_at,
                fetched_at=datetime.now(UTC),
                content_excerpt=item.evidence_reference[:2000],
                fetch_status="verified",
            )
            context.db.add(article)
            context.db.flush()
        saved.items.append(
            DigestItem(
                article_id=article.id,
                rank=rank,
                generated_title=item.generated_title,
                generated_summary=item.summary,
                recommendation_reason=item.recommendation_reason,
                category=item.category,
                event_status=item.event_status,
                keywords=item.keywords,
                relevance_score=item.relevance_score,
                evidence_reference=item.evidence_reference,
            )
        )
    context.db.commit()
    return {
        "digest_id": saved.id,
        "version": saved.version,
        "status": saved.status,
        "item_count": len(saved.items),
        "already_existed": False,
    }


def _email_body(digest: Digest) -> str:
    lines = [digest.title, "", digest.overview, ""]
    for item in digest.items:
        lines.extend(
            [
                f"{item.rank}. [{item.event_status}] {item.generated_title}",
                item.generated_summary,
                f"推荐理由：{item.recommendation_reason}",
                f"来源：{item.article.source_name} {item.article.canonical_url}",
                "",
            ]
        )
    return "\n".join(lines)


def _send_email(context: ToolContext, digest: Digest, recipient: str) -> str:
    settings = context.settings
    if not settings.smtp_host or not settings.smtp_from:
        raise ValueError("SMTP is not configured")
    message = EmailMessage()
    message["Subject"] = digest.title
    message["From"] = settings.smtp_from
    message["To"] = recipient
    message.set_content(_email_body(digest))
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
        if settings.smtp_use_tls:
            smtp.starttls()
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(message)
    return message["Message-ID"] or f"smtp-{digest.id}"


def push_digest(context: ToolContext, digest_id: int, channel: str) -> dict[str, Any]:
    digest = context.db.scalar(
        select(Digest).where(Digest.id == digest_id, Digest.user_id == context.user_id)
    )
    if digest is None:
        raise ValueError("digest not found or not owned by user")
    idempotency_key = f"digest:{digest.id}:channel:{channel}"
    existing = context.db.scalar(
        select(Delivery).where(Delivery.idempotency_key == idempotency_key)
    )
    if existing and existing.status == "SENT":
        return {
            "delivery_id": existing.id,
            "channel": channel,
            "status": existing.status,
            "already_sent": True,
        }

    delivery = existing or Delivery(
        digest_id=digest.id,
        channel_type=channel,
        idempotency_key=idempotency_key,
        status="PENDING",
    )
    if existing is None:
        context.db.add(delivery)
    delivery.attempt_count = (delivery.attempt_count or 0) + 1
    try:
        if channel == "in_app":
            provider_id = f"in-app-{digest.id}"
        elif channel == "email":
            configured = context.db.scalar(
                select(DeliveryChannel).where(
                    DeliveryChannel.user_id == context.user_id,
                    DeliveryChannel.channel_type == "email",
                    DeliveryChannel.enabled.is_(True),
                    DeliveryChannel.verified_at.is_not(None),
                )
            )
            if configured is None or not configured.config.get("email"):
                raise ValueError("no verified email delivery channel")
            delivery.channel_id = configured.id
            provider_id = _send_email(context, digest, str(configured.config["email"]))
        else:
            raise ValueError("unsupported delivery channel")
        delivery.provider_message_id = provider_id
        delivery.status = "SENT"
        delivery.sent_at = datetime.now(UTC)
        delivery.last_error = None
        context.db.commit()
    except Exception as exc:
        delivery.status = "FAILED"
        delivery.last_error = str(exc)[:1000]
        context.db.commit()
        raise
    return {
        "delivery_id": delivery.id,
        "channel": channel,
        "status": delivery.status,
        "sent_at": delivery.sent_at.isoformat() if delivery.sent_at else None,
        "already_sent": False,
    }
