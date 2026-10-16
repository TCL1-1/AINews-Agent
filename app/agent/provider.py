from __future__ import annotations

from typing import Any

from openai import AsyncOpenAI

from app.config import Settings


class DeepSeekProvider:
    def __init__(self, settings: Settings) -> None:
        if not settings.deepseek_api_key:
            raise ValueError("DEEPSEEK_API_KEY is not configured")
        self.model = settings.deepseek_model
        self.client = AsyncOpenAI(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            timeout=60.0,
            max_retries=2,
        )

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Any:
        return await self.client.chat.completions.create(  # type: ignore[call-overload]
            model=self.model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            temperature=0.2,
            stream=False,
        )
