from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.config import Settings

ToolHandler = Callable[..., dict[str, Any] | list[Any] | Awaitable[dict[str, Any] | list[Any]]]


@dataclass(slots=True)
class ToolContext:
    db: Session
    user_id: int
    run_id: int
    workspace: Path
    settings: Settings


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler

    def as_api_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"duplicate tool: {spec.name}")
        self._tools[spec.name] = spec

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    def api_tools(self) -> list[dict[str, Any]]:
        return [tool.as_api_tool() for tool in self._tools.values()]

    async def execute(
        self, name: str, arguments: dict[str, Any], context: ToolContext
    ) -> dict[str, Any] | list[Any]:
        spec = self._tools.get(name)
        if spec is None:
            raise ValueError(f"unknown tool: {name}")
        result = spec.handler(context=context, **arguments)
        if inspect.isawaitable(result):
            return await result
        return result


def object_schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def build_tool_registry() -> ToolRegistry:
    from app.schemas import DigestDraft
    from app.tools.digest import push_digest, save_digest
    from app.tools.filesystem import bash, list_dir, read_file, search_content, write_file
    from app.tools.news import fetch_article, query_news_history, search_news
    from app.tools.x_accounts import search_x_accounts

    registry = ToolRegistry()
    digest_schema = DigestDraft.model_json_schema()
    digest_definitions = digest_schema.pop("$defs", {})
    save_digest_schema = object_schema({"digest": digest_schema}, ["digest"])
    if digest_definitions:
        save_digest_schema["$defs"] = digest_definitions
    registry.register(
        ToolSpec(
            "list_dir",
            "列出当前 Agent 工作目录内的文件和子目录。",
            object_schema({"path": {"type": "string", "default": "."}}, []),
            list_dir,
        )
    )
    registry.register(
        ToolSpec(
            "read_file",
            "读取当前 Agent 工作目录内的 UTF-8 文本文件。",
            object_schema(
                {
                    "path": {"type": "string"},
                    "max_chars": {"type": "integer", "minimum": 1, "maximum": 50000},
                },
                ["path"],
            ),
            read_file,
        )
    )
    registry.register(
        ToolSpec(
            "search_content",
            "在当前 Agent 工作目录的文本文件中搜索关键词。",
            object_schema(
                {"keyword": {"type": "string"}, "dir": {"type": "string", "default": "."}},
                ["keyword"],
            ),
            search_content,
        )
    )
    registry.register(
        ToolSpec(
            "write_file",
            "向当前 Agent 工作目录写入中间分析或导出内容。",
            object_schema(
                {"path": {"type": "string"}, "content": {"type": "string"}},
                ["path", "content"],
            ),
            write_file,
        )
    )
    registry.register(
        ToolSpec(
            "bash",
            "在隔离工作目录中执行白名单命令，不支持管道、重定向或任意 Shell。",
            object_schema({"command": {"type": "string"}}, ["command"]),
            bash,
        )
    )
    registry.register(
        ToolSpec(
            "search_news",
            "从配置的可信 RSS 新闻源中搜索指定主题和时间范围的候选新闻。",
            object_schema(
                {
                    "query": {"type": "string"},
                    "start_time": {"type": "string", "format": "date-time"},
                    "end_time": {"type": "string", "format": "date-time"},
                    "sources": {"type": "array", "items": {"type": "string"}},
                    "language": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                ["query", "start_time", "end_time"],
            ),
            search_news,
        )
    )
    registry.register(
        ToolSpec(
            "search_x_accounts",
            (
                "通过 TwitterAPI.io Advanced Search 逐个检索用户配置的 X 关注账号。"
                "handles 必须来自 preferences.x_accounts；有候选的账号需至少选入一条简报。"
            ),
            object_schema(
                {
                    "handles": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 3,
                        "uniqueItems": True,
                    },
                    "start_time": {"type": "string", "format": "date-time"},
                    "end_time": {"type": "string", "format": "date-time"},
                    "limit_per_account": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 10,
                        "default": 5,
                    },
                },
                ["handles", "start_time", "end_time"],
            ),
            search_x_accounts,
        )
    )
    registry.register(
        ToolSpec(
            "fetch_article",
            "安全抓取新闻 URL 的正文、规范链接和页面元数据。",
            object_schema(
                {
                    "url": {"type": "string", "format": "uri"},
                    "max_chars": {"type": "integer", "minimum": 1000, "maximum": 30000},
                },
                ["url"],
            ),
            fetch_article,
        )
    )
    registry.register(
        ToolSpec(
            "query_news_history",
            "查询该用户近期已经收录或推送的新闻，用于跨日去重。",
            object_schema(
                {
                    "urls": {"type": "array", "items": {"type": "string"}},
                    "title_hashes": {"type": "array", "items": {"type": "string"}},
                    "keywords": {"type": "array", "items": {"type": "string"}},
                    "days": {"type": "integer", "minimum": 1, "maximum": 365},
                },
                [],
            ),
            query_news_history,
        )
    )
    registry.register(
        ToolSpec(
            "save_digest",
            "验证并保存结构化简报；新闻必须具有可验证来源。",
            save_digest_schema,
            save_digest,
        )
    )
    registry.register(
        ToolSpec(
            "push_digest",
            "把已保存简报推送到该用户已验证的站内或邮件渠道。",
            object_schema(
                {
                    "digest_id": {"type": "integer", "minimum": 1},
                    "channel": {"type": "string", "enum": ["in_app", "email"]},
                },
                ["digest_id", "channel"],
            ),
            push_digest,
        )
    )
    return registry

