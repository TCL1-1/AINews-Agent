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


@dataclass(slots=True)
class _ToolExecution:
    result: dict[str, Any] | list[Any]
    status: str
    error_code: str | None
    duration_ms: int


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


def _audit_result_summary(
    tool_name: str,
    result: dict[str, Any] | list[Any],
    max_chars: int = 2000,
) -> str:
    redacted = _redact(result)
    if isinstance(redacted, dict):
        summary_fields = {
            "search_x_accounts": (
                "provider",
                "provider_status",
                "requested_handles",
                "handles_with_results",
                "handles_without_results",
                "count",
                "source_errors",
            ),
            "search_news": (
                "query",
                "mode",
                "count",
                "total_unique_candidates",
                "source_errors",
                "provider_metrics",
                "search_policy",
            ),
            "fetch_article": (
                "url",
                "canonical_url",
                "title",
                "source",
                "published_at",
                "content_hash",
                "fetch_status",
                "ok",
                "error",
                "error_type",
            ),
        }
        fields = summary_fields.get(tool_name)
        if fields:
            redacted = {field: redacted[field] for field in fields if field in redacted}
            for list_field in ("source_errors", "provider_metrics"):
                value = redacted.get(list_field)
                if isinstance(value, list):
                    redacted[list_field] = value[:20]

    serialized = json.dumps(redacted, ensure_ascii=False, default=str)
    if len(serialized) <= max_chars:
        return serialized
    fallback = {
        "truncated": True,
        "tool_name": tool_name,
        "result_type": type(result).__name__,
        "keys": list(result)[:50] if isinstance(result, dict) else [],
        "preview": serialized[:800],
    }
    return json.dumps(fallback, ensure_ascii=False, default=str)


async def _execute_tool_batch(
    registry: ToolRegistry,
    context: ToolContext,
    calls: list[tuple[str, dict[str, Any]]],
    concurrency: int,
) -> list[_ToolExecution]:
    semaphore = asyncio.Semaphore(concurrency)

    async def execute(name: str, arguments: dict[str, Any]) -> _ToolExecution:
        started = datetime.now(UTC)
        try:
            async with semaphore:
                result = await registry.execute(name, arguments, context)
            status = "SUCCEEDED"
            error_code = None
        except Exception as exc:
            result = {"ok": False, "error": str(exc), "error_type": type(exc).__name__}
            status = "FAILED"
            error_code = type(exc).__name__
        finished = datetime.now(UTC)
        return _ToolExecution(
            result=result,
            status=status,
            error_code=error_code,
            duration_ms=int((finished - started).total_seconds() * 1000),
        )

    return await asyncio.gather(*(execute(name, arguments) for name, arguments in calls))


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
                "content": build_user_prompt(
                    preferences,
                    coverage_start,
                    coverage_end,
                    tool_budgets={
                        "total": self.settings.agent_max_tool_calls,
                        "search_news": self.settings.agent_max_search_calls,
                        "fetch_article": self.settings.agent_max_fetch_calls,
                        "reserved_for_save_and_push": (self.settings.agent_reserved_final_calls),
                        "target_unique_candidates": (self.settings.agent_search_target_candidates),
                    },
                    integrations={
                        "twitterapi_x_search": bool(self.settings.twitterapi_api_key),
                    },
                ),
            },
        ]
        call_signatures: dict[str, int] = {}
        tool_counts: dict[str, int] = {}
        discovered_urls: set[str] = set()
        search_modes_used: set[str] = set()
        twitterapi_x_search_used = False
        require_twitterapi_x_search = bool(self.settings.twitterapi_api_key)
        consecutive_searches_without_new_urls = 0
        policy_rejections = 0
        audit_sequence = self.run_record.tool_call_count
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

                tool_calls = list(message.tool_calls)
                call_index = 0
                while call_index < len(tool_calls):
                    call = tool_calls[call_index]
                    name = call.function.name
                    if name == "fetch_article":
                        batch_end = call_index
                        while (
                            batch_end < len(tool_calls)
                            and tool_calls[batch_end].function.name == "fetch_article"
                        ):
                            batch_end += 1
                        batch_calls = tool_calls[call_index:batch_end]
                        parsed_batch: list[tuple[Any, dict[str, Any], str]] = []
                        batch_signature_counts: dict[str, int] = {}
                        batch_arguments_valid = True
                        for batch_call in batch_calls:
                            try:
                                batch_arguments = json.loads(batch_call.function.arguments or "{}")
                                if not isinstance(batch_arguments, dict):
                                    raise ValueError("tool arguments must be a JSON object")
                            except (json.JSONDecodeError, ValueError):
                                batch_arguments_valid = False
                                break
                            arguments_key = json.dumps(
                                batch_arguments, ensure_ascii=False, sort_keys=True
                            )
                            batch_signature = f"fetch_article:{arguments_key}"
                            batch_signature_counts[batch_signature] = (
                                batch_signature_counts.get(batch_signature, 0) + 1
                            )
                            parsed_batch.append((batch_call, batch_arguments, batch_signature))

                        remaining_calls = (
                            self.settings.agent_max_tool_calls - self.run_record.tool_call_count
                        )
                        batch_within_budget = (
                            len(batch_calls) > 1
                            and len(batch_calls)
                            <= remaining_calls - self.settings.agent_reserved_final_calls
                            and tool_counts.get("fetch_article", 0) + len(batch_calls)
                            <= self.settings.agent_max_fetch_calls
                        )
                        batch_without_duplicates = all(
                            call_signatures.get(signature, 0) + count <= 2
                            for signature, count in batch_signature_counts.items()
                        )
                        if (
                            batch_arguments_valid
                            and batch_within_budget
                            and batch_without_duplicates
                        ):
                            prepared_batch: list[tuple[Any, dict[str, Any], ToolCall]] = []
                            for batch_call, batch_arguments, batch_signature in parsed_batch:
                                call_signatures[batch_signature] = (
                                    call_signatures.get(batch_signature, 0) + 1
                                )
                                audit_sequence += 1
                                batch_audit = ToolCall(
                                    run_id=self.run_record.id,
                                    sequence_number=audit_sequence,
                                    tool_name="fetch_article",
                                    arguments_redacted=_redact(batch_arguments),
                                    status="RUNNING",
                                )
                                self.db.add(batch_audit)
                                prepared_batch.append((batch_call, batch_arguments, batch_audit))
                            self.db.commit()
                            executions = await _execute_tool_batch(
                                self.registry,
                                self.context,
                                [
                                    ("fetch_article", batch_arguments)
                                    for _, batch_arguments, _ in prepared_batch
                                ],
                                self.settings.agent_fetch_concurrency,
                            )
                            for prepared, execution in zip(prepared_batch, executions, strict=True):
                                batch_call, _, batch_audit = prepared
                                batch_audit.status = execution.status
                                batch_audit.error_code = execution.error_code
                                batch_audit.finished_at = datetime.now(UTC)
                                batch_audit.duration_ms = execution.duration_ms
                                batch_audit.result_summary = _audit_result_summary(
                                    "fetch_article", execution.result
                                )
                                if execution.status == "SUCCEEDED":
                                    tool_counts["fetch_article"] = (
                                        tool_counts.get("fetch_article", 0) + 1
                                    )
                                self.run_record.tool_call_count += 1
                                serialized = json.dumps(
                                    execution.result,
                                    ensure_ascii=False,
                                    default=str,
                                )
                                messages.append(
                                    {
                                        "role": "tool",
                                        "tool_call_id": batch_call.id,
                                        "content": serialized[:30000],
                                    }
                                )
                            self.db.commit()
                            call_index = batch_end
                            continue

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
                        call_index += 1
                        continue

                    remaining_calls = (
                        self.settings.agent_max_tool_calls - self.run_record.tool_call_count
                    )
                    policy_error: str | None = None
                    if (
                        name == "save_digest"
                        and require_twitterapi_x_search
                        and not twitterapi_x_search_used
                        and tool_counts.get("search_news", 0) < self.settings.agent_max_search_calls
                        and remaining_calls > self.settings.agent_reserved_final_calls
                    ):
                        policy_error = (
                            "TwitterAPI X coverage is still missing. Before saving, call "
                            "search_news with mode='platform' and platforms=['x']; use the result "
                            "only as a lead and verify important claims."
                        )
                    elif name not in {"save_digest", "push_digest"} and (
                        remaining_calls <= self.settings.agent_reserved_final_calls
                    ):
                        policy_error = (
                            "Finalization budget is reserved. Stop discovery and call save_digest, "
                            "then push_digest(channel='in_app')."
                        )
                    elif name == "search_news" and tool_counts.get(name, 0) >= (
                        self.settings.agent_max_search_calls
                    ):
                        policy_error = (
                            "Search budget exhausted. Use the best verified candidates "
                            "already found; "
                            "do not search again. Missing categories may be disclosed in warnings."
                        )
                    elif name == "fetch_article" and tool_counts.get(name, 0) >= (
                        self.settings.agent_max_fetch_calls
                    ):
                        policy_error = (
                            "Article fetch budget exhausted. Finish with verified material already "
                            "available and disclose coverage gaps in warnings."
                        )
                    elif self.run_record.tool_call_count >= self.settings.agent_max_tool_calls:
                        raise RuntimeError("Agent exceeded the tool call budget")

                    arguments_key = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
                    signature = f"{name}:{arguments_key}"
                    call_signatures[signature] = call_signatures.get(signature, 0) + 1
                    audit_sequence += 1
                    audit = ToolCall(
                        run_id=self.run_record.id,
                        sequence_number=audit_sequence,
                        tool_name=name,
                        arguments_redacted=_redact(arguments),
                        status="RUNNING",
                    )
                    self.db.add(audit)
                    self.db.commit()
                    started = datetime.now(UTC)
                    try:
                        if policy_error:
                            policy_rejections += 1
                            result = {
                                "ok": False,
                                "blocked_by_policy": True,
                                "error": policy_error,
                                "remaining_total_calls": remaining_calls,
                            }
                            audit.status = "REJECTED"
                            audit.error_code = "ToolBudgetPolicy"
                            if policy_rejections > 3:
                                raise RuntimeError("Agent ignored tool budget guidance repeatedly")
                        else:
                            if call_signatures[signature] > 2:
                                raise RuntimeError("duplicate tool call loop detected")
                            result = await self.registry.execute(name, arguments, self.context)
                            audit.status = "SUCCEEDED"
                            tool_counts[name] = tool_counts.get(name, 0) + 1

                        if name == "search_news" and not policy_error and isinstance(result, dict):
                            search_mode = str(arguments.get("mode", "hybrid"))
                            search_modes_used.add(search_mode)
                            requested_platforms = arguments.get("platforms")
                            if search_mode == "platform" and (
                                requested_platforms is None
                                or (
                                    isinstance(requested_platforms, list)
                                    and "x" in requested_platforms
                                )
                            ):
                                twitterapi_x_search_used = True
                            candidate_urls = {
                                str(candidate.get("url"))
                                for candidate in result.get("candidates", [])
                                if isinstance(candidate, dict) and candidate.get("url")
                            }
                            new_urls = candidate_urls - discovered_urls
                            discovered_urls.update(candidate_urls)
                            if new_urls:
                                consecutive_searches_without_new_urls = 0
                            else:
                                consecutive_searches_without_new_urls += 1
                            search_count = tool_counts.get("search_news", 0)
                            stop_reasons: list[str] = []
                            if search_count >= self.settings.agent_max_search_calls:
                                stop_reasons.append("search_call_limit_reached")
                            if len(discovered_urls) >= (
                                self.settings.agent_search_target_candidates
                            ) and (not require_twitterapi_x_search or twitterapi_x_search_used):
                                stop_reasons.append("enough_unique_candidates")
                            if (
                                consecutive_searches_without_new_urls >= 2
                                and search_count >= 4
                                and (not require_twitterapi_x_search or twitterapi_x_search_used)
                            ):
                                stop_reasons.append("two_searches_without_new_urls")
                            missing_required_searches = []
                            if require_twitterapi_x_search and not twitterapi_x_search_used:
                                missing_required_searches.append("platform:x")
                            result["search_policy"] = {
                                "search_calls_used": search_count,
                                "search_calls_limit": self.settings.agent_max_search_calls,
                                "new_urls_this_call": len(new_urls),
                                "unique_urls_discovered": len(discovered_urls),
                                "modes_used": sorted(search_modes_used),
                                "missing_required_searches": missing_required_searches,
                                "stop_search": bool(stop_reasons),
                                "stop_reasons": stop_reasons,
                                "next_action": (
                                    "Stop searching. Fetch and verify the strongest "
                                    "candidates, then "
                                    "save and push the digest."
                                    if stop_reasons
                                    else (
                                        "Before stopping, call search_news with mode='platform' "
                                        "and platforms=['x'] because TwitterAPI is configured."
                                        if missing_required_searches
                                        else (
                                            "Search may continue only if an important preference "
                                            "lacks coverage."
                                        )
                                    )
                                ),
                            }
                        if name == "save_digest" and not policy_error and isinstance(result, dict):
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
                    audit.result_summary = _audit_result_summary(name, result)
                    if not policy_error:
                        self.run_record.tool_call_count += 1
                    self.db.commit()
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": serialized[:30000],
                        }
                    )
                    call_index += 1

        raise RuntimeError("Agent exceeded the maximum number of model rounds")
