"""Testing utilities for the Google Gemini SDK Temporal integration.

These let you exercise workflows that use
:class:`~temporalio.contrib.google_genai.TemporalAsyncClient` without making
real Gemini API calls.  Script the model's responses with :func:`text_response`
/ :func:`function_call_response`, build a plugin with
:class:`GeminiTestServer`, and register it on your worker like the real
:class:`~temporalio.contrib.google_genai.GoogleGenAIPlugin`.

Example::

    server = GeminiTestServer(
        [
            function_call_response("get_weather", {"city": "Tokyo"}),
            text_response("It's sunny in Tokyo."),
        ]
    )
    async with Worker(
        client,
        task_queue="test",
        workflows=[MyAgentWorkflow],
        activities=[get_weather],
        plugins=[server.plugin()],
    ):
        ...
    assert len(server.requests) == 2  # one per model turn
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from google.genai import Client as GeminiClient
from google.genai.types import HttpResponse as SdkHttpResponse

from temporalio.contrib.google_genai._google_genai_plugin import GoogleGenAIPlugin

__all__ = [
    "GeminiTestServer",
    "function_call_response",
    "text_response",
]


def text_response(text: str) -> str:
    """Build a ``generate_content`` response body with a single text part."""
    return json.dumps(
        {
            "candidates": [
                {
                    "content": {"role": "model", "parts": [{"text": text}]},
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 5,
                "candidatesTokenCount": 10,
            },
        }
    )


def function_call_response(name: str, args: dict[str, Any]) -> str:
    """Build a ``generate_content`` response body with a single function call.

    The Gemini SDK's automatic function calling loop will invoke the matching
    tool, then request another response — so pair each function-call response
    with a following :func:`text_response` (or further calls).
    """
    return json.dumps(
        {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [{"functionCall": {"name": name, "args": args}}],
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


class GeminiTestServer:
    """Scripts Gemini model responses so workflows run without real API calls.

    Pass canned response bodies built with :func:`text_response` /
    :func:`function_call_response`.  Each model call — including each turn of an
    automatic-function-calling loop and each ``generate_content_stream`` call —
    consumes the next response in order.  Build a plugin with :meth:`plugin`
    and register it on your worker; inspect :attr:`requests` afterwards to
    assert exactly what the integration sent.

    Only model calls (``client.models``) are scripted.  File, interaction, and
    agent operations are not; mock those on a ``genai.Client`` directly if a
    test needs them.
    """

    def __init__(self, responses: Sequence[str]) -> None:
        """Initialize with the response bodies to serve, in order."""
        self._responses = list(responses)
        self._index = 0
        self.requests: list[dict[str, Any]] = []

    def _next(self) -> str:
        idx = self._index
        self._index += 1
        if idx >= len(self._responses):
            raise AssertionError(
                f"GeminiTestServer ran out of responses (call {idx + 1}, "
                f"have {len(self._responses)}); script another response."
            )
        return self._responses[idx]

    def plugin(self) -> GoogleGenAIPlugin:
        """Return a :class:`GoogleGenAIPlugin` whose model calls serve the script.

        The real plugin activities run; only the underlying HTTP layer is
        replaced, so request formatting and the AFC loop are exercised exactly
        as in production.
        """
        client = GeminiClient(api_key="fake-test-key")

        async def fake_async_request(*_args: Any, **kwargs: Any) -> SdkHttpResponse:
            self.requests.append(dict(kwargs.get("request_dict") or {}))
            return SdkHttpResponse(
                headers={"content-type": "application/json"},
                body=self._next(),
            )

        async def fake_async_request_streamed(*_args: Any, **kwargs: Any) -> Any:
            self.requests.append(dict(kwargs.get("request_dict") or {}))
            body = self._next()

            async def _gen() -> Any:
                yield SdkHttpResponse(
                    headers={"content-type": "application/json"}, body=body
                )

            return _gen()

        client._api_client.async_request = fake_async_request  # type: ignore[assignment]
        client._api_client.async_request_streamed = fake_async_request_streamed  # type: ignore[assignment]
        return GoogleGenAIPlugin(client)
