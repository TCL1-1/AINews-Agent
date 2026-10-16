from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import SourceConfig
from app.tools.news import DEFAULT_FEEDS


def seed_sources(db: Session) -> None:
    for feed in DEFAULT_FEEDS:
        if db.scalar(select(SourceConfig).where(SourceConfig.name == feed["name"])):
            continue
        db.add(
            SourceConfig(
                name=feed["name"],
                provider_type="rss",
                base_url=feed["url"],
                language=feed["language"],
                category=feed["category"],
                trust_level=4,
            )
        )
    db.commit()

