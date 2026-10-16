from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.agent.prompts import SYSTEM_PROMPT, build_user_prompt
from app.agent.provider import DeepSeekProvider
from app.config import Settings
from app.models import AgentRun, ToolCall
from app.tools import ToolContext, ToolRegistry


@dataclass(slots=True)
class AgentOutcome:
    digest_id: int
    final_message: str
    stop_reason: str


def _redact(value: Any, key: str = "") -> Any:
    sensitive = ("password", "secret", "token", "api_key", "authorization")
    if any(part in key.lower() for part in sensitive):
        return "***"
    if isinstance(value, dict):
        return {name: _redact(item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, key) for item in value[:50]]
    if isinstance(value, str) and len(value) > 2000:
        return value[:2000] + "…"
    return value


class AgentRuntime:
    def __init__(
        self,
        settings: Settings,
        db: Session,
        run: AgentRun,
        registry: ToolRegistry,
        context: ToolContext,
    ) -> None:
        self.settings = settings
        self.db = db
        self.run_record = run
        self.registry = registry
        self.context = context
        self.provider = DeepSeekProvider(settings)

    async def run(
        self,
        preferences: dict[str, Any],
        coverage_start: datetime,
        coverage_end: datetime,
    ) -> AgentOutcome:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": build_user_prompt(preferences, coverage_start, coverage_end),
            },
        ]
        call_signatures: dict[str, int] = {}
        saved_digest_id: int | None = None
        pushed_to_in_app = False

        async with asyncio.timeout(self.settings.agent_timeout_seconds):
            for _round in range(1, self.settings.agent_max_rounds + 1):
                response = await self.provider.complete(messages, self.registry.api_tools())
                self.run_record.model_call_count += 1
                usage = response.usage
                if usage:
                    self.run_record.input_tokens += usage.prompt_tokens or 0
                    self.run_record.output_tokens += usage.completion_tokens or 0
                message = response.choices[0].message
                messages.append(message.model_dump(exclude_none=True))

                if not message.tool_calls:
                    if saved_digest_id is None:
                        raise RuntimeError("Agent stopped before calling save_digest")
                    if not pushed_to_in_app:
                        raise RuntimeError("Agent stopped before pushing the digest to in_app")
                    return AgentOutcome(
                        digest_id=saved_digest_id,
                        final_message=message.content or "简报已生成并保存。",
                        stop_reason="completed",
                    )

                for call in message.tool_calls:
                    if self.run_record.tool_call_count >= self.settings.agent_max_tool_calls:
                        raise RuntimeError("Agent exceeded the tool call budget")
                    name = call.function.name
                    try:
                        arguments = json.loads(call.function.arguments or "{}")
                        if not isinstance(arguments, dict):
                            raise ValueError("tool arguments must be a JSON object")
                    except (json.JSONDecodeError, ValueError) as exc:
                        result: dict[str, Any] | list[Any] = {
                            "ok": False,
                            "error": f"invalid tool arguments: {exc}",
                        }
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.id,
                                "content": json.dumps(result, ensure_ascii=False),
                            }
                        )
                        continue

                    arguments_key = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
                    signature = f"{name}:{arguments_key}"
                    call_signatures[signature] = call_signatures.get(signature, 0) + 1
                    audit = ToolCall(
                        run_id=self.run_record.id,
                        sequence_number=self.run_record.tool_call_count + 1,
                        tool_name=name,
                        arguments_redacted=_redact(arguments),
                        status="RUNNING",
                    )
                    self.db.add(audit)
                    self.db.commit()
                    started = datetime.now(UTC)
                    try:
                        if call_signatures[signature] > 2:
                            raise RuntimeError("duplicate tool call loop detected")
                        result = await self.registry.execute(name, arguments, self.context)
                        audit.status = "SUCCEEDED"
                        if name == "save_digest" and isinstance(result, dict):
                            saved_digest_id = int(result["digest_id"])
                        if (
                            name == "push_digest"
                            and isinstance(result, dict)
                            and result.get("channel") == "in_app"
                            and result.get("status") == "SENT"
                        ):
                            pushed_to_in_app = True
                    except Exception as exc:
                        result = {"ok": False, "error": str(exc), "error_type": type(exc).__name__}
                        audit.status = "FAILED"
                        audit.error_code = type(exc).__name__
                    finished = datetime.now(UTC)
                    audit.finished_at = finished
                    audit.duration_ms = int((finished - started).total_seconds() * 1000)
                    serialized = json.dumps(result, ensure_ascii=False, default=str)
                    audit.result_summary = serialized[:2000]
                    self.run_record.tool_call_count += 1
                    self.db.commit()
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": serialized[:30000],
                        }
                    )

        raise RuntimeError("Agent exceeded the maximum number of model rounds")
