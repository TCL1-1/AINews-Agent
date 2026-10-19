from __future__ import annotations

import json
import smtplib
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import Any

from sqlalchemy import func, select

from app.models import (
    AgentRun,
    Article,
    Delivery,
    DeliveryChannel,
    Digest,
    DigestItem,
    PreferenceProfile,
    ToolCall,
)
from app.normalization import canonicalize_url, normalize_title, stable_hash
from app.schemas import DigestDraft
from app.tools.registry import ToolContext
from app.tools.x_accounts import normalize_x_handles


def _x_handle_from_url(url: str) -> str | None:
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    if parsed.netloc.lower().removeprefix("www.") not in {"x.com", "twitter.com"}:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 3 or parts[1].lower() != "status":
        return None
    try:
        return normalize_x_handles([parts[0]])[0].lower()
    except (ValueError, IndexError):
        return None


def _enforce_x_watchlist(context: ToolContext, draft: DigestDraft) -> None:
    run = context.db.get(AgentRun, context.run_id)
    snapshot = run.preference_snapshot if run and isinstance(run.preference_snapshot, dict) else {}
    raw_accounts = snapshot.get("x_accounts", [])
    configured = normalize_x_handles(raw_accounts if isinstance(raw_accounts, list) else [])
    if not configured:
        return

    calls = context.db.scalars(
        select(ToolCall)
        .where(
            ToolCall.run_id == context.run_id,
            ToolCall.tool_name == "search_x_accounts",
            ToolCall.status == "SUCCEEDED",
        )
        .order_by(ToolCall.sequence_number)
    ).all()
    if not calls:
        raise ValueError("X 关注账号尚未检索；保存前必须调用 search_x_accounts，并传入完整账号列表")

    requested: set[str] = set()
    handles_with_results: set[str] = set()
    for call in calls:
        try:
            result = json.loads(call.result_summary or "{}")
        except json.JSONDecodeError:
            continue
        if not isinstance(result, dict):
            continue
        requested.update(str(item).lower() for item in result.get("requested_handles", []))
        handles_with_results.update(
            str(item).lower() for item in result.get("handles_with_results", [])
        )

    missing_searches = [handle for handle in configured if handle.lower() not in requested]
    if missing_searches:
        names = "、".join(f"@{handle}" for handle in missing_searches)
        raise ValueError(f"以下 X 关注账号尚未通过 TwitterAPI 检索：{names}")

    selected_handles = {
        handle
        for item in draft.items
        for handle in (
            _x_handle_from_url(str(item.original_url)),
            _x_handle_from_url(str(item.canonical_url)),
        )
        if handle
    }
    missing_items = [
        handle for handle in configured if handle.lower() in handles_with_results - selected_handles
    ]
    if missing_items:
        names = "、".join(f"@{handle}" for handle in missing_items)
        raise ValueError(f"以下有新内容的 X 关注账号尚未纳入简报：{names}")


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
    _enforce_x_watchlist(context, draft)
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
