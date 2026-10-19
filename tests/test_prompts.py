import json
from datetime import UTC, datetime

from app.agent.prompts import build_user_prompt


def test_user_prompt_exposes_twitterapi_availability() -> None:
    payload = json.loads(
        build_user_prompt(
            {"max_items": 10},
            datetime(2026, 10, 18, tzinfo=UTC),
            datetime(2026, 10, 19, tzinfo=UTC),
            integrations={"twitterapi_x_search": True},
        )
    )

    assert payload["integrations"]["twitterapi_x_search"] is True
