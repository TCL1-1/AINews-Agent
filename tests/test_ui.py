from pathlib import Path

from app.main import display_title


def test_display_title_removes_trailing_parenthetical_qualifiers() -> None:
    assert display_title("AI 垂直资讯日报 · 2026-10-18（LLM / Agent，受限发布）") == (
        "AI 垂直资讯日报 · 2026-10-18"
    )
    assert display_title("AI 日报 (抓取日志)") == "AI 日报"


def test_display_title_preserves_non_suffix_parentheses() -> None:
    assert display_title("OpenAI（中国）动态日报 · 2026-10-18") == (
        "OpenAI（中国）动态日报 · 2026-10-18"
    )


def test_sidebar_focuses_on_news_and_links_daily_brief_to_latest_digest() -> None:
    template = (Path(__file__).parents[1] / "app" / "templates" / "base.html").read_text(
        encoding="utf-8"
    )

    assert "情报来源" not in template
    assert "Agent 运行" not in template
    assert "/digests/{{ latest_digest_id }}" in template
    assert "request.url.path.startswith('/digests')" in template
