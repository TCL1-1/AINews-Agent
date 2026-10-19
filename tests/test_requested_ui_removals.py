from pathlib import Path

TEMPLATE_DIR = Path(__file__).parents[1] / "app" / "templates"


def test_dashboard_does_not_render_metric_cards() -> None:
    template = (TEMPLATE_DIR / "dashboard.html").read_text(encoding="utf-8")

    assert 'class="metric-grid"' not in template
    assert "简报总数" not in template
    assert "收录资讯" not in template
    assert "我的收藏" not in template
    assert "每日生成" not in template


def test_digest_does_not_render_feedback_actions() -> None:
    template = (TEMPLATE_DIR / "digest.html").read_text(encoding="utf-8")

    assert 'action="/feedback"' not in template
    assert 'name="feedback_type"' not in template
    assert ">收藏<" not in template
    assert ">不感兴趣<" not in template
    assert "相关度" in template
