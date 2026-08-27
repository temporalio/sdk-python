from datetime import timedelta
from typing import Any, cast

from mcp.types import (
    CallToolResult,
    GetPromptResult,
    ListPromptsResult,
    ListResourcesResult,
    ListResourceTemplatesResult,
    ListToolsResult,
    Prompt,
    PromptMessage,
    ReadResourceResult,
    Resource,
    ResourceTemplate,
    TextContent,
    TextResourceContents,
    Tool,
)

from temporalio import activity
from temporalio.contrib.mcp._activities import _MCPActivities
from temporalio.contrib.mcp._client import _MCPClientBackend


class FakeClient:
    protocol_version = "2026-07-28"

    def __init__(self) -> None:
        self.metas: list[dict[str, Any] | None] = []

    async def __aenter__(self) -> "FakeClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    def _page(self, cursor: str | None) -> tuple[str, str | None]:
        return ("one", "next") if cursor is None else ("two", None)

    async def list_tools(
        self, *, cursor: str | None, cache_mode: str
    ) -> ListToolsResult:
        assert cache_mode == "bypass"
        name, next_cursor = self._page(cursor)
        return ListToolsResult(
            tools=[Tool(name=name, input_schema={"type": "object"})],
            next_cursor=next_cursor,
        )

    async def list_prompts(self, *, cursor: str | None) -> ListPromptsResult:
        name, next_cursor = self._page(cursor)
        return ListPromptsResult(prompts=[Prompt(name=name)], next_cursor=next_cursor)

    async def list_resources(self, *, cursor: str | None) -> ListResourcesResult:
        name, next_cursor = self._page(cursor)
        return ListResourcesResult(
            resources=[Resource(name=name, uri=f"test://{name}")],
            next_cursor=next_cursor,
        )

    async def list_resource_templates(
        self, *, cursor: str | None
    ) -> ListResourceTemplatesResult:
        name, next_cursor = self._page(cursor)
        return ListResourceTemplatesResult(
            resource_templates=[
                ResourceTemplate(name=name, uri_template=f"test://{name}/{{id}}")
            ],
            next_cursor=next_cursor,
        )

    async def call_tool(
        self,
        name: str,
        _arguments: dict[str, Any] | None,
        *,
        meta: dict[str, Any] | None,
    ) -> CallToolResult:
        self.metas.append(meta)
        return CallToolResult(content=[TextContent(text=name)])

    async def get_prompt(
        self, name: str, _arguments: dict[str, str] | None
    ) -> GetPromptResult:
        return GetPromptResult(
            messages=[PromptMessage(role="user", content=TextContent(text=name))]
        )

    async def read_resource(self, uri: str) -> ReadResourceResult:
        return ReadResourceResult(
            contents=[TextResourceContents(uri=uri, text="contents")]
        )


async def test_operations_are_plain_json_and_lists_are_fully_paginated() -> None:
    client = FakeClient()
    support = _MCPActivities(
        {"test": lambda: _MCPClientBackend(cast(Any, client))},
        idle_timeout=timedelta(minutes=5),
    )
    functions: dict[str, Any] = {}
    for fn in support.activities:
        definition = activity._Definition.from_callable(fn)
        assert definition is not None and definition.name is not None
        functions[definition.name] = fn
    request: dict[str, Any] = {"factory_argument": None}
    try:
        for operation, result_key in (
            ("list-tools", "tools"),
            ("list-prompts", "prompts"),
            ("list-resources", "resources"),
            ("list-resource-templates", "resource_templates"),
        ):
            result = await functions[f"temporalio.contrib.mcp.test.{operation}"](
                request
            )
            assert [item["name"] for item in result[result_key]] == ["one", "two"]
            assert result["next_cursor"] is None

        tool_result = await functions["temporalio.contrib.mcp.test.call-tool"](
            {**request, "name": "echo", "arguments": {}, "meta": {"trace": "value"}}
        )
        assert tool_result["content"][0]["text"] == "echo"

        prompt_result = await functions["temporalio.contrib.mcp.test.get-prompt"](
            {**request, "name": "prompt", "arguments": {}}
        )
        assert prompt_result["messages"][0]["content"]["text"] == "prompt"

        resource_result = await functions["temporalio.contrib.mcp.test.read-resource"](
            {**request, "uri": "test://resource"}
        )
        assert resource_result["contents"][0]["text"] == "contents"

        # call_tool is the only operation that carries request metadata.
        assert client.metas == [{"trace": "value"}]
    finally:
        await support._pool.close()
