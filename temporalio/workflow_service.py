"""Underlying gRPC workflow service."""

from __future__ import annotations

import hashlib
import logging
import os
import socket
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Generic, Mapping, Optional, Type, TypeVar

import google.protobuf.message
import grpc

import temporalio.api.workflowservice.v1
import temporalio.bridge.client
import temporalio.bridge.telemetry
import temporalio.exceptions

WorkflowServiceRequest = TypeVar(
    "WorkflowServiceRequest", bound=google.protobuf.message.Message
)
WorkflowServiceResponse = TypeVar(
    "WorkflowServiceResponse", bound=google.protobuf.message.Message
)

logger = logging.getLogger(__name__)

# Set to true to log all requests and responses
LOG_PROTOS = False


@dataclass
class TLSConfig:
    """TLS configuration for connecting to Temporal server."""

    server_root_ca_cert: Optional[bytes] = None
    """Root CA to validate the server certificate against."""

    domain: Optional[str] = None
    """TLS domain."""

    client_cert: Optional[bytes] = None
    """Client certificate for mTLS.
    
    This must be combined with :py:attr:`client_private_key`."""

    client_private_key: Optional[bytes] = None
    """Client private key for mTLS.
    
    This must be combined with :py:attr:`client_cert`."""

    def _to_bridge_config(self) -> temporalio.bridge.client.ClientTlsConfig:
        return temporalio.bridge.client.ClientTlsConfig(
            server_root_ca_cert=self.server_root_ca_cert,
            domain=self.domain,
            client_cert=self.client_cert,
            client_private_key=self.client_private_key,
        )


@dataclass
class RetryConfig:
    """Retry configuration for server calls."""

    initial_interval_millis: int = 100
    """Initial backoff interval."""
    randomization_factor: float = 0.2
    """Randomization jitter to add."""
    multiplier: float = 1.5
    """Backoff multiplier."""
    max_interval_millis: int = 5000
    """Maximum backoff interval."""
    max_elapsed_time_millis: Optional[int] = 10000
    """Maximum total time."""
    max_retries: int = 10
    """Maximum number of retries."""

    def _to_bridge_config(self) -> temporalio.bridge.client.ClientRetryConfig:
        return temporalio.bridge.client.ClientRetryConfig(
            initial_interval_millis=self.initial_interval_millis,
            randomization_factor=self.randomization_factor,
            multiplier=self.multiplier,
            max_interval_millis=self.max_interval_millis,
            max_elapsed_time_millis=self.max_elapsed_time_millis,
            max_retries=self.max_retries,
        )


@dataclass
class ConnectConfig:
    """Config for connecting to the server."""

    target_url: str
    tls_config: Optional[TLSConfig] = None
    retry_config: Optional[RetryConfig] = None
    static_headers: Mapping[str, str] = field(default_factory=dict)
    identity: str = ""
    worker_binary_id: str = ""

    def __post_init__(self) -> None:
        """Set extra defaults on unset properties."""
        if not self.identity:
            self.identity = f"{os.getpid()}@{socket.gethostname()}"
        if not self.worker_binary_id:
            self.worker_binary_id = load_default_worker_binary_id()

    def _to_bridge_config(self) -> temporalio.bridge.client.ClientConfig:
        return temporalio.bridge.client.ClientConfig(
            target_url=self.target_url,
            tls_config=self.tls_config._to_bridge_config() if self.tls_config else None,
            retry_config=self.retry_config._to_bridge_config()
            if self.retry_config
            else None,
            static_headers=self.static_headers,
            identity=self.identity,
            worker_binary_id=self.worker_binary_id or "",
        )


class WorkflowService(ABC):
    """Client to the Temporal server's workflow service."""

    @staticmethod
    async def connect(config: ConnectConfig) -> WorkflowService:
        """Connect directly to the workflow service."""
        return await _BridgeWorkflowService.connect(config)

    def __init__(self, config: ConnectConfig) -> None:
        """Initialize the base workflow service."""
        super().__init__()
        self._config = config

        wsv1 = temporalio.api.workflowservice.v1

        self.count_workflow_executions = self._new_call(
            "count_workflow_executions",
            wsv1.CountWorkflowExecutionsRequest,
            wsv1.CountWorkflowExecutionsResponse,
        )
        self.deprecate_namespace = self._new_call(
            "deprecate_namespace",
            wsv1.DeprecateNamespaceRequest,
            wsv1.DeprecateNamespaceResponse,
        )
        self.describe_namespace = self._new_call(
            "describe_namespace",
            wsv1.DescribeNamespaceRequest,
            wsv1.DescribeNamespaceResponse,
        )
        self.describe_task_queue = self._new_call(
            "describe_task_queue",
            wsv1.DescribeTaskQueueRequest,
            wsv1.DescribeTaskQueueResponse,
        )
        self.describe_workflow_execution = self._new_call(
            "describe_workflow_execution",
            wsv1.DescribeWorkflowExecutionRequest,
            wsv1.DescribeWorkflowExecutionResponse,
        )
        self.get_cluster_info = self._new_call(
            "get_cluster_info",
            wsv1.GetClusterInfoRequest,
            wsv1.GetClusterInfoResponse,
        )
        self.get_search_attributes = self._new_call(
            "get_search_attributes",
            wsv1.GetSearchAttributesRequest,
            wsv1.GetSearchAttributesResponse,
        )
        self.get_system_info = self._new_call(
            "get_system_info",
            wsv1.GetSystemInfoRequest,
            wsv1.GetSystemInfoResponse,
        )
        self.get_workflow_execution_history = self._new_call(
            "get_workflow_execution_history",
            wsv1.GetWorkflowExecutionHistoryRequest,
            wsv1.GetWorkflowExecutionHistoryResponse,
        )
        # TODO(cretz): Fix when https://github.com/temporalio/sdk-core/issues/335 fixed
        # self.get_workflow_execution_history_reverse = self._new_call(
        #     "get_workflow_execution_history_reverse",
        #     wsv1.GetWorkflowExecutionHistoryReverseRequest,
        #     wsv1.GetWorkflowExecutionHistoryReverseResponse,
        # )
        self.list_archived_workflow_executions = self._new_call(
            "list_archived_workflow_executions",
            wsv1.ListArchivedWorkflowExecutionsRequest,
            wsv1.ListArchivedWorkflowExecutionsResponse,
        )
        self.list_closed_workflow_executions = self._new_call(
            "list_closed_workflow_executions",
            wsv1.ListClosedWorkflowExecutionsRequest,
            wsv1.ListClosedWorkflowExecutionsResponse,
        )
        self.list_namespaces = self._new_call(
            "list_namespaces",
            wsv1.ListNamespacesRequest,
            wsv1.ListNamespacesResponse,
        )
        self.list_open_workflow_executions = self._new_call(
            "list_open_workflow_executions",
            wsv1.ListOpenWorkflowExecutionsRequest,
            wsv1.ListOpenWorkflowExecutionsResponse,
        )
        self.list_task_queue_partitions = self._new_call(
            "list_task_queue_partitions",
            wsv1.ListTaskQueuePartitionsRequest,
            wsv1.ListTaskQueuePartitionsResponse,
        )
        self.list_workflow_executions = self._new_call(
            "list_workflow_executions",
            wsv1.ListWorkflowExecutionsRequest,
            wsv1.ListWorkflowExecutionsResponse,
        )
        self.poll_activity_task_queue = self._new_call(
            "poll_activity_task_queue",
            wsv1.PollActivityTaskQueueRequest,
            wsv1.PollActivityTaskQueueResponse,
        )
        self.poll_workflow_task_queue = self._new_call(
            "poll_workflow_task_queue",
            wsv1.PollWorkflowTaskQueueRequest,
            wsv1.PollWorkflowTaskQueueResponse,
        )
        self.query_workflow = self._new_call(
            "query_workflow",
            wsv1.QueryWorkflowRequest,
            wsv1.QueryWorkflowResponse,
        )
        self.record_activity_task_heartbeat = self._new_call(
            "record_activity_task_heartbeat",
            wsv1.RecordActivityTaskHeartbeatRequest,
            wsv1.RecordActivityTaskHeartbeatResponse,
        )
        self.record_activity_task_heartbeat_by_id = self._new_call(
            "record_activity_task_heartbeat_by_id",
            wsv1.RecordActivityTaskHeartbeatByIdRequest,
            wsv1.RecordActivityTaskHeartbeatByIdResponse,
        )
        self.register_namespace = self._new_call(
            "register_namespace",
            wsv1.RegisterNamespaceRequest,
            wsv1.RegisterNamespaceResponse,
        )
        self.request_cancel_workflow_execution = self._new_call(
            "request_cancel_workflow_execution",
            wsv1.RequestCancelWorkflowExecutionRequest,
            wsv1.RequestCancelWorkflowExecutionResponse,
        )
        self.reset_sticky_task_queue = self._new_call(
            "reset_sticky_task_queue",
            wsv1.ResetStickyTaskQueueRequest,
            wsv1.ResetStickyTaskQueueResponse,
        )
        self.reset_workflow_execution = self._new_call(
            "reset_workflow_execution",
            wsv1.ResetWorkflowExecutionRequest,
            wsv1.ResetWorkflowExecutionResponse,
        )
        self.respond_activity_task_canceled = self._new_call(
            "respond_activity_task_canceled",
            wsv1.RespondActivityTaskCanceledRequest,
            wsv1.RespondActivityTaskCanceledResponse,
        )
        self.respond_activity_task_canceled_by_id = self._new_call(
            "respond_activity_task_canceled_by_id",
            wsv1.RespondActivityTaskCanceledByIdRequest,
            wsv1.RespondActivityTaskCanceledByIdResponse,
        )
        self.respond_activity_task_completed = self._new_call(
            "respond_activity_task_completed",
            wsv1.RespondActivityTaskCompletedRequest,
            wsv1.RespondActivityTaskCompletedResponse,
        )
        self.respond_activity_task_completed_by_id = self._new_call(
            "respond_activity_task_completed_by_id",
            wsv1.RespondActivityTaskCompletedByIdRequest,
            wsv1.RespondActivityTaskCompletedByIdResponse,
        )
        self.respond_activity_task_failed = self._new_call(
            "respond_activity_task_failed",
            wsv1.RespondActivityTaskFailedRequest,
            wsv1.RespondActivityTaskFailedResponse,
        )
        self.respond_activity_task_failed_by_id = self._new_call(
            "respond_activity_task_failed_by_id",
            wsv1.RespondActivityTaskFailedByIdRequest,
            wsv1.RespondActivityTaskFailedByIdResponse,
        )
        self.respond_query_task_completed = self._new_call(
            "respond_query_task_completed",
            wsv1.RespondQueryTaskCompletedRequest,
            wsv1.RespondQueryTaskCompletedResponse,
        )
        self.respond_workflow_task_completed = self._new_call(
            "respond_workflow_task_completed",
            wsv1.RespondWorkflowTaskCompletedRequest,
            wsv1.RespondWorkflowTaskCompletedResponse,
        )
        self.respond_workflow_task_failed = self._new_call(
            "respond_workflow_task_failed",
            wsv1.RespondWorkflowTaskFailedRequest,
            wsv1.RespondWorkflowTaskFailedResponse,
        )
        self.scan_workflow_executions = self._new_call(
            "scan_workflow_executions",
            wsv1.ScanWorkflowExecutionsRequest,
            wsv1.ScanWorkflowExecutionsResponse,
        )
        self.signal_with_start_workflow_execution = self._new_call(
            "signal_with_start_workflow_execution",
            wsv1.SignalWithStartWorkflowExecutionRequest,
            wsv1.SignalWithStartWorkflowExecutionResponse,
        )
        self.signal_workflow_execution = self._new_call(
            "signal_workflow_execution",
            wsv1.SignalWorkflowExecutionRequest,
            wsv1.SignalWorkflowExecutionResponse,
        )
        self.start_workflow_execution = self._new_call(
            "start_workflow_execution",
            wsv1.StartWorkflowExecutionRequest,
            wsv1.StartWorkflowExecutionResponse,
        )
        self.terminate_workflow_execution = self._new_call(
            "terminate_workflow_execution",
            wsv1.TerminateWorkflowExecutionRequest,
            wsv1.TerminateWorkflowExecutionResponse,
        )
        self.update_namespace = self._new_call(
            "update_namespace",
            wsv1.UpdateNamespaceRequest,
            wsv1.UpdateNamespaceResponse,
        )

    @property
    def config(self) -> ConnectConfig:
        """Config originally used to connect."""
        return self._config

    @property
    @abstractmethod
    def worker_workflow_service(self) -> _BridgeWorkflowService:
        """Underlying workflow service."""
        raise NotImplementedError

    @abstractmethod
    async def _rpc_call(
        self,
        rpc: str,
        req: google.protobuf.message.Message,
        resp_type: Type[WorkflowServiceResponse],
        *,
        retry: bool = False,
    ) -> WorkflowServiceResponse:
        raise NotImplementedError

    def _new_call(
        self,
        name: str,
        req_type: Type[WorkflowServiceRequest],
        resp_type: Type[WorkflowServiceResponse],
    ) -> "WorkflowServiceCall[WorkflowServiceRequest, WorkflowServiceResponse]":
        return WorkflowServiceCall(self, name, req_type, resp_type)


class WorkflowServiceCall(Generic[WorkflowServiceRequest, WorkflowServiceResponse]):
    """Callable RPC method for :py:class:`WorkflowService`."""

    def __init__(
        self,
        service: WorkflowService,
        name: str,
        req_type: Type[WorkflowServiceRequest],
        resp_type: Type[WorkflowServiceResponse],
    ) -> None:
        """Initialize the workflow service call."""
        self.service = service
        self.name = name
        self.req_type = req_type
        self.resp_type = resp_type

    async def __call__(
        self, req: WorkflowServiceRequest, *, retry: bool = False
    ) -> WorkflowServiceResponse:
        """Invoke underlying client with the given request.

        Args:
            req: Request for the call.
            retry: If true, will use retry config to retry failed calls.

        Returns:
            RPC response.

        Raises:
            RPCError: Any RPC error that occurs during the call.
        """
        return await self.service._rpc_call(self.name, req, self.resp_type, retry=retry)


class _BridgeWorkflowService(WorkflowService):
    @staticmethod
    async def connect(config: ConnectConfig) -> _BridgeWorkflowService:
        # TODO(cretz): Expose telemetry init config
        temporalio.bridge.telemetry.init_telemetry(
            temporalio.bridge.telemetry.TelemetryConfig(),
            warn_if_already_inited=False,
        )

        return _BridgeWorkflowService(
            config,
            await temporalio.bridge.client.Client.connect(config._to_bridge_config()),
        )

    _bridge_client: temporalio.bridge.client.Client

    def __init__(
        self, config: ConnectConfig, bridge_client: temporalio.bridge.client.Client
    ) -> None:
        super().__init__(config)
        self._bridge_client = bridge_client

    @property
    def worker_workflow_service(self) -> _BridgeWorkflowService:
        """Underlying workflow service."""
        return self

    async def _rpc_call(
        self,
        rpc: str,
        req: google.protobuf.message.Message,
        resp_type: Type[WorkflowServiceResponse],
        *,
        retry: bool = False,
    ) -> WorkflowServiceResponse:
        global LOG_PROTOS
        if LOG_PROTOS:
            logger.debug("WorkflowService request to %s: %s", rpc, req)
        try:
            resp = await self._bridge_client.rpc_call(rpc, req, resp_type, retry=retry)
            if LOG_PROTOS:
                logger.debug("WorkflowService response from %s: %s", rpc, resp)
            return resp
        except temporalio.bridge.client.RPCError as err:
            # Intentionally swallowing the cause instead of using "from"
            status, message, details = err.args
            raise RPCError(message, RPCStatusCode(status), details)


class RPCStatusCode(IntEnum):
    """Status code for :py:class:`RPCError`."""

    OK = grpc.StatusCode.OK.value[0]
    CANCELLED = grpc.StatusCode.CANCELLED.value[0]
    UNKNOWN = grpc.StatusCode.UNKNOWN.value[0]
    INVALID_ARGUMENT = grpc.StatusCode.INVALID_ARGUMENT.value[0]
    DEADLINE_EXCEEDED = grpc.StatusCode.DEADLINE_EXCEEDED.value[0]
    NOT_FOUND = grpc.StatusCode.NOT_FOUND.value[0]
    ALREADY_EXISTS = grpc.StatusCode.ALREADY_EXISTS.value[0]
    PERMISSION_DENIED = grpc.StatusCode.PERMISSION_DENIED.value[0]
    RESOURCE_EXHAUSTED = grpc.StatusCode.RESOURCE_EXHAUSTED.value[0]
    FAILED_PRECONDITION = grpc.StatusCode.FAILED_PRECONDITION.value[0]
    ABORTED = grpc.StatusCode.ABORTED.value[0]
    OUT_OF_RANGE = grpc.StatusCode.OUT_OF_RANGE.value[0]
    UNIMPLEMENTED = grpc.StatusCode.UNIMPLEMENTED.value[0]
    INTERNAL = grpc.StatusCode.INTERNAL.value[0]
    UNAVAILABLE = grpc.StatusCode.UNAVAILABLE.value[0]
    DATA_LOSS = grpc.StatusCode.DATA_LOSS.value[0]
    UNAUTHENTICATED = grpc.StatusCode.UNAUTHENTICATED.value[0]


class RPCError(temporalio.exceptions.TemporalError):
    """Error during RPC call."""

    def __init__(self, message: str, status: RPCStatusCode, details: bytes) -> None:
        """Initialize RPC error."""
        super().__init__(message)
        self._status = status
        self._details = details

    @property
    def status(self) -> RPCStatusCode:
        """Status code for the error."""
        return self._status

    @property
    def details(self) -> bytes:
        """Any details on the error."""
        return self._details


_default_worker_binary_id: Optional[str] = None


def load_default_worker_binary_id(*, memoize: bool = True) -> str:
    """Load the default worker binary ID.

    The worker binary ID is a unique hash representing the entire set of code
    including Temporal code and external code. The default here is currently
    implemented by walking loaded modules and hashing their bytecode into a
    common hash.

    Args:
        memoize: If true, the default, this will cache to a global variable to
            keep from having to run again on successive calls.

    Returns:
        Unique identifier representing the set of running code.
    """
    # Memoize
    global _default_worker_binary_id
    if memoize and _default_worker_binary_id:
        return _default_worker_binary_id

    # The goal is to get a hash representing the set of runtime code, both
    # Temporal's and the user's. After all options were explored, we have
    # decided to default to hashing all bytecode of imported modules. We accept
    # that this has the following limitations:
    #
    # * Dynamic imports later on can affect this value
    # * Dynamic imports based on env var, platform, etc can affect this value
    # * Using the loader's get_code seems to use get_data which does a disk read
    # * Using the loader's get_code in rare cases can cause a compile()

    got_temporal_code = False
    m = hashlib.md5()
    for mod_name in sorted(sys.modules):
        # Try to read code
        code = _get_module_code(mod_name)
        if not code:
            continue
        if mod_name == "temporalio":
            got_temporal_code = True
        # Add to MD5 digest
        m.update(code)
    # If we didn't even get the temporalio module from this approach, this
    # approach is flawed and we prefer to return an error forcing user to
    # explicitly set the worker binary ID instead of silently using a value that
    # may never change
    if not got_temporal_code:
        raise RuntimeError(
            "Cannot get default unique worker binary ID, the value should be explicitly set"
        )
    # Return the hex digest
    digest = m.hexdigest()
    if memoize:
        _default_worker_binary_id = digest
    return digest


def _get_module_code(mod_name: str) -> Optional[bytes]:
    # First try the module's loader and if that fails, try __cached__ file
    try:
        loader: Any = sys.modules[mod_name].__loader__
        code = loader.get_code(mod_name).co_code
        if code:
            return code
    except Exception:
        pass
    try:
        # Technically we could read smaller chunks per file here and update the
        # hash, but the benefit is negligible especially since many non-built-in
        # modules will use get_code above
        with open(sys.modules[mod_name].__cached__, "rb") as f:
            return f.read()
    except Exception:
        pass
    return None
