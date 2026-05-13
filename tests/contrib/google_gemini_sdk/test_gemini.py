"""Integration tests for the Google Gemini SDK Temporal integration.

Tests cover:
- Basic generate_content through workflow
- Tool calling via activity_as_tool (single arg, multi arg, class method)
- Workflow method as a plain tool (runs in-workflow, not as an activity)
- Tool failure propagation
- Multiple sequential tool calls with arg verification
- Batched streaming via generate_content_stream
- Per-request http_options propagation
- File upload (str path + io.BytesIO) and download via TemporalAsyncFiles
- File search store upload via TemporalAsyncFileSearchStores
- Multi-turn chat via client.chats
- TemporalAsyncClient wiring (files, file_search_stores)
- TemporalApiClient edge cases (sync raises)
- activity_as_tool validation and metadata preservation
- gemini_client configuration
"""

import inspect
import io
import json
import uuid
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from google.genai import Client as GeminiClient
from google.genai import types
from google.genai.types import HttpResponse as SdkHttpResponse

from temporalio import activity, workflow
from temporalio.client import Client, WorkflowFailureError
from temporalio.common import RetryPolicy
from temporalio.contrib.google_gemini_sdk import (
    GeminiPlugin,
    activity_as_tool,
    gemini_client,
)
from temporalio.contrib.google_gemini_sdk._models import (
    _GeminiApiRequest,
    _GeminiApiResponse,
    _GeminiApiStreamedResponse,
    _GeminiDownloadFileRequest,
    _GeminiUploadFileRequest,
    _GeminiUploadToFileSearchStoreRequest,
)
from temporalio.contrib.google_gemini_sdk._temporal_api_client import (
    TemporalApiClient,
)
from temporalio.contrib.google_gemini_sdk._temporal_async_client import (
    TemporalAsyncClient,
)
from temporalio.contrib.google_gemini_sdk._temporal_file_search_stores import (
    TemporalAsyncFileSearchStores,
)
from temporalio.contrib.google_gemini_sdk._temporal_files import (
    TemporalAsyncFiles,
)
from temporalio.exceptions import ApplicationError
from temporalio.workflow import ActivityConfig
from tests.helpers import new_worker

# ---------------------------------------------------------------------------
# Mock response helpers
# ---------------------------------------------------------------------------


def make_text_response(text: str) -> str:
    """Build a JSON body string for a simple text response."""
    return json.dumps(
        {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [{"text": text}],
                    },
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 5,
                "candidatesTokenCount": 10,
            },
        }
    )


def make_function_call_response(fn_name: str, args: dict) -> str:
    """Build a JSON body string for a function-call response."""
    return json.dumps(
        {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [{"functionCall": {"name": fn_name, "args": args}}],
                    },
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 10,
                "candidatesTokenCount": 15,
            },
        }
    )


# ---------------------------------------------------------------------------
# Tool call tracker — records every tool invocation for assertion
# ---------------------------------------------------------------------------


class ToolCallTracker:
    """Tracks tool invocations across activities and workflow methods.

    Each tool appends (name, args_dict) to ``calls`` so tests can assert
    exactly which tools were called, in what order, with what arguments.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    @activity.defn
    async def get_weather(self, city: str) -> str:
        """Get the weather for a given city."""
        self.calls.append(("get_weather", {"city": city}))
        return f"Weather in {city}: Sunny, 20C"

    @activity.defn
    async def get_weather_country(self, city: str, country: str) -> str:
        """Get the weather for a given city in a country."""
        self.calls.append(("get_weather_country", {"city": city, "country": country}))
        return f"Weather in {city}, {country}: Rainy, 15C"

    @activity.defn
    async def get_weather_failure(self, city: str) -> str:
        """Activity that always fails."""
        self.calls.append(("get_weather_failure", {"city": city}))
        raise ApplicationError("Weather service unavailable", non_retryable=True)


# ---------------------------------------------------------------------------
# Test helper: tracking gemini_api_client_async_request activity
# ---------------------------------------------------------------------------


class GeminiApiCallTracker:
    """A test replacement for the gemini_api_client activities.

    Records every ``_GeminiApiRequest`` received and returns canned
    ``_GeminiApiResponse`` bodies in order.  After the workflow completes,
    inspect ``requests`` to verify exactly what the integration sent.

    For streamed requests, the mock response is split into per-line chunks
    to simulate multiple streamed chunks.

    The real ``GeminiPlugin`` is still used for its data converter, sandbox
    passthrough, and workflow runner configuration — only its activity
    registration is suppressed so this tracker can take its place.
    """

    def __init__(self, mock_responses: list[str]) -> None:
        self._mock_responses = mock_responses
        self.requests: list[_GeminiApiRequest] = []
        self.file_upload_requests: list[_GeminiUploadFileRequest] = []
        self.file_download_requests: list[_GeminiDownloadFileRequest] = []
        self.file_search_store_upload_requests: list[
            _GeminiUploadToFileSearchStoreRequest
        ] = []
        self._call_index = 0

    def _next_response(self, req: _GeminiApiRequest) -> str:
        self.requests.append(req)
        idx = self._call_index
        self._call_index += 1
        if idx >= len(self._mock_responses):
            raise ApplicationError(
                f"No more mock responses (called {idx + 1} times, "
                f"have {len(self._mock_responses)})",
                non_retryable=True,
            )
        return self._mock_responses[idx]

    @activity.defn
    async def gemini_api_client_async_request(
        self, req: _GeminiApiRequest
    ) -> _GeminiApiResponse:
        return _GeminiApiResponse(
            headers={"content-type": "application/json"},
            body=self._next_response(req),
        )

    @activity.defn
    async def gemini_api_client_async_request_streamed(
        self, req: _GeminiApiRequest
    ) -> _GeminiApiStreamedResponse:
        body = self._next_response(req)
        # Split the response text into word-level chunks so tests can
        # verify that multiple chunks are yielded back to the workflow.
        parsed = json.loads(body)
        full_text = (
            parsed.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "")
        )
        words = full_text.split()
        chunks = []
        for word in words:
            chunks.append(
                _GeminiApiResponse(
                    headers={"content-type": "application/json"},
                    body=make_text_response(word),
                )
            )
        return _GeminiApiStreamedResponse(chunks=chunks)

    @activity.defn
    async def gemini_files_upload(self, req: _GeminiUploadFileRequest) -> types.File:
        self.file_upload_requests.append(req)
        return types.File(
            name="files/test-uploaded-file",
            uri="https://fake.uri/files/test-uploaded-file",
            size_bytes=len(req.file_bytes) if req.file_bytes else 0,
        )

    @activity.defn
    async def gemini_files_download(self, req: _GeminiDownloadFileRequest) -> bytes:
        self.file_download_requests.append(req)
        return b"fake file content"

    @activity.defn
    async def gemini_file_search_stores_upload(
        self, req: _GeminiUploadToFileSearchStoreRequest
    ) -> types.UploadToFileSearchStoreOperation:
        self.file_search_store_upload_requests.append(req)
        return types.UploadToFileSearchStoreOperation.model_construct(
            name="operations/test-op",
        )


def apply_plugin(
    client: Client, mock_responses: list[str]
) -> tuple[Client, GeminiApiCallTracker]:
    """Create a real GeminiPlugin whose activities include a tracking fake.

    Monkey-patches ``GeminiApiCaller.activities`` so that when the plugin
    constructs itself, it registers our tracking activity instead of
    the real ones.  Everything else — data converter, sandbox passthrough,
    workflow runner — is the real plugin code.

    Returns the configured Temporal client and the tracker.
    """
    from temporalio.contrib.google_gemini_sdk._gemini_activity import GeminiApiCaller

    tracker = GeminiApiCallTracker(mock_responses)
    original_activities = GeminiApiCaller.activities
    GeminiApiCaller.activities = lambda self: [  # type: ignore[method-assign]
        tracker.gemini_api_client_async_request,
        tracker.gemini_api_client_async_request_streamed,
        tracker.gemini_files_upload,
        tracker.gemini_files_download,
        tracker.gemini_file_search_stores_upload,
    ]
    try:
        gemini = GeminiClient(api_key="fake-test-key")
        plugin = GeminiPlugin(gemini)
    finally:
        GeminiApiCaller.activities = original_activities  # type: ignore[method-assign]

    config = client.config()
    config["plugins"] = [plugin]
    return Client(**config), tracker


# ---------------------------------------------------------------------------
# Workflows
# ---------------------------------------------------------------------------


@workflow.defn
class SimpleGenerateWorkflow:
    """Workflow that does a simple generate_content call."""

    @workflow.run
    async def run(self, prompt: str) -> str:
        client = gemini_client()
        response = await client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        return response.text or ""


@workflow.defn
class SingleArgToolWorkflow:
    """Workflow that uses activity_as_tool for a single-arg tool."""

    @workflow.run
    async def run(self, prompt: str) -> str:
        client = gemini_client()
        response = await client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[
                    activity_as_tool(
                        ToolCallTracker.get_weather,
                        activity_config=ActivityConfig(
                            start_to_close_timeout=timedelta(seconds=10),
                        ),
                    ),
                ],
            ),
        )
        return response.text or ""


@workflow.defn
class MultiArgToolWorkflow:
    """Workflow with multi-arg tool."""

    @workflow.run
    async def run(self, prompt: str) -> str:
        client = gemini_client()
        response = await client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[
                    activity_as_tool(
                        ToolCallTracker.get_weather_country,
                        activity_config=ActivityConfig(
                            start_to_close_timeout=timedelta(seconds=10),
                        ),
                    ),
                ],
            ),
        )
        return response.text or ""


@workflow.defn
class ToolFailureWorkflow:
    """Workflow with a tool that always fails."""

    @workflow.run
    async def run(self, prompt: str) -> str:
        client = gemini_client()
        response = await client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[
                    activity_as_tool(
                        ToolCallTracker.get_weather_failure,
                        activity_config=ActivityConfig(
                            start_to_close_timeout=timedelta(seconds=10),
                            retry_policy=RetryPolicy(maximum_attempts=1),
                        ),
                    ),
                ],
            ),
        )
        return response.text or ""


@workflow.defn
class MultipleToolsWorkflow:
    """Workflow with multiple tools that are called in sequence."""

    @workflow.run
    async def run(self, prompt: str) -> str:
        client = gemini_client()
        response = await client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[
                    activity_as_tool(
                        ToolCallTracker.get_weather,
                        activity_config=ActivityConfig(
                            start_to_close_timeout=timedelta(seconds=10),
                        ),
                    ),
                    activity_as_tool(
                        ToolCallTracker.get_weather_country,
                        activity_config=ActivityConfig(
                            start_to_close_timeout=timedelta(seconds=10),
                        ),
                    ),
                ],
            ),
        )
        return response.text or ""


@workflow.defn
class WorkflowMethodToolWorkflow:
    """Workflow that passes a plain method as a tool (runs in-workflow, not as an activity)."""

    def __init__(self) -> None:
        self.tool_calls: list[tuple[str, dict]] = []

    @workflow.run
    async def run(self, prompt: str) -> str:
        client = gemini_client()
        response = await client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[self.lookup_city],
            ),
        )
        return response.text or ""

    async def lookup_city(self, city: str) -> str:
        """Look up info about a city."""
        self.tool_calls.append(("lookup_city", {"city": city}))
        return f"{city} is a great place to visit"

    @workflow.query
    def get_tool_calls(self) -> list[tuple[str, dict]]:
        return self.tool_calls


@workflow.defn
class StreamedGenerateWorkflow:
    """Workflow that uses generate_content_stream."""

    @workflow.run
    async def run(self, prompt: str) -> list[str]:
        client = gemini_client()
        chunks: list[str] = []
        async for chunk in await client.models.generate_content_stream(
            model="gemini-2.5-flash",
            contents=prompt,
        ):
            if chunk.text:
                chunks.append(chunk.text)
        return chunks


@workflow.defn
class HttpOptionsWorkflow:
    """Workflow that passes per-request http_options through generate_content."""

    @workflow.run
    async def run(self, prompt: str, http_options: types.HttpOptionsDict) -> str:
        client = gemini_client()
        response = await client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                http_options=types.HttpOptions.model_validate(http_options),
            ),
        )
        return response.text or ""


@workflow.defn
class FullIntegrationWorkflow:
    """Exercises every activity path in a single workflow run.

    Uses the real GeminiPlugin activities (not the tracker), so this
    tests the actual activity implementations end-to-end with a mocked
    genai.Client.
    """

    @workflow.run
    async def run(self, prompt: str) -> dict[str, Any]:
        client = gemini_client()
        results: dict[str, Any] = {}

        # 1. generate_content (async_request activity)
        response = await client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        results["generate"] = response.text or ""

        # 2. generate_content_stream (async_request_streamed activity)
        chunks: list[str] = []
        async for chunk in await client.models.generate_content_stream(
            model="gemini-2.5-flash",
            contents=prompt,
        ):
            if chunk.text:
                chunks.append(chunk.text)
        results["stream_chunks"] = chunks

        # 3. files.upload (gemini_files_upload activity)
        uploaded = await client.files.upload(
            file="/tmp/fake.txt",
            config=types.UploadFileConfig(display_name="Integration Test"),
        )
        results["upload_name"] = uploaded.name or ""

        # 4. files.download (gemini_files_download activity)
        data = await client.files.download(file="files/some-file")
        results["download"] = data.decode() if isinstance(data, bytes) else str(data)

        # 5. file_search_stores.upload_to_file_search_store activity
        store_name = "fileSearchStores/test"
        op = await client.file_search_stores.upload_to_file_search_store(
            file_search_store_name=store_name,
            file="/tmp/doc.txt",
        )
        results["fss_upload_op"] = op.name or ""

        # 6. generate_content grounded with file_search tool (RAG query)
        rag_response = await client.models.generate_content(
            model="gemini-2.5-flash",
            contents="What does the document say?",
            config=types.GenerateContentConfig(
                tools=[
                    types.Tool(
                        file_search=types.FileSearch(
                            file_search_store_names=[store_name],
                        ),
                    ),
                ],
            ),
        )
        results["rag"] = rag_response.text or ""

        # 7. Clean up the file search store
        await client.file_search_stores.delete(
            name=store_name,
            config=types.DeleteFileSearchStoreConfig(force=True),
        )
        results["store_deleted"] = True

        return results


@workflow.defn
class FileUploadStrWorkflow:
    """Workflow that uploads a file via str path."""

    @workflow.run
    async def run(self, file_path: str) -> str:
        client = gemini_client()
        uploaded = await client.files.upload(
            file=file_path,
            config=types.UploadFileConfig(
                display_name="Test File",
                mime_type="text/plain",
            ),
        )
        return uploaded.name or ""


@workflow.defn
class FileUploadBytesWorkflow:
    """Workflow that uploads a file via io.BytesIO."""

    @workflow.run
    async def run(self, data: bytes) -> str:
        client = gemini_client()
        uploaded = await client.files.upload(
            file=io.BytesIO(data),
            config=types.UploadFileConfig(
                display_name="Bytes File",
                mime_type="text/plain",
            ),
        )
        return uploaded.name or ""


@workflow.defn
class FileDownloadWorkflow:
    """Workflow that downloads a file by name."""

    @workflow.run
    async def run(self, file_name: str) -> bytes:
        client = gemini_client()
        return await client.files.download(file=file_name)


@workflow.defn
class FileSearchStoreUploadWorkflow:
    """Workflow that uploads to a file search store."""

    @workflow.run
    async def run(self, store_name: str, file_path: str) -> str:
        client = gemini_client()
        op = await client.file_search_stores.upload_to_file_search_store(
            file_search_store_name=store_name,
            file=file_path,
            config=types.UploadToFileSearchStoreConfig(
                display_name="Test Doc",
                mime_type="text/plain",
            ),
        )
        return op.name or ""


@workflow.defn
class RegisterFilesWorkflow:
    """Workflow that calls files.register_files."""

    @workflow.run
    async def run(self, uris: list[str]) -> str:
        client = gemini_client()
        # auth arg is ignored by TemporalAsyncFiles — the activity uses
        # credentials from GeminiPlugin init.  We pass a dummy here;
        # can't import google.auth.credentials in the sandbox so we
        # use a sentinel that satisfies the type at runtime.
        resp = await client.files.register_files(
            auth=None,  # type: ignore[arg-type]
            uris=uris,
        )
        return str(len(resp.files or []))


@workflow.defn
class ChatWorkflow:
    """Workflow that uses client.chats for multi-turn conversation."""

    @workflow.run
    async def run(self, prompt: str) -> list[str]:
        client = gemini_client()
        chat = client.chats.create(
            model="gemini-2.5-flash",
        )
        r1 = await chat.send_message(prompt)
        r2 = await chat.send_message("Follow up question")
        return [r1.text or "", r2.text or ""]


# ===========================================================================
# Integration tests — run workflows against a real Temporal test server
# ===========================================================================


async def test_simple_generate_content(client: Client):
    """Basic generate_content returns text through a workflow."""
    new_client, _ = apply_plugin(client, [make_text_response("Hello from Gemini!")])

    async with new_worker(new_client, SimpleGenerateWorkflow) as worker:
        result = await new_client.execute_workflow(
            SimpleGenerateWorkflow.run,
            "Say hello",
            id=f"gemini-simple-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=10),
        )

    assert result == "Hello from Gemini!"


async def test_tool_call_single_arg(client: Client):
    """Tool calling with a single-argument activity via AFC."""
    tool_tracker = ToolCallTracker()
    new_client, _ = apply_plugin(
        client,
        [
            make_function_call_response("get_weather", {"city": "Tokyo"}),
            make_text_response("The weather in Tokyo is sunny and 20C."),
        ],
    )

    async with new_worker(
        new_client,
        SingleArgToolWorkflow,
        activities=[tool_tracker.get_weather],
    ) as worker:
        result = await new_client.execute_workflow(
            SingleArgToolWorkflow.run,
            "What's the weather in Tokyo?",
            id=f"gemini-tool-single-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=10),
        )

    assert tool_tracker.calls == [("get_weather", {"city": "Tokyo"})]
    assert result == "The weather in Tokyo is sunny and 20C."


async def test_tool_call_multi_arg(client: Client):
    """Tool calling with a multi-argument activity."""
    tool_tracker = ToolCallTracker()
    new_client, _ = apply_plugin(
        client,
        [
            make_function_call_response(
                "get_weather_country", {"city": "Paris", "country": "France"}
            ),
            make_text_response("Paris, France: Rainy, 15C."),
        ],
    )

    async with new_worker(
        new_client,
        MultiArgToolWorkflow,
        activities=[tool_tracker.get_weather_country],
    ) as worker:
        result = await new_client.execute_workflow(
            MultiArgToolWorkflow.run,
            "What's the weather in Paris, France?",
            id=f"gemini-tool-multi-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=10),
        )

    assert tool_tracker.calls == [
        ("get_weather_country", {"city": "Paris", "country": "France"})
    ]
    assert result == "Paris, France: Rainy, 15C."


async def test_tool_failure_propagation(client: Client):
    """Tool activity failure causes the workflow to fail."""
    tool_tracker = ToolCallTracker()
    new_client, _ = apply_plugin(
        client,
        [
            make_function_call_response("get_weather_failure", {"city": "Nowhere"}),
        ],
    )

    async with new_worker(
        new_client,
        ToolFailureWorkflow,
        activities=[tool_tracker.get_weather_failure],
    ) as worker:
        with pytest.raises(WorkflowFailureError):
            await new_client.execute_workflow(
                ToolFailureWorkflow.run,
                "Weather in Nowhere?",
                id=f"gemini-tool-fail-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=10),
            )

    assert tool_tracker.calls == [("get_weather_failure", {"city": "Nowhere"})]


async def test_multiple_tools_sequential(client: Client):
    """Multiple tools called in sequence within one generate_content call."""
    tool_tracker = ToolCallTracker()
    new_client, _ = apply_plugin(
        client,
        [
            make_function_call_response("get_weather", {"city": "Tokyo"}),
            make_function_call_response(
                "get_weather_country", {"city": "Paris", "country": "France"}
            ),
            make_text_response("Tokyo is sunny; Paris is rainy."),
        ],
    )

    async with new_worker(
        new_client,
        MultipleToolsWorkflow,
        activities=[
            tool_tracker.get_weather,
            tool_tracker.get_weather_country,
        ],
    ) as worker:
        result = await new_client.execute_workflow(
            MultipleToolsWorkflow.run,
            "Compare Tokyo and Paris weather",
            id=f"gemini-multi-tools-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=15),
        )

    assert tool_tracker.calls == [
        ("get_weather", {"city": "Tokyo"}),
        ("get_weather_country", {"city": "Paris", "country": "France"}),
    ]
    assert result == "Tokyo is sunny; Paris is rainy."


async def test_workflow_method_as_tool(client: Client):
    """A plain workflow method (not an activity) used as a tool runs in-workflow."""
    new_client, _ = apply_plugin(
        client,
        [
            make_function_call_response("lookup_city", {"city": "Berlin"}),
            make_text_response("Berlin is wonderful."),
        ],
    )

    async with new_worker(new_client, WorkflowMethodToolWorkflow) as worker:
        handle = await new_client.start_workflow(
            WorkflowMethodToolWorkflow.run,
            "Tell me about Berlin",
            id=f"gemini-wf-method-tool-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=10),
        )
        result = await handle.result()
        # Query must happen while worker is alive
        tool_calls = await handle.query(WorkflowMethodToolWorkflow.get_tool_calls)

    assert tool_calls == [("lookup_city", {"city": "Berlin"})]
    assert result == "Berlin is wonderful."


async def test_streamed_generate_content(client: Client):
    """generate_content_stream collects batched chunks from the activity."""
    new_client, _ = apply_plugin(
        client, [make_text_response("The quick brown fox jumps over the lazy dog")]
    )

    async with new_worker(new_client, StreamedGenerateWorkflow) as worker:
        result = await new_client.execute_workflow(
            StreamedGenerateWorkflow.run,
            "Say something",
            id=f"gemini-streamed-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=10),
        )

    # The tracker splits the text into per-word chunks
    assert len(result) == 9
    assert " ".join(result) == "The quick brown fox jumps over the lazy dog"


# ===========================================================================
# http_options propagation tests - per request overrides
# ===========================================================================


async def test_http_options_headers_propagate(client: Client):
    """Custom headers passed via http_options arrive at the activity."""
    new_client, api_tracker = apply_plugin(client, [make_text_response("ok")])

    async with new_worker(new_client, HttpOptionsWorkflow) as worker:
        await new_client.execute_workflow(
            HttpOptionsWorkflow.run,
            args=["hi", {"headers": {"X-Custom": "test-value"}}],
            id=f"gemini-http-headers-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=10),
        )

    assert len(api_tracker.requests) == 1
    opts = api_tracker.requests[0].http_options_overrides
    assert opts is not None
    assert opts.headers == {"X-Custom": "test-value"}


async def test_http_options_api_version_propagates(client: Client):
    """api_version passed via http_options arrives at the activity."""
    new_client, api_tracker = apply_plugin(client, [make_text_response("ok")])

    async with new_worker(new_client, HttpOptionsWorkflow) as worker:
        await new_client.execute_workflow(
            HttpOptionsWorkflow.run,
            args=["hi", {"api_version": "v1"}],
            id=f"gemini-http-version-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=10),
        )

    assert len(api_tracker.requests) == 1
    opts = api_tracker.requests[0].http_options_overrides
    assert opts is not None
    assert opts.api_version == "v1"


async def test_http_options_base_url_propagates(client: Client):
    """base_url passed via http_options arrives at the activity."""
    new_client, api_tracker = apply_plugin(client, [make_text_response("ok")])

    async with new_worker(new_client, HttpOptionsWorkflow) as worker:
        await new_client.execute_workflow(
            HttpOptionsWorkflow.run,
            args=["hi", {"base_url": "https://custom.example.com"}],
            id=f"gemini-http-base-url-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=10),
        )

    assert len(api_tracker.requests) == 1
    opts = api_tracker.requests[0].http_options_overrides
    assert opts is not None
    assert opts.base_url == "https://custom.example.com"


async def test_http_options_multiple_fields_propagate(client: Client):
    """Multiple http_options fields propagate together to the activity."""
    new_client, api_tracker = apply_plugin(client, [make_text_response("ok")])

    async with new_worker(new_client, HttpOptionsWorkflow) as worker:
        await new_client.execute_workflow(
            HttpOptionsWorkflow.run,
            args=[
                "hi",
                {
                    "api_version": "v1beta",
                    "headers": {"X-Foo": "bar"},
                    "base_url": "https://other.example.com",
                },
            ],
            id=f"gemini-http-multi-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=10),
        )

    assert len(api_tracker.requests) == 1
    opts = api_tracker.requests[0].http_options_overrides
    assert opts is not None
    assert opts.api_version == "v1beta"
    assert opts.headers == {"X-Foo": "bar"}
    assert opts.base_url == "https://other.example.com"


async def test_no_http_options_passes_none(client: Client):
    """When no per-request http_options are set, None reaches the activity."""
    new_client, api_tracker = apply_plugin(client, [make_text_response("ok")])

    async with new_worker(new_client, SimpleGenerateWorkflow) as worker:
        await new_client.execute_workflow(
            SimpleGenerateWorkflow.run,
            "hi",
            id=f"gemini-http-none-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=10),
        )

    assert len(api_tracker.requests) == 1
    assert api_tracker.requests[0].http_options_overrides is None


# ===========================================================================
# File upload/download tests
# ===========================================================================


async def test_file_upload_str_path(client: Client):
    """Upload a file via str path dispatches through the activity."""
    new_client, api_tracker = apply_plugin(client, [])

    async with new_worker(new_client, FileUploadStrWorkflow) as worker:
        result = await new_client.execute_workflow(
            FileUploadStrWorkflow.run,
            "/tmp/test.txt",
            id=f"gemini-file-upload-str-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=10),
        )

    assert len(api_tracker.file_upload_requests) == 1
    req = api_tracker.file_upload_requests[0]
    assert req.file_path == "/tmp/test.txt"
    assert req.file_bytes is None
    assert req.config is not None
    assert req.config.display_name == "Test File"
    assert result == "files/test-uploaded-file"


async def test_file_upload_bytes(client: Client):
    """Upload a file via io.BytesIO sends bytes through the activity."""
    new_client, api_tracker = apply_plugin(client, [])

    async with new_worker(new_client, FileUploadBytesWorkflow) as worker:
        result = await new_client.execute_workflow(
            FileUploadBytesWorkflow.run,
            b"hello world",
            id=f"gemini-file-upload-bytes-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=10),
        )

    assert len(api_tracker.file_upload_requests) == 1
    req = api_tracker.file_upload_requests[0]
    assert req.file_bytes == b"hello world"
    assert req.file_path is None
    assert req.config is not None
    assert req.config.display_name == "Bytes File"
    assert result == "files/test-uploaded-file"


async def test_file_download(client: Client):
    """Download a file dispatches through the activity and returns bytes."""
    new_client, api_tracker = apply_plugin(client, [])

    async with new_worker(new_client, FileDownloadWorkflow) as worker:
        result = await new_client.execute_workflow(
            FileDownloadWorkflow.run,
            "files/some-file",
            id=f"gemini-file-download-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=10),
        )

    assert len(api_tracker.file_download_requests) == 1
    assert api_tracker.file_download_requests[0].file == "files/some-file"
    assert result == b"fake file content"


# ===========================================================================
# File search store upload tests
# ===========================================================================


async def test_file_search_store_upload(client: Client):
    """Upload to file search store dispatches through the activity."""
    new_client, api_tracker = apply_plugin(client, [])

    async with new_worker(new_client, FileSearchStoreUploadWorkflow) as worker:
        result = await new_client.execute_workflow(
            FileSearchStoreUploadWorkflow.run,
            args=["fileSearchStores/my-store", "/tmp/doc.txt"],
            id=f"gemini-fss-upload-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=10),
        )

    assert len(api_tracker.file_search_store_upload_requests) == 1
    req = api_tracker.file_search_store_upload_requests[0]
    assert req.file_search_store_name == "fileSearchStores/my-store"
    assert req.file_path == "/tmp/doc.txt"
    assert req.config is not None
    assert req.config.display_name == "Test Doc"
    assert result == "operations/test-op"


# ===========================================================================
# Multi-turn chat tests
# ===========================================================================


async def test_chat_multi_turn(client: Client):
    """Multi-turn chat sends multiple requests through the activity."""
    new_client, api_tracker = apply_plugin(
        client,
        [
            make_text_response("First answer"),
            make_text_response("Second answer"),
        ],
    )

    async with new_worker(new_client, ChatWorkflow) as worker:
        result = await new_client.execute_workflow(
            ChatWorkflow.run,
            "Hello",
            id=f"gemini-chat-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=10),
        )

    assert len(api_tracker.requests) == 2
    assert result == ["First answer", "Second answer"]


# ===========================================================================
# Full integration test — real activities, mocked client
# ===========================================================================


def _apply_plugin_with_mock_client(client: Client, mock_responses: list[str]) -> Client:
    """Create a real GeminiPlugin with real activities but a mocked client.

    Unlike ``apply_plugin``, this does NOT replace the activities.  The
    real ``GeminiApiCaller.activities()`` are registered, exercising the
    full activity code path.  The underlying ``genai.Client`` HTTP layer
    and high-level file methods are mocked so no network calls are made.
    """
    gemini = GeminiClient(api_key="fake-test-key")

    call_state = {"index": 0}

    async def fake_async_request(*_args: Any, **_kwargs: Any) -> SdkHttpResponse:
        idx = call_state["index"]
        call_state["index"] += 1
        if idx >= len(mock_responses):
            raise RuntimeError(
                f"No more mock responses (called {idx + 1} times, "
                f"have {len(mock_responses)})"
            )
        return SdkHttpResponse(
            headers={"content-type": "application/json"},
            body=mock_responses[idx],
        )

    async def fake_async_request_streamed(*_args: Any, **_kwargs: Any) -> Any:
        idx = call_state["index"]
        call_state["index"] += 1
        if idx >= len(mock_responses):
            raise RuntimeError(
                f"No more mock responses (called {idx + 1} times, "
                f"have {len(mock_responses)})"
            )

        async def _gen():
            yield SdkHttpResponse(
                headers={"content-type": "application/json"},
                body=mock_responses[idx],
            )

        return _gen()

    gemini._api_client.async_request = fake_async_request  # type: ignore[assignment]
    gemini._api_client.async_request_streamed = fake_async_request_streamed  # type: ignore[assignment]

    # Mock file operations at the high-level SDK interface (these are what
    # the real activities call).
    gemini.aio.files.upload = AsyncMock(  # type: ignore[method-assign]
        return_value=types.File(
            name="files/mock-uploaded",
            uri="https://fake.uri/files/mock-uploaded",
            size_bytes=42,
        )
    )
    gemini.aio.files.download = AsyncMock(return_value=b"mock download content")  # type: ignore[method-assign]
    gemini.aio.file_search_stores.upload_to_file_search_store = AsyncMock(  # type: ignore[method-assign]
        return_value=types.UploadToFileSearchStoreOperation.model_construct(
            name="operations/mock-op"
        )
    )

    plugin = GeminiPlugin(gemini)
    config = client.config()
    config["plugins"] = [plugin]
    return Client(**config)


async def test_full_integration_with_mock_client(client: Client):
    """Run a workflow through real activities with a mocked genai.Client.

    This is the only test that exercises the actual activity implementations
    in _gemini_activity.py.  Every other test uses the GeminiApiCallTracker
    which replaces the activities entirely.
    """
    # Mock responses are consumed in order by the async_request and
    # async_request_streamed mocks.  Steps 3-5 (file upload, download,
    # store upload) are mocked separately at the SDK level and don't
    # consume from this list.
    new_client = _apply_plugin_with_mock_client(
        client,
        [
            make_text_response("Real activity response"),  # generate_content
            make_text_response("Streamed via real activity"),  # generate_content_stream
            make_text_response("Grounded RAG answer"),  # RAG query with file_search
            make_text_response(""),  # file_search_stores.delete
        ],
    )

    async with new_worker(new_client, FullIntegrationWorkflow) as worker:
        result = await new_client.execute_workflow(
            FullIntegrationWorkflow.run,
            "test prompt",
            id=f"gemini-full-integration-{uuid.uuid4()}",
            task_queue=worker.task_queue,
            execution_timeout=timedelta(seconds=15),
        )

    assert result["generate"] == "Real activity response"
    assert len(result["stream_chunks"]) > 0
    assert "Streamed" in " ".join(result["stream_chunks"])
    assert result["upload_name"] == "files/mock-uploaded"
    assert result["download"] == "mock download content"
    assert result["fss_upload_op"] == "operations/mock-op"
    assert result["rag"] == "Grounded RAG answer"
    assert result["store_deleted"] is True


async def test_register_files_without_credentials_fails(client: Client):
    """register_files raises when no credentials are available."""
    # _apply_plugin_with_mock_client uses api_key auth with no
    # extra_credentials, so the activity should raise ValueError.
    new_client = _apply_plugin_with_mock_client(client, [])

    async with new_worker(new_client, RegisterFilesWorkflow) as worker:
        with pytest.raises(WorkflowFailureError) as exc_info:
            await new_client.execute_workflow(
                RegisterFilesWorkflow.run,
                ["gs://bucket/file.txt"],
                id=f"gemini-register-no-creds-{uuid.uuid4()}",
                task_queue=worker.task_queue,
                execution_timeout=timedelta(seconds=10),
            )

    # The error is nested: WorkflowFailureError → ActivityError → ApplicationError
    cause = exc_info.value.cause
    while cause.__cause__ is not None:
        cause = cause.__cause__
    assert "No credentials available for register_files" in str(cause)


# ===========================================================================
# TemporalAsyncClient wiring tests
# ===========================================================================


def test_temporal_async_client_has_temporal_files():
    """gemini_client() returns a client with TemporalAsyncFiles."""
    client = gemini_client()
    assert isinstance(client, TemporalAsyncClient)
    assert isinstance(client.files, TemporalAsyncFiles)


def test_temporal_async_client_has_temporal_file_search_stores():
    """gemini_client() returns a client with TemporalAsyncFileSearchStores."""
    client = gemini_client()
    assert isinstance(client.file_search_stores, TemporalAsyncFileSearchStores)


# ===========================================================================
# Unit tests for TemporalApiClient
# ===========================================================================


def test_sync_request_raises():
    """Synchronous request() raises RuntimeError."""
    api_client = TemporalApiClient()
    with pytest.raises(RuntimeError, match="Synchronous requests are not supported"):
        api_client.request("GET", "/test", {})


def test_sync_request_streamed_raises():
    """Synchronous request_streamed() raises RuntimeError."""
    api_client = TemporalApiClient()
    with pytest.raises(RuntimeError, match="Synchronous streaming is not supported"):
        api_client.request_streamed("GET", "/test", {})


def test_upload_file_raises():
    """Low-level upload_file() raises NotImplementedError."""
    api_client = TemporalApiClient()
    with pytest.raises(NotImplementedError, match="client.files.upload"):
        api_client.upload_file()


def test_download_file_raises():
    """Low-level download_file() raises NotImplementedError."""
    api_client = TemporalApiClient()
    with pytest.raises(NotImplementedError, match="client.files.download"):
        api_client.download_file()


# ===========================================================================
# Unit tests for activity_as_tool
# ===========================================================================


def test_activity_as_tool_bare_function_raises():
    """activity_as_tool rejects a function without @activity.defn."""

    async def not_an_activity(x: str) -> str:
        return x

    with pytest.raises(ApplicationError, match="@activity.defn"):
        activity_as_tool(not_an_activity)


def test_activity_as_tool_preserves_name():
    """Returned wrapper keeps the original function name."""
    wrapper = activity_as_tool(ToolCallTracker.get_weather)
    assert wrapper.__name__ == "get_weather"


def test_activity_as_tool_preserves_doc():
    """Returned wrapper keeps the original docstring."""
    wrapper = activity_as_tool(ToolCallTracker.get_weather)
    assert wrapper.__doc__ == "Get the weather for a given city."


def test_activity_as_tool_preserves_signature():
    """Returned wrapper has the correct parameter signature (self hidden)."""
    wrapper = activity_as_tool(ToolCallTracker.get_weather)
    sig = inspect.signature(wrapper)
    params = list(sig.parameters.keys())
    assert params == ["city"]


def test_activity_as_tool_multi_arg_signature():
    """Multi-arg activity preserves all parameter names (self hidden)."""
    wrapper = activity_as_tool(ToolCallTracker.get_weather_country)
    sig = inspect.signature(wrapper)
    params = list(sig.parameters.keys())
    assert params == ["city", "country"]


def test_activity_as_tool_is_async_callable():
    """Returned wrapper is an async callable."""
    wrapper = activity_as_tool(ToolCallTracker.get_weather)
    assert inspect.iscoroutinefunction(wrapper)


# ===========================================================================
# Unit tests for gemini_client
# ===========================================================================


def test_gemini_client_vertexai_config():
    """gemini_client() forwards Vertex AI configuration to the TemporalApiClient."""
    result = gemini_client(vertexai=True, project="proj", location="us-central1")
    assert result._api_client.vertexai is True
    assert result._api_client.project == "proj"
    assert result._api_client.location == "us-central1"


# ===========================================================================
# Unit tests for io.IOBase text-stream rejection
# ===========================================================================


async def test_file_upload_text_stream_raises():
    """TemporalAsyncFiles.upload rejects text streams with a clear TypeError."""
    files = TemporalAsyncFiles(TemporalApiClient())
    with pytest.raises(
        TypeError, match="file must be a binary stream when passing an io.IOBase"
    ):
        await files.upload(file=io.StringIO("text"))


async def test_file_search_store_upload_text_stream_raises():
    """TemporalAsyncFileSearchStores.upload_to_file_search_store rejects text streams."""
    stores = TemporalAsyncFileSearchStores(TemporalApiClient())
    with pytest.raises(
        TypeError, match="file must be a binary stream when passing an io.IOBase"
    ):
        await stores.upload_to_file_search_store(
            file_search_store_name="fileSearchStores/x",
            file=io.StringIO("text"),
        )
