from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.schemas import DigestDraft


def digest_payload() -> dict:
    now = datetime.now(UTC)
    return {
        "title": "AI 产业每日简报",
        "digest_date": now.date().isoformat(),
        "coverage_start": now.isoformat(),
        "coverage_end": now.isoformat(),
        "overview": "今天的核心变化来自模型、芯片与自动驾驶产业的多项正式进展。",
        "items": [
            {
                "original_title": "Example AI announcement",
                "generated_title": "示例 AI 官方公告",
                "original_url": "https://example.com/news/1",
                "canonical_url": "https://example.com/news/1",
                "source_name": "Example",
                "published_at": now.isoformat(),
                "summary": "这是一条用于验证结构化简报字段和真实性约束的测试新闻摘要。",
                "recommendation_reason": "与用户关注的 LLM 主题直接相关。",
                "category": "LLM 与 AI Agent",
                "event_status": "官方确认",
                "keywords": ["LLM"],
                "relevance_score": 95,
                "evidence_reference": "官方公告页面。",
            }
        ],
        "warnings": [],
    }


def test_digest_schema_accepts_supported_section_and_status() -> None:
    assert DigestDraft.model_validate(digest_payload()).items[0].event_status == "官方确认"


def test_digest_schema_rejects_unknown_section() -> None:
    payload = digest_payload()
    payload["items"][0]["category"] = "传闻专区"
    with pytest.raises(ValidationError):
        DigestDraft.model_validate(payload)


def test_digest_schema_rejects_duplicate_urls() -> None:
    payload = digest_payload()
    payload["items"].append(payload["items"][0].copy())
    with pytest.raises(ValidationError, match="duplicate"):
        DigestDraft.model_validate(payload)

