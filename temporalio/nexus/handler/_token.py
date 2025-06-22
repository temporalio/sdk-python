from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Generic, Literal, Optional

from nexusrpc.types import OutputT

if TYPE_CHECKING:
    from temporalio.client import Client, WorkflowHandle

OPERATION_TOKEN_TYPE_WORKFLOW = 1
OperationTokenType = Literal[1]


@dataclass(frozen=True)
class WorkflowOperationToken(Generic[OutputT]):
    """Represents the structured data of a Nexus workflow operation token."""

    namespace: str
    workflow_id: str
    _type: OperationTokenType = OPERATION_TOKEN_TYPE_WORKFLOW
    # Version of the token. Treated as v1 if missing. This field is not included in the
    # serialized token; it's only used to reject newer token versions on load.
    version: Optional[int] = None

    # TODO(nexus-preview): Is it helpful to parameterize WorkflowOperationToken by
    # OutputT? The return type here should be dictated by the input workflow handle
    # type.
    @classmethod
    def from_workflow_handle(
        cls, workflow_handle: WorkflowHandle[Any, OutputT]
    ) -> WorkflowOperationToken[OutputT]:
        """Creates a token from a workflow handle."""
        return cls(
            namespace=workflow_handle._client.namespace,
            workflow_id=workflow_handle.id,
        )

    def to_workflow_handle(self, client: Client) -> WorkflowHandle[Any, OutputT]:
        """Creates a workflow handle from this token."""
        if client.namespace != self.namespace:
            raise ValueError(
                f"Client namespace {client.namespace} does not match token namespace {self.namespace}"
            )
        return client.get_workflow_handle(self.workflow_id)

    def encode(self) -> str:
        return _base64url_encode_no_padding(
            json.dumps(
                {
                    "t": self._type,
                    "ns": self.namespace,
                    "wid": self.workflow_id,
                },
                separators=(",", ":"),
            ).encode("utf-8")
        )

    @classmethod
    def decode(cls, token: str) -> WorkflowOperationToken[OutputT]:
        """Decodes and validates a token from its base64url-encoded string representation."""
        if not token:
            raise TypeError("invalid workflow token: token is empty")
        try:
            decoded_bytes = _base64url_decode_no_padding(token)
        except Exception as err:
            raise TypeError("failed to decode token as base64url") from err
        try:
            workflow_operation_token = json.loads(decoded_bytes.decode("utf-8"))
        except Exception as err:
            raise TypeError("failed to unmarshal workflow operation token") from err

        if not isinstance(workflow_operation_token, dict):
            raise TypeError(
                f"invalid workflow token: expected dict, got {type(workflow_operation_token)}"
            )

        _type = workflow_operation_token.get("t")
        if _type != OPERATION_TOKEN_TYPE_WORKFLOW:
            raise TypeError(
                f"invalid workflow token type: {_type}, expected: {OPERATION_TOKEN_TYPE_WORKFLOW}"
            )

        version = workflow_operation_token.get("v")
        if version is not None and version != 0:
            raise TypeError(
                "invalid workflow token: 'v' field, if present, must be 0 or null/absent"
            )

        workflow_id = workflow_operation_token.get("wid")
        if not workflow_id or not isinstance(workflow_id, str):
            raise TypeError(
                "invalid workflow token: missing, empty, or non-string workflow ID (wid)"
            )

        namespace = workflow_operation_token.get("ns")
        if namespace is None or not isinstance(namespace, str):
            # Allow empty string for ns, but it must be present and a string
            raise TypeError(
                "invalid workflow token: missing or non-string namespace (ns)"
            )

        return cls(
            _type=_type,
            namespace=namespace,
            workflow_id=workflow_id,
            version=version,
        )


def _base64url_encode_no_padding(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def _base64url_decode_no_padding(s: str) -> bytes:
    if not all(
        c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
        for c in s
    ):
        raise ValueError(
            "invalid base64URL encoded string: contains invalid characters"
        )
    padding = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + padding)
