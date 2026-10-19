from app.tools import build_tool_registry


def test_registry_contains_expected_tools() -> None:
    registry = build_tool_registry()
    assert registry.names == [
        "list_dir",
        "read_file",
        "search_content",
        "write_file",
        "bash",
        "search_news",
        "search_x_accounts",
        "fetch_article",
        "query_news_history",
        "save_digest",
        "push_digest",
    ]


def test_all_tools_have_closed_object_schemas() -> None:
    for tool in build_tool_registry().api_tools():
        parameters = tool["function"]["parameters"]
        assert parameters["type"] == "object"
        assert parameters["additionalProperties"] is False


def test_save_digest_exposes_resolvable_nested_schema() -> None:
    save_tool = next(
        tool
        for tool in build_tool_registry().api_tools()
        if tool["function"]["name"] == "save_digest"
    )
    parameters = save_tool["function"]["parameters"]
    assert "DigestItemDraft" in parameters["$defs"]
    assert parameters["properties"]["digest"]["properties"]["items"]["items"]["$ref"] == (
        "#/$defs/DigestItemDraft"
    )

