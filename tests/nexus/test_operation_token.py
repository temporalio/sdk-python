import base64
import json
from typing import Any

import pytest

from temporalio.nexus._token import (
    OperationToken,
    OperationTokenType,
    WorkflowHandle,
)


def _encode_json_token(value: Any) -> str:
    return _encode_bytes(json.dumps(value, separators=(",", ":")).encode("utf-8"))


def _encode_bytes(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("utf-8").rstrip("=")


def _decode_bytes(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def test_operation_token_encode_decode_round_trip():
    token = OperationToken(
        type=OperationTokenType.WORKFLOW,
        namespace="default",
        workflow_id="workflow-id",
        version=0,
    ).encode()

    assert "=" not in token
    assert OperationToken.decode(token) == OperationToken(
        type=OperationTokenType.WORKFLOW,
        namespace="default",
        workflow_id="workflow-id",
        version=0,
    )


def test_operation_token_activity_encode_decode_round_trip():
    token = OperationToken(
        type=OperationTokenType.ACTIVITY,
        namespace="default",
        activity_id="activity-id",
        version=0,
    ).encode()

    assert "=" not in token
    assert OperationToken.decode(token) == OperationToken(
        type=OperationTokenType.ACTIVITY,
        namespace="default",
        activity_id="activity-id",
        version=0,
    )


def test_operation_token_activity_encode_uses_activity_id_and_omits_workflow_id():
    token = OperationToken(
        type=OperationTokenType.ACTIVITY,
        namespace="default",
        activity_id="activity-id",
    ).encode()

    assert json.loads(_decode_bytes(token)) == {
        "t": 2,
        "ns": "default",
        "aid": "activity-id",
    }


def test_workflow_handle_to_from_token_round_trip():
    handle = WorkflowHandle[str](namespace="default", workflow_id="workflow-id")

    assert WorkflowHandle[str].from_token(handle.to_token()) == handle


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        (
            _encode_json_token({"t": 1, "ns": "default", "wid": "workflow-id"}),
            OperationToken(
                type=OperationTokenType.WORKFLOW,
                namespace="default",
                workflow_id="workflow-id",
            ),
        ),
        (
            _encode_json_token({"t": 1, "ns": "", "wid": "workflow-id"}),
            OperationToken(
                type=OperationTokenType.WORKFLOW,
                namespace="",
                workflow_id="workflow-id",
            ),
        ),
        (
            _encode_json_token(
                {"t": 1, "ns": "default", "wid": "workflow-id", "v": None}
            ),
            OperationToken(
                type=OperationTokenType.WORKFLOW,
                namespace="default",
                workflow_id="workflow-id",
            ),
        ),
        (
            _encode_json_token({"t": 1, "ns": "default", "wid": "workflow-id", "v": 0}),
            OperationToken(
                type=OperationTokenType.WORKFLOW,
                namespace="default",
                workflow_id="workflow-id",
                version=0,
            ),
        ),
        # Activity tokens
        (
            _encode_json_token({"t": 2, "ns": "default", "aid": "activity-id"}),
            OperationToken(
                type=OperationTokenType.ACTIVITY,
                namespace="default",
                activity_id="activity-id",
            ),
        ),
        (
            _encode_json_token({"t": 2, "ns": "", "aid": "activity-id"}),
            OperationToken(
                type=OperationTokenType.ACTIVITY,
                namespace="",
                activity_id="activity-id",
            ),
        ),
        (
            _encode_json_token(
                {"t": 2, "ns": "default", "aid": "activity-id", "v": None}
            ),
            OperationToken(
                type=OperationTokenType.ACTIVITY,
                namespace="default",
                activity_id="activity-id",
            ),
        ),
        (
            _encode_json_token({"t": 2, "ns": "default", "aid": "activity-id", "v": 0}),
            OperationToken(
                type=OperationTokenType.ACTIVITY,
                namespace="default",
                activity_id="activity-id",
                version=0,
            ),
        ),
    ],
)
def test_operation_token_decode_accepts_valid_tokens(
    token: str,
    expected: OperationToken,
):
    assert OperationToken.decode(token) == expected


@pytest.mark.parametrize(
    ("token", "message"),
    [
        ("", "invalid token: token is empty"),
        ("not+a-base64url-token", "failed to decode token as base64url"),
        (_encode_bytes(b"not json"), "failed to unmarshal operation token"),
        (_encode_json_token(["not", "a", "dict"]), "expected dict"),
        (
            _encode_json_token({"ns": "default", "wid": "workflow-id"}),
            "expected token type to be an int",
        ),
        (
            _encode_json_token({"t": "1", "ns": "default", "wid": "workflow-id"}),
            "expected token type to be an int",
        ),
        (
            _encode_json_token({"t": 999, "ns": "default", "wid": "workflow-id"}),
            "unknown token type",
        ),
        (
            _encode_json_token({"t": 1, "ns": "default"}),
            "expected non-empty workflow id for token type `WORKFLOW`",
        ),
        (
            _encode_json_token({"t": 1, "ns": "default", "wid": 123}),
            "expected workflow id to be a string",
        ),
        (
            _encode_json_token({"t": 1, "ns": "default", "wid": ""}),
            "expected non-empty workflow id for token type `WORKFLOW`",
        ),
        (
            _encode_json_token({"t": 1, "wid": "workflow-id"}),
            "expected namespace to be a string",
        ),
        (
            _encode_json_token({"t": 1, "ns": 123, "wid": "workflow-id"}),
            "expected namespace to be a string",
        ),
        (
            _encode_json_token(
                {"t": 1, "ns": "default", "wid": "workflow-id", "v": "0"}
            ),
            "expected version to be an int or null",
        ),
        # Activity tokens
        (
            _encode_json_token({"t": 2, "ns": "default"}),
            "expected non-empty activity id for token type `ACTIVITY`",
        ),
        (
            _encode_json_token({"t": 2, "ns": "default", "aid": ""}),
            "expected non-empty activity id for token type `ACTIVITY`",
        ),
        (
            _encode_json_token({"t": 2, "ns": "default", "aid": 123}),
            "expected activity id to be a string",
        ),
        (
            _encode_json_token({"t": 2, "aid": "activity-id"}),
            "expected namespace to be a string",
        ),
        (
            _encode_json_token(
                {"t": 2, "ns": "default", "aid": "activity-id", "v": "0"}
            ),
            "expected version to be an int or null",
        ),
    ],
)
def test_operation_token_decode_rejects_invalid_tokens(token: str, message: str):
    with pytest.raises(TypeError, match=message):
        OperationToken.decode(token)


def test_workflow_handle_from_token_accepts_version_zero():
    token = _encode_json_token({"t": 1, "ns": "default", "wid": "workflow-id", "v": 0})

    assert WorkflowHandle[str].from_token(token) == WorkflowHandle[str](
        namespace="default",
        workflow_id="workflow-id",
        version=0,
    )


def test_workflow_handle_from_token_rejects_unsupported_version():
    token = _encode_json_token({"t": 1, "ns": "default", "wid": "workflow-id", "v": 1})

    with pytest.raises(TypeError, match="'v' field, if present, must be 0"):
        WorkflowHandle[str].from_token(token)
