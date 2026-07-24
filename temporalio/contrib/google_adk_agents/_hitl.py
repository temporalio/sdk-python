"""Human-in-the-loop helpers for Google ADK agents running in Temporal workflows.

ADK pauses a run by emitting an event that carries a special function call
(``adk_request_input`` for human-input nodes, ``adk_request_confirmation`` for
tool confirmation, ``adk_request_credential`` for auth) and resumes when a
later user message answers it with a matching ``FunctionResponse``. Inside a
Temporal workflow the pause maps naturally onto a durable wait: collect the
pending requests from the events yielded by ``runner.run_async``, expose them
via a query, wait for responses via ``workflow.wait_condition`` on a signal or
update handler, then call ``runner.run_async`` again with the response parts.

These helpers cover the wire format only; the wait topology stays ordinary
Temporal workflow code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, Optional

from google.adk.events import Event
from google.adk.tools.tool_confirmation import ToolConfirmation
from google.genai import types

# The function-call names ADK uses on the wire for HITL pauses.
_REQUEST_INPUT_FUNCTION_CALL_NAME = "adk_request_input"
_REQUEST_CONFIRMATION_FUNCTION_CALL_NAME = "adk_request_confirmation"
_REQUEST_CREDENTIAL_FUNCTION_CALL_NAME = "adk_request_credential"

_KIND_BY_FUNCTION_CALL_NAME: dict[
    str, Literal["input", "tool_confirmation", "credential"]
] = {
    _REQUEST_INPUT_FUNCTION_CALL_NAME: "input",
    _REQUEST_CONFIRMATION_FUNCTION_CALL_NAME: "tool_confirmation",
    _REQUEST_CREDENTIAL_FUNCTION_CALL_NAME: "credential",
}


@dataclass(frozen=True)
class HitlRequest:
    """A pending human-in-the-loop request extracted from an ADK event.

    .. warning::
        This class is experimental and may change in future versions.
        Use with caution in production environments.

    Attributes:
        kind: ``"input"`` for a human-input node's ``RequestInput``,
            ``"tool_confirmation"`` for a tool confirmation request,
            ``"credential"`` for an auth request.
        interrupt_id: The id a response must reference. Pass it to
            :func:`hitl_input_response` or :func:`hitl_confirmation_response`.
        invocation_id: The ADK invocation that is paused on this request.
        author: The agent/node that raised the request.
        message: Human-readable prompt (``RequestInput.message`` or the
            confirmation hint), if any.
        payload: Custom payload attached to the request, if any.
        response_schema: JSON schema the response must satisfy (input
            requests only), if any.
        original_function_call: For tool confirmations, the gated tool call
            as ``{"name": ..., "args": ..., "id": ...}`` — useful for
            displaying what is being approved.
    """

    kind: Literal["input", "tool_confirmation", "credential"]
    interrupt_id: str
    invocation_id: Optional[str] = None
    author: Optional[str] = None
    message: Optional[str] = None
    payload: Optional[Any] = None
    response_schema: Optional[dict[str, Any]] = None
    original_function_call: Optional[dict[str, Any]] = None


def pending_hitl_requests(event: Event) -> list[HitlRequest]:
    """Extracts pending human-in-the-loop requests from an ADK event.

    .. warning::
        This function is experimental and may change in future versions.
        Use with caution in production environments.

    Call this on each event yielded by ``runner.run_async``. A non-empty
    result means the run is pausing for the returned requests; once
    ``run_async`` completes, resume by sending a new user message whose parts
    answer them (see :func:`hitl_input_response` and
    :func:`hitl_confirmation_response`).

    Args:
        event: An event yielded by ``runner.run_async``.

    Returns:
        The requests carried by this event; empty for ordinary events.
    """
    if not event.long_running_tool_ids:
        return []
    if not event.content or not event.content.parts:
        return []
    requests: list[HitlRequest] = []
    for part in event.content.parts:
        function_call = part.function_call
        if not function_call or not function_call.id:
            continue
        kind = _KIND_BY_FUNCTION_CALL_NAME.get(function_call.name or "")
        if kind is None:
            continue
        args = function_call.args or {}
        message: Optional[str] = None
        payload: Optional[Any] = None
        response_schema: Optional[dict[str, Any]] = None
        original_function_call: Optional[dict[str, Any]] = None
        if kind == "input":
            message = args.get("message")
            payload = args.get("payload")
            response_schema = args.get("response_schema")
        elif kind == "tool_confirmation":
            confirmation = args.get("toolConfirmation") or {}
            message = confirmation.get("hint")
            payload = confirmation.get("payload")
            original_function_call = args.get("originalFunctionCall")
        else:
            payload = args
        requests.append(
            HitlRequest(
                kind=kind,
                interrupt_id=function_call.id,
                invocation_id=event.invocation_id,
                author=event.author,
                message=message,
                payload=payload,
                response_schema=response_schema,
                original_function_call=original_function_call,
            )
        )
    return requests


def hitl_input_response(interrupt_id: str, response: Any) -> types.Part:
    """Builds the message part answering a human-input (``RequestInput``) request.

    .. warning::
        This function is experimental and may change in future versions.
        Use with caution in production environments.

    Compose one or more parts into ``types.Content(role="user", parts=[...])``
    and pass it as ``new_message`` to ``runner.run_async`` to resume the
    paused run. Non-mapping values are wrapped as ``{"result": value}`` per
    ADK's convention and unwrapped on delivery to the node.

    Args:
        interrupt_id: The :attr:`HitlRequest.interrupt_id` being answered.
        response: The human's response value.
    """
    if isinstance(response, Mapping):
        response_dict = dict(response)
    else:
        response_dict = {"result": response}
    return types.Part(
        function_response=types.FunctionResponse(
            id=interrupt_id,
            name=_REQUEST_INPUT_FUNCTION_CALL_NAME,
            response=response_dict,
        )
    )


def hitl_confirmation_response(
    interrupt_id: str, *, confirmed: bool, payload: Optional[Any] = None
) -> types.Part:
    """Builds the message part answering a tool-confirmation request.

    .. warning::
        This function is experimental and may change in future versions.
        Use with caution in production environments.

    Compose one or more parts into ``types.Content(role="user", parts=[...])``
    and pass it as ``new_message`` to ``runner.run_async`` to resume the
    paused run. If ``confirmed`` is false the gated tool is not executed and
    the model receives a rejection response instead.

    Args:
        interrupt_id: The :attr:`HitlRequest.interrupt_id` being answered.
        confirmed: Whether the human approved running the tool.
        payload: Optional custom payload made available to the tool via
            ``tool_context.tool_confirmation.payload``.
    """
    confirmation = ToolConfirmation(confirmed=confirmed, payload=payload)
    return types.Part(
        function_response=types.FunctionResponse(
            id=interrupt_id,
            name=_REQUEST_CONFIRMATION_FUNCTION_CALL_NAME,
            response=confirmation.model_dump(mode="json"),
        )
    )
