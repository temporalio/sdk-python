"""Tests for LangSmith interceptor points and helper functions."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from temporalio.api.common.v1 import Payload
from temporalio.contrib.langsmith import LangSmithInterceptor
from temporalio.contrib.langsmith._interceptor import (
    HEADER_KEY,
    _extract_context,
    _inject_context,
    _maybe_run,
    _ReplaySafeRunTree,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Common patch targets (interceptor module)
_MOD = "temporalio.contrib.langsmith._interceptor"
_PATCH_RUNTREE = f"{_MOD}.RunTree"
_PATCH_IN_WORKFLOW = f"{_MOD}.temporalio.workflow.in_workflow"
_PATCH_IS_REPLAYING = f"{_MOD}.temporalio.workflow.unsafe.is_replaying_history_events"
_PATCH_WF_NOW = f"{_MOD}.temporalio.workflow.now"
_PATCH_WF_INFO = f"{_MOD}.temporalio.workflow.info"
_PATCH_TRACING_CTX = f"{_MOD}.tracing_context"
_PATCH_EXTRACT_NEXUS = f"{_MOD}._extract_nexus_context"
_PATCH_INJECT_NEXUS = f"{_MOD}._inject_nexus_context"
_PATCH_GET_CURRENT_RUN = f"{_MOD}.get_current_run_tree"


def _make_mock_run() -> MagicMock:
    """Create a mock RunTree with working to_headers() for _inject_context."""
    mock_run = MagicMock()
    mock_run.to_headers.return_value = {"langsmith-trace": "test-trace-id"}
    return mock_run


def _mock_workflow_info(**overrides: Any) -> MagicMock:
    """Create a mock workflow Info object."""
    info = MagicMock()
    info.workflow_id = overrides.get("workflow_id", "test-wf-id")
    info.run_id = overrides.get("run_id", "test-run-id")
    info.workflow_type = overrides.get("workflow_type", "TestWorkflow")
    return info


def _mock_activity_info(**overrides: Any) -> MagicMock:
    """Create a mock activity Info object."""
    info = MagicMock()
    info.workflow_id = overrides.get("workflow_id", "test-wf-id")
    info.workflow_run_id = overrides.get("workflow_run_id", "test-run-id")
    info.activity_id = overrides.get("activity_id", "test-activity-id")
    info.activity_type = overrides.get("activity_type", "test_activity")
    return info


def _make_executor() -> ThreadPoolExecutor:
    """Create a single-worker executor for tests."""
    return ThreadPoolExecutor(max_workers=1)


def _get_runtree_name(MockRunTree: MagicMock) -> str:
    """Extract the 'name' kwarg from RunTree constructor call."""
    MockRunTree.assert_called_once()
    return MockRunTree.call_args.kwargs["name"]


def _get_runtree_metadata(MockRunTree: MagicMock) -> dict[str, Any]:
    """Extract metadata from RunTree constructor kwargs.

    The design stores metadata in the 'extra' kwarg as {"metadata": {...}}.
    """
    MockRunTree.assert_called_once()
    kwargs = MockRunTree.call_args.kwargs
    extra = kwargs.get("extra", {})
    if extra and "metadata" in extra:
        return extra["metadata"]
    # Alternatively, metadata might be passed directly
    return kwargs.get("metadata", {})


# ===================================================================
# TestContextPropagation
# ===================================================================


class TestContextPropagation:
    """Tests for _inject_context / _extract_context roundtrip."""

    @patch(_PATCH_RUNTREE)
    def test_inject_extract_roundtrip(self, MockRunTree: Any) -> None:
        """Inject a mock run tree's headers, then extract. Verify roundtrip."""
        mock_run = MagicMock()
        mock_run.to_headers.return_value = {
            "langsmith-trace": "test-trace-id",
            "parent": "abc-123",
        }

        headers: dict[str, Payload] = {}
        result = _inject_context(headers, mock_run)

        assert HEADER_KEY in result

        # Mock from_headers for extraction (real one needs valid LangSmith header format)
        mock_extracted = MagicMock()
        MockRunTree.from_headers.return_value = mock_extracted

        extracted = _extract_context(result, _make_executor(), MagicMock())
        # extracted should be a _ReplaySafeRunTree wrapping the reconstructed run
        assert isinstance(extracted, _ReplaySafeRunTree)
        assert extracted._run is mock_extracted
        MockRunTree.from_headers.assert_called_once()

    def test_extract_missing_header(self) -> None:
        """When the _temporal-langsmith-context header is absent, returns None."""
        headers: dict[str, Payload] = {}
        result = _extract_context(headers, _make_executor(), MagicMock())
        assert result is None

    def test_inject_preserves_existing_headers(self) -> None:
        """Injecting LangSmith context does not overwrite other existing headers."""
        mock_run = MagicMock()
        mock_run.to_headers.return_value = {"langsmith-trace": "val"}

        existing_payload = Payload(data=b"existing")
        headers: dict[str, Payload] = {"my-header": existing_payload}
        result = _inject_context(headers, mock_run)

        assert "my-header" in result
        assert result["my-header"] is existing_payload
        assert HEADER_KEY in result


# ===================================================================
# TestReplaySafety
# ===================================================================


class TestReplaySafety:
    """Tests for replay-safe tracing behavior."""

    @patch(_PATCH_RUNTREE)
    @patch(_PATCH_IS_REPLAYING, return_value=True)
    @patch(_PATCH_IN_WORKFLOW, return_value=True)
    def test_replay_noop_post_end_patch(
        self, _mock_in_wf: Any, _mock_replaying: Any, MockRunTree: Any
    ) -> None:
        """During replay, RunTree is created but post/end/patch are no-ops."""
        mock_run = _make_mock_run()
        MockRunTree.return_value = mock_run
        mock_client = MagicMock()
        with _maybe_run(
            mock_client,
            "TestRun",
            add_temporal_runs=True,
            executor=_make_executor(),
        ):
            pass
        # RunTree IS created (wrapped in _ReplaySafeRunTree)
        MockRunTree.assert_called_once()
        # But post/end/patch are no-ops during replay
        mock_run.post.assert_not_called()
        mock_run.end.assert_not_called()
        mock_run.patch.assert_not_called()

    @patch(_PATCH_WF_NOW, return_value=datetime.now(timezone.utc))
    @patch(_PATCH_RUNTREE)
    @patch(_PATCH_IS_REPLAYING, return_value=False)
    @patch(_PATCH_IN_WORKFLOW, return_value=True)
    def test_create_trace_when_not_replaying(
        self, _mock_in_wf: Any, _mock_replaying: Any, MockRunTree: Any, _mock_now: Any
    ) -> None:
        """When not replaying (but in workflow), _maybe_run creates a _ReplaySafeRunTree."""
        mock_run = _make_mock_run()
        MockRunTree.return_value = mock_run
        mock_client = MagicMock()
        with _maybe_run(
            mock_client,
            "TestRun",
            add_temporal_runs=True,
            executor=_make_executor(),
        ):
            pass
        MockRunTree.assert_called_once()
        assert MockRunTree.call_args.kwargs["name"] == "TestRun"

    @patch(_PATCH_RUNTREE)
    @patch(_PATCH_IN_WORKFLOW, return_value=False)
    def test_create_trace_outside_workflow(
        self, _mock_in_wf: Any, MockRunTree: Any
    ) -> None:
        """Outside workflow (client/activity), RunTree IS created."""
        mock_run = _make_mock_run()
        MockRunTree.return_value = mock_run
        mock_client = MagicMock()
        with _maybe_run(
            mock_client,
            "TestRun",
            add_temporal_runs=True,
            executor=_make_executor(),
        ):
            pass
        MockRunTree.assert_called_once()


# ===================================================================
# TestErrorHandling
# ===================================================================


class TestErrorHandling:
    """Tests for _maybe_run error handling."""

    @patch(_PATCH_RUNTREE)
    @patch(_PATCH_IN_WORKFLOW, return_value=False)
    def test_exception_marks_run_errored(
        self, _mock_in_wf: Any, MockRunTree: Any
    ) -> None:
        """RuntimeError marks the run as errored and re-raises."""
        mock_run = _make_mock_run()
        MockRunTree.return_value = mock_run
        mock_client = MagicMock()
        with pytest.raises(RuntimeError, match="boom"):
            with _maybe_run(
                mock_client,
                "TestRun",
                add_temporal_runs=True,
                executor=_make_executor(),
            ):
                raise RuntimeError("boom")
        # run.end should have been called with error containing "boom"
        mock_run.end.assert_called()
        end_kwargs = mock_run.end.call_args.kwargs
        assert end_kwargs["error"] == "RuntimeError: boom"
        mock_run.patch.assert_called()

    @patch(_PATCH_RUNTREE)
    @patch(_PATCH_IN_WORKFLOW, return_value=False)
    def test_benign_application_error_not_marked(
        self, _mock_in_wf: Any, MockRunTree: Any
    ) -> None:
        """Benign ApplicationError does not mark the run as errored."""
        from temporalio.exceptions import ApplicationError, ApplicationErrorCategory

        mock_run = _make_mock_run()
        MockRunTree.return_value = mock_run
        mock_client = MagicMock()
        with pytest.raises(ApplicationError):
            with _maybe_run(
                mock_client,
                "TestRun",
                add_temporal_runs=True,
                executor=_make_executor(),
            ):
                raise ApplicationError(
                    "benign",
                    category=ApplicationErrorCategory.BENIGN,
                )
        # run.end should NOT have been called with error=
        end_calls = mock_run.end.call_args_list
        for c in end_calls:
            assert "error" not in (c.kwargs or {})

    @patch(_PATCH_RUNTREE)
    @patch(_PATCH_IN_WORKFLOW, return_value=False)
    def test_non_benign_application_error_marked(
        self, _mock_in_wf: Any, MockRunTree: Any
    ) -> None:
        """Non-benign ApplicationError marks the run as errored."""
        from temporalio.exceptions import ApplicationError

        mock_run = _make_mock_run()
        MockRunTree.return_value = mock_run
        mock_client = MagicMock()
        with pytest.raises(ApplicationError):
            with _maybe_run(
                mock_client,
                "TestRun",
                add_temporal_runs=True,
                executor=_make_executor(),
            ):
                raise ApplicationError("bad", non_retryable=True)
        mock_run.end.assert_called()
        end_kwargs = mock_run.end.call_args.kwargs
        assert end_kwargs["error"] == "ApplicationError: bad"
        mock_run.patch.assert_called()

    @patch(_PATCH_RUNTREE)
    @patch(_PATCH_IN_WORKFLOW, return_value=False)
    def test_success_completes_normally(
        self, _mock_in_wf: Any, MockRunTree: Any
    ) -> None:
        """On success, run.end(outputs={"status": "ok"}) and run.patch() are called."""
        mock_run = _make_mock_run()
        MockRunTree.return_value = mock_run
        mock_client = MagicMock()
        with _maybe_run(
            mock_client,
            "TestRun",
            add_temporal_runs=True,
            executor=_make_executor(),
        ):
            pass
        mock_run.end.assert_called_once()
        end_kwargs = mock_run.end.call_args.kwargs
        assert end_kwargs.get("outputs") == {"status": "ok"}
        mock_run.patch.assert_called()

    @patch(_PATCH_RUNTREE)
    @patch(_PATCH_IN_WORKFLOW, return_value=False)
    def test_cancelled_error_propagates_without_marking_run(
        self, _mock_in_wf: Any, MockRunTree: Any
    ) -> None:
        """CancelledError (BaseException) propagates without marking run as errored.

        _maybe_run catches Exception only, so CancelledError bypasses error marking.
        """
        mock_run = _make_mock_run()
        MockRunTree.return_value = mock_run
        mock_client = MagicMock()
        with pytest.raises(asyncio.CancelledError):
            with _maybe_run(
                mock_client,
                "TestRun",
                add_temporal_runs=True,
                executor=_make_executor(),
            ):
                raise asyncio.CancelledError()
        # run.end should NOT have been called with error=
        end_calls = mock_run.end.call_args_list
        for c in end_calls:
            assert "error" not in (c.kwargs or {})


# ===================================================================
# TestClientOutboundInterceptor
# ===================================================================


class TestClientOutboundInterceptor:
    """Tests for _LangSmithClientOutboundInterceptor."""

    def _make_client_interceptor(
        self, *, add_temporal_runs: bool = True
    ) -> tuple[Any, MagicMock]:
        """Create a client outbound interceptor with a mock next."""
        config = LangSmithInterceptor(
            client=MagicMock(), add_temporal_runs=add_temporal_runs
        )
        mock_next = MagicMock()
        mock_next.start_workflow = AsyncMock()
        mock_next.query_workflow = AsyncMock()
        mock_next.signal_workflow = AsyncMock()
        mock_next.start_workflow_update = AsyncMock()
        mock_next.start_update_with_start_workflow = AsyncMock()
        interceptor = config.intercept_client(mock_next)
        return interceptor, mock_next

    @pytest.mark.parametrize(
        "method,input_attrs,expected_name",
        [
            (
                "start_workflow",
                {"workflow": "MyWorkflow", "start_signal": None},
                "StartWorkflow:MyWorkflow",
            ),
            (
                "start_workflow",
                {"workflow": "MyWorkflow", "start_signal": "my_signal"},
                "SignalWithStartWorkflow:MyWorkflow",
            ),
            ("query_workflow", {"query": "get_status"}, "QueryWorkflow:get_status"),
            ("signal_workflow", {"signal": "my_signal"}, "SignalWorkflow:my_signal"),
            (
                "start_workflow_update",
                {"update": "my_update"},
                "StartWorkflowUpdate:my_update",
            ),
        ],
        ids=["start_workflow", "signal_with_start", "query", "signal", "update"],
    )
    @pytest.mark.asyncio
    @patch(_PATCH_RUNTREE)
    async def test_creates_trace_and_injects_headers(
        self,
        MockRunTree: Any,
        method: str,
        input_attrs: dict[str, Any],
        expected_name: str,
    ) -> None:
        """Each client method creates the correct trace and injects headers."""
        mock_run = _make_mock_run()
        MockRunTree.return_value = mock_run
        interceptor, mock_next = self._make_client_interceptor()
        mock_input = MagicMock()
        for k, v in input_attrs.items():
            setattr(mock_input, k, v)
        mock_input.headers = {}

        with patch(_PATCH_GET_CURRENT_RUN, return_value=mock_run):
            await getattr(interceptor, method)(mock_input)

        assert _get_runtree_name(MockRunTree) == expected_name
        assert HEADER_KEY in mock_input.headers
        getattr(mock_next, method).assert_called_once()

    @pytest.mark.asyncio
    @patch(_PATCH_RUNTREE)
    async def test_start_update_with_start_workflow(self, MockRunTree: Any) -> None:
        """start_update_with_start_workflow injects headers into BOTH start and update inputs."""
        mock_run = _make_mock_run()
        MockRunTree.return_value = mock_run
        interceptor, mock_next = self._make_client_interceptor()
        mock_input = MagicMock()
        mock_input.start_workflow_input = MagicMock()
        mock_input.start_workflow_input.workflow = "MyWorkflow"
        mock_input.start_workflow_input.headers = {}
        mock_input.update_workflow_input = MagicMock()
        mock_input.update_workflow_input.headers = {}

        with patch(_PATCH_GET_CURRENT_RUN, return_value=mock_run):
            await interceptor.start_update_with_start_workflow(mock_input)

        assert (
            _get_runtree_name(MockRunTree) == "StartUpdateWithStartWorkflow:MyWorkflow"
        )
        assert HEADER_KEY in mock_input.start_workflow_input.headers
        assert HEADER_KEY in mock_input.update_workflow_input.headers
        mock_next.start_update_with_start_workflow.assert_called_once()

    @pytest.mark.asyncio
    @patch(_PATCH_GET_CURRENT_RUN, return_value=None)
    @patch(_PATCH_RUNTREE)
    async def test_add_temporal_runs_false_skips_trace(
        self, MockRunTree: Any, mock_get_current: Any
    ) -> None:
        """With add_temporal_runs=False and no ambient context, no run is created
        and no headers are injected.

        _inject_current_context() is called unconditionally, but
        _get_current_run_for_propagation() returns None so headers are unchanged.
        """
        interceptor, mock_next = self._make_client_interceptor(add_temporal_runs=False)
        mock_input = MagicMock()
        mock_input.workflow = "MyWorkflow"
        mock_input.start_signal = None
        mock_input.headers = {}

        await interceptor.start_workflow(mock_input)

        # RunTree should NOT be created
        MockRunTree.assert_not_called()
        # _inject_current_context was called but found no ambient context
        mock_get_current.assert_called_once()
        # Headers should NOT have been modified (no ambient context)
        assert HEADER_KEY not in mock_input.headers
        # super() should still be called
        mock_next.start_workflow.assert_called_once()

    @pytest.mark.asyncio
    @patch(_PATCH_RUNTREE)
    async def test_add_temporal_runs_false_with_ambient_context(
        self, MockRunTree: Any
    ) -> None:
        """With add_temporal_runs=False but user-provided ambient context,
        no run is created but the ambient context IS injected into headers.

        This verifies that context propagation works even without plugin-created
        runs — if the user wraps the call in langsmith.trace(), that context
        gets propagated through Temporal headers.
        """
        mock_ambient_run = _make_mock_run()
        interceptor, mock_next = self._make_client_interceptor(add_temporal_runs=False)
        mock_input = MagicMock()
        mock_input.workflow = "MyWorkflow"
        mock_input.start_signal = None
        mock_input.headers = {}

        with patch(_PATCH_GET_CURRENT_RUN, return_value=mock_ambient_run):
            await interceptor.start_workflow(mock_input)

        # RunTree should NOT be created (no Temporal run)
        MockRunTree.assert_not_called()
        # But headers SHOULD be injected from the ambient context
        assert HEADER_KEY in mock_input.headers
        mock_next.start_workflow.assert_called_once()


# ===================================================================
# TestActivityInboundInterceptor
# ===================================================================


class TestActivityInboundInterceptor:
    """Tests for _LangSmithActivityInboundInterceptor."""

    def _make_activity_interceptor(
        self, *, add_temporal_runs: bool = True
    ) -> tuple[Any, MagicMock]:
        config = LangSmithInterceptor(
            client=MagicMock(), add_temporal_runs=add_temporal_runs
        )
        mock_next = MagicMock()
        mock_next.execute_activity = AsyncMock(return_value="activity_result")
        interceptor = config.intercept_activity(mock_next)
        return interceptor, mock_next

    @pytest.mark.asyncio
    @patch(_PATCH_TRACING_CTX)
    @patch(_PATCH_RUNTREE)
    @patch("temporalio.activity.info")
    async def test_execute_activity_creates_run_with_context_and_metadata(
        self, mock_info_fn: Any, MockRunTree: Any, mock_tracing_ctx: Any
    ) -> None:
        """Activity execution creates a correctly named run with metadata and parent context."""
        mock_info_fn.return_value = _mock_activity_info(
            activity_type="do_thing",
            workflow_id="wf-123",
            workflow_run_id="run-456",
            activity_id="act-789",
        )
        mock_run = _make_mock_run()
        MockRunTree.return_value = mock_run
        interceptor, mock_next = self._make_activity_interceptor()

        mock_input = MagicMock()
        mock_input.headers = {}

        result = await interceptor.execute_activity(mock_input)

        # Verify trace name and run_type
        assert _get_runtree_name(MockRunTree) == "RunActivity:do_thing"
        assert MockRunTree.call_args.kwargs.get("run_type") == "tool"
        # Verify metadata
        metadata = _get_runtree_metadata(MockRunTree)
        assert metadata["temporalWorkflowID"] == "wf-123"
        assert metadata["temporalRunID"] == "run-456"
        assert metadata["temporalActivityID"] == "act-789"
        # Verify tracing_context sets parent (wrapped in _ReplaySafeRunTree)
        mock_tracing_ctx.assert_called()
        ctx_kwargs = mock_tracing_ctx.call_args.kwargs
        parent = ctx_kwargs.get("parent")
        assert isinstance(parent, _ReplaySafeRunTree)
        assert parent._run is mock_run
        # Verify super() called and result passed through
        mock_next.execute_activity.assert_called_once()
        assert result == "activity_result"

    @pytest.mark.asyncio
    @patch(_PATCH_RUNTREE)
    @patch("temporalio.activity.info")
    async def test_execute_activity_no_header(
        self, mock_info_fn: Any, MockRunTree: Any
    ) -> None:
        """When no LangSmith header is present, activity still executes (no parent, no crash)."""
        mock_info_fn.return_value = _mock_activity_info()
        mock_run = _make_mock_run()
        MockRunTree.return_value = mock_run
        interceptor, _mock_next = self._make_activity_interceptor()

        mock_input = MagicMock()
        mock_input.headers = {}  # No LangSmith header

        result = await interceptor.execute_activity(mock_input)

        # Should still create a run (just without a parent)
        MockRunTree.assert_called_once()
        assert MockRunTree.call_args.kwargs.get("parent_run") is None
        assert result == "activity_result"


# ===================================================================
# TestWorkflowInboundInterceptor
# ===================================================================


class TestWorkflowInboundInterceptor:
    """Tests for _LangSmithWorkflowInboundInterceptor."""

    def _make_workflow_interceptors(
        self, *, add_temporal_runs: bool = True
    ) -> tuple[Any, MagicMock]:
        """Create workflow inbound interceptor and a mock next."""
        config = LangSmithInterceptor(
            client=MagicMock(), add_temporal_runs=add_temporal_runs
        )
        mock_next = MagicMock()
        mock_next.execute_workflow = AsyncMock(return_value="wf_result")
        mock_next.handle_signal = AsyncMock()
        mock_next.handle_query = AsyncMock(return_value="query_result")
        mock_next.handle_update_validator = MagicMock()
        mock_next.handle_update_handler = AsyncMock(return_value="update_result")

        # Get the workflow interceptor class
        wf_class_input = MagicMock()
        wf_interceptor_cls = config.workflow_interceptor_class(wf_class_input)
        assert wf_interceptor_cls is not None

        # Instantiate with mock next
        wf_interceptor = wf_interceptor_cls(mock_next)

        # Initialize with mock outbound
        mock_outbound = MagicMock()
        wf_interceptor.init(mock_outbound)

        return wf_interceptor, mock_next

    @pytest.mark.asyncio
    @patch(_PATCH_WF_NOW, return_value=datetime.now(timezone.utc))
    @patch(_PATCH_RUNTREE)
    @patch(_PATCH_IS_REPLAYING, return_value=False)
    @patch(_PATCH_IN_WORKFLOW, return_value=True)
    @patch(_PATCH_WF_INFO)
    async def test_execute_workflow(
        self,
        mock_wf_info: Any,
        _mock_in_wf: Any,
        _mock_replaying: Any,
        MockRunTree: Any,
        _mock_now: Any,
    ) -> None:
        """execute_workflow creates a run named RunWorkflow:{workflow_type}."""
        mock_wf_info.return_value = _mock_workflow_info(workflow_type="MyWorkflow")
        mock_run = _make_mock_run()
        MockRunTree.return_value = mock_run
        interceptor, mock_next = self._make_workflow_interceptors()

        mock_input = MagicMock()
        mock_input.headers = {}

        result = await interceptor.execute_workflow(mock_input)

        # Verify trace name
        assert _get_runtree_name(MockRunTree) == "RunWorkflow:MyWorkflow"
        # Verify metadata includes workflow ID and run ID
        metadata = _get_runtree_metadata(MockRunTree)
        assert metadata == {
            "temporalWorkflowID": "test-wf-id",
            "temporalRunID": "test-run-id",
        }
        # Verify super() called and result passed through
        mock_next.execute_workflow.assert_called_once()
        assert result == "wf_result"

    @pytest.mark.parametrize(
        "method,input_attr,input_val,expected_name",
        [
            ("handle_signal", "signal", "my_signal", "HandleSignal:my_signal"),
            ("handle_query", "query", "get_status", "HandleQuery:get_status"),
            (
                "handle_update_validator",
                "update",
                "my_update",
                "ValidateUpdate:my_update",
            ),
            ("handle_update_handler", "update", "my_update", "HandleUpdate:my_update"),
        ],
        ids=["signal", "query", "validator", "update_handler"],
    )
    @pytest.mark.asyncio
    @patch(_PATCH_WF_NOW, return_value=datetime.now(timezone.utc))
    @patch(_PATCH_RUNTREE)
    @patch(_PATCH_IS_REPLAYING, return_value=False)
    @patch(_PATCH_IN_WORKFLOW, return_value=True)
    @patch(_PATCH_WF_INFO)
    async def test_handler_creates_trace(
        self,
        mock_wf_info: Any,
        _mock_in_wf: Any,
        _mock_replaying: Any,
        MockRunTree: Any,
        _mock_now: Any,
        method: str,
        input_attr: str,
        input_val: str,
        expected_name: str,
    ) -> None:
        """Each workflow handler creates the correct trace name."""
        mock_wf_info.return_value = _mock_workflow_info()
        mock_run = _make_mock_run()
        MockRunTree.return_value = mock_run
        interceptor, mock_next = self._make_workflow_interceptors()

        mock_input = MagicMock()
        setattr(mock_input, input_attr, input_val)
        mock_input.headers = {}

        result = getattr(interceptor, method)(mock_input)
        if asyncio.iscoroutine(result):
            await result

        assert _get_runtree_name(MockRunTree) == expected_name
        getattr(mock_next, method).assert_called_once()


# ===================================================================
# TestWorkflowOutboundInterceptor
# ===================================================================


class TestWorkflowOutboundInterceptor:
    """Tests for _LangSmithWorkflowOutboundInterceptor."""

    def _make_outbound_interceptor(
        self, *, add_temporal_runs: bool = True
    ) -> tuple[Any, MagicMock, Any]:
        """Create outbound interceptor with mock next and ambient run.

        Returns (outbound_interceptor, mock_next, mock_current_run).
        """
        config = LangSmithInterceptor(
            client=MagicMock(), add_temporal_runs=add_temporal_runs
        )

        # Create mock next for inbound
        mock_inbound_next = MagicMock()
        mock_inbound_next.execute_workflow = AsyncMock()
        mock_inbound_next.handle_signal = AsyncMock()
        mock_inbound_next.handle_query = AsyncMock()
        mock_inbound_next.handle_update_validator = MagicMock()
        mock_inbound_next.handle_update_handler = AsyncMock()

        # Create inbound interceptor
        wf_class_input = MagicMock()
        wf_interceptor_cls = config.workflow_interceptor_class(wf_class_input)
        inbound = wf_interceptor_cls(mock_inbound_next)

        # Create mock outbound next
        mock_outbound_next = MagicMock()
        mock_outbound_next.start_activity = MagicMock()
        mock_outbound_next.start_local_activity = MagicMock()
        mock_outbound_next.start_child_workflow = AsyncMock()
        mock_outbound_next.signal_child_workflow = AsyncMock()
        mock_outbound_next.signal_external_workflow = AsyncMock()
        mock_outbound_next.continue_as_new = MagicMock()
        mock_outbound_next.start_nexus_operation = AsyncMock()

        # Initialize inbound (which should create the outbound)
        inbound.init(mock_outbound_next)

        # Create the outbound directly for unit testing
        from temporalio.contrib.langsmith._interceptor import (
            _LangSmithWorkflowOutboundInterceptor,
        )

        outbound = _LangSmithWorkflowOutboundInterceptor(mock_outbound_next, config)

        # Simulate active workflow execution via ambient context
        mock_current_run = _make_mock_run()

        return outbound, mock_outbound_next, mock_current_run

    @pytest.mark.parametrize(
        "method,input_attr,input_val,expected_name",
        [
            ("start_activity", "activity", "do_thing", "StartActivity:do_thing"),
            (
                "start_local_activity",
                "activity",
                "local_thing",
                "StartActivity:local_thing",
            ),
            (
                "start_child_workflow",
                "workflow",
                "ChildWorkflow",
                "StartChildWorkflow:ChildWorkflow",
            ),
            (
                "signal_child_workflow",
                "signal",
                "child_signal",
                "SignalChildWorkflow:child_signal",
            ),
            (
                "signal_external_workflow",
                "signal",
                "ext_signal",
                "SignalExternalWorkflow:ext_signal",
            ),
        ],
        ids=[
            "activity",
            "local_activity",
            "child_workflow",
            "signal_child",
            "signal_external",
        ],
    )
    @pytest.mark.asyncio
    @patch(_PATCH_WF_NOW, return_value=datetime.now(timezone.utc))
    @patch(_PATCH_RUNTREE)
    @patch(_PATCH_IS_REPLAYING, return_value=False)
    @patch(_PATCH_IN_WORKFLOW, return_value=True)
    async def test_creates_trace_and_injects_headers(
        self,
        _mock_in_wf: Any,
        _mock_replaying: Any,
        MockRunTree: Any,
        _mock_now: Any,
        method: str,
        input_attr: str,
        input_val: str,
        expected_name: str,
    ) -> None:
        """Each outbound method creates the correct trace and injects headers."""
        mock_run = _make_mock_run()
        MockRunTree.return_value = mock_run
        outbound, mock_next, mock_current_run = self._make_outbound_interceptor()

        mock_input = MagicMock()
        setattr(mock_input, input_attr, input_val)
        mock_input.headers = {}

        with patch(_PATCH_GET_CURRENT_RUN, return_value=mock_current_run):
            result = getattr(outbound, method)(mock_input)
            if asyncio.iscoroutine(result):
                await result

        assert _get_runtree_name(MockRunTree) == expected_name
        assert HEADER_KEY in mock_input.headers
        getattr(mock_next, method).assert_called_once()

    @patch(_PATCH_RUNTREE)
    @patch(_PATCH_IS_REPLAYING, return_value=False)
    @patch(_PATCH_IN_WORKFLOW, return_value=True)
    def test_continue_as_new(
        self, _mock_in_wf: Any, _mock_replaying: Any, MockRunTree: Any
    ) -> None:
        """continue_as_new does NOT create a new trace, but injects context from ambient run."""
        outbound, mock_next, mock_current_run = self._make_outbound_interceptor()

        mock_input = MagicMock()
        mock_input.headers = {}

        with patch(_PATCH_GET_CURRENT_RUN, return_value=mock_current_run):
            outbound.continue_as_new(mock_input)

        # No new RunTree should be created for continue_as_new
        MockRunTree.assert_not_called()
        # But headers SHOULD be modified (context from ambient run)
        assert HEADER_KEY in mock_input.headers
        mock_next.continue_as_new.assert_called_once()

    @pytest.mark.asyncio
    @patch(_PATCH_WF_NOW, return_value=datetime.now(timezone.utc))
    @patch(_PATCH_RUNTREE)
    @patch(_PATCH_IS_REPLAYING, return_value=False)
    @patch(_PATCH_IN_WORKFLOW, return_value=True)
    async def test_start_nexus_operation(
        self,
        _mock_in_wf: Any,
        _mock_replaying: Any,
        MockRunTree: Any,
        _mock_now: Any,
    ) -> None:
        """start_nexus_operation creates a trace named StartNexusOperation:{service}/{operation}."""
        mock_run = _make_mock_run()
        MockRunTree.return_value = mock_run
        outbound, mock_next, mock_current_run = self._make_outbound_interceptor()

        mock_input = MagicMock()
        mock_input.service = "MyService"
        mock_input.operation_name = "do_op"
        mock_input.headers = {}

        with patch(_PATCH_GET_CURRENT_RUN, return_value=mock_current_run):
            await outbound.start_nexus_operation(mock_input)

        assert _get_runtree_name(MockRunTree) == "StartNexusOperation:MyService/do_op"
        # Nexus uses string headers, so context injection uses _inject_nexus_context
        # The headers dict should be modified
        mock_next.start_nexus_operation.assert_called_once()


# ===================================================================
# TestNexusInboundInterceptor
# ===================================================================


class TestNexusInboundInterceptor:
    """Tests for _LangSmithNexusOperationInboundInterceptor."""

    def _make_nexus_interceptor(
        self, *, add_temporal_runs: bool = True
    ) -> tuple[Any, MagicMock]:
        config = LangSmithInterceptor(
            client=MagicMock(), add_temporal_runs=add_temporal_runs
        )
        mock_next = MagicMock()
        mock_next.execute_nexus_operation_start = AsyncMock()
        mock_next.execute_nexus_operation_cancel = AsyncMock()
        interceptor = config.intercept_nexus_operation(mock_next)
        return interceptor, mock_next

    @pytest.mark.asyncio
    @patch(_PATCH_RUNTREE)
    @patch(_PATCH_EXTRACT_NEXUS)
    async def test_execute_nexus_operation_start(
        self, mock_extract_nexus: Any, MockRunTree: Any
    ) -> None:
        """Creates a run named RunStartNexusOperationHandler:{service}/{operation}.

        Uses _extract_nexus_context (not _extract_context) for Nexus string headers.
        """
        mock_extract_nexus.return_value = None  # no parent
        mock_run = _make_mock_run()
        MockRunTree.return_value = mock_run
        interceptor, mock_next = self._make_nexus_interceptor()

        mock_input = MagicMock()
        mock_input.ctx = MagicMock()
        mock_input.ctx.service = "MyService"
        mock_input.ctx.operation = "start_op"
        mock_input.ctx.headers = {}

        await interceptor.execute_nexus_operation_start(mock_input)

        # Verify _extract_nexus_context was called (not _extract_context)
        mock_extract_nexus.assert_called_once()
        assert mock_extract_nexus.call_args[0][0] is mock_input.ctx.headers
        # Verify trace name
        assert (
            _get_runtree_name(MockRunTree)
            == "RunStartNexusOperationHandler:MyService/start_op"
        )
        # Verify run_type is "tool" for Nexus operations
        assert MockRunTree.call_args.kwargs.get("run_type") == "tool"
        mock_next.execute_nexus_operation_start.assert_called_once()

    @pytest.mark.asyncio
    @patch(_PATCH_RUNTREE)
    @patch(_PATCH_EXTRACT_NEXUS)
    async def test_execute_nexus_operation_cancel(
        self, mock_extract_nexus: Any, MockRunTree: Any
    ) -> None:
        """Creates a run named RunCancelNexusOperationHandler:{service}/{operation}.

        Uses _extract_nexus_context for context extraction.
        """
        mock_extract_nexus.return_value = None
        mock_run = _make_mock_run()
        MockRunTree.return_value = mock_run
        interceptor, mock_next = self._make_nexus_interceptor()

        mock_input = MagicMock()
        mock_input.ctx = MagicMock()
        mock_input.ctx.service = "MyService"
        mock_input.ctx.operation = "cancel_op"
        mock_input.ctx.headers = {}

        await interceptor.execute_nexus_operation_cancel(mock_input)

        mock_extract_nexus.assert_called_once()
        assert mock_extract_nexus.call_args[0][0] is mock_input.ctx.headers
        assert (
            _get_runtree_name(MockRunTree)
            == "RunCancelNexusOperationHandler:MyService/cancel_op"
        )
        assert MockRunTree.call_args.kwargs.get("run_type") == "tool"
        mock_next.execute_nexus_operation_cancel.assert_called_once()


# ===================================================================
# TestLazyClientPrevention
# ===================================================================


class TestLazyClientPrevention:
    """Tests that RunTree always receives ls_client= to prevent lazy Client creation."""

    @patch(_PATCH_IN_WORKFLOW, return_value=False)
    @patch(_PATCH_RUNTREE)
    def test_runtree_always_receives_ls_client(
        self, MockRunTree: Any, _mock_in_wf: Any
    ) -> None:
        """Every RunTree() created by _maybe_run receives ls_client= (pre-created client)."""
        mock_client = MagicMock()
        mock_run = _make_mock_run()
        MockRunTree.return_value = mock_run

        with _maybe_run(
            mock_client,
            "TestRun",
            add_temporal_runs=True,
            executor=_make_executor(),
        ):
            pass

        MockRunTree.assert_called_once()
        call_kwargs = MockRunTree.call_args.kwargs
        assert "ls_client" in call_kwargs
        assert call_kwargs["ls_client"] is mock_client


# ===================================================================
# TestAddTemporalRunsToggle
# ===================================================================


class TestAddTemporalRunsToggle:
    """Tests for the add_temporal_runs toggle."""

    @patch(_PATCH_RUNTREE)
    @patch(_PATCH_IN_WORKFLOW, return_value=False)
    def test_false_skips_traces(self, _mock_in_wf: Any, MockRunTree: Any) -> None:
        """With add_temporal_runs=False, _maybe_run yields None (no run created).

        Callers are responsible for propagating context even when the run is None.
        See test_false_still_propagates_context for the full behavior.
        """
        mock_client = MagicMock()
        with _maybe_run(
            mock_client,
            "TestRun",
            add_temporal_runs=False,
            executor=_make_executor(),
        ) as run:
            assert run is None
        MockRunTree.assert_not_called()

    @pytest.mark.asyncio
    @patch(_PATCH_TRACING_CTX)
    @patch(_PATCH_RUNTREE)
    @patch(_PATCH_IS_REPLAYING, return_value=False)
    @patch(_PATCH_IN_WORKFLOW, return_value=True)
    @patch(_PATCH_WF_INFO)
    @patch(f"{_MOD}.temporalio.activity.info")
    async def test_false_still_propagates_context(
        self,
        mock_act_info: Any,
        mock_wf_info: Any,
        _mock_in_wf: Any,
        _mock_replaying: Any,
        MockRunTree: Any,
        mock_tracing_ctx: Any,
    ) -> None:
        """With add_temporal_runs=False, no runs are created but context still propagates.

        1. Workflow outbound: injects the ambient run's context into headers even
           though no StartActivity run is created.
        2. Activity inbound: sets tracing_context(parent=extracted_parent)
           unconditionally (before _maybe_run), so @traceable code nests correctly
           even without a RunActivity run.
        """
        from temporalio.contrib.langsmith._interceptor import (
            _LangSmithWorkflowOutboundInterceptor,
        )

        mock_wf_info.return_value = _mock_workflow_info()
        mock_act_info.return_value = _mock_activity_info()

        # --- Workflow outbound: context propagation without run creation ---
        config = LangSmithInterceptor(client=MagicMock(), add_temporal_runs=False)

        # Create inbound interceptor
        wf_class_input = MagicMock()
        wf_interceptor_cls = config.workflow_interceptor_class(wf_class_input)
        mock_inbound_next = MagicMock()
        mock_inbound_next.execute_workflow = AsyncMock()
        inbound = wf_interceptor_cls(mock_inbound_next)

        # Create outbound interceptor
        mock_outbound_next = MagicMock()
        mock_outbound_next.start_activity = MagicMock()
        inbound.init(mock_outbound_next)
        outbound = _LangSmithWorkflowOutboundInterceptor(mock_outbound_next, config)

        # Simulate an ambient parent context (as if from active workflow execution)
        mock_parent = _make_mock_run()

        mock_input = MagicMock()
        mock_input.activity = "do_thing"
        mock_input.headers = {}

        with patch(_PATCH_GET_CURRENT_RUN, return_value=mock_parent):
            outbound.start_activity(mock_input)

        # No RunTree should be created (add_temporal_runs=False)
        MockRunTree.assert_not_called()
        # But headers SHOULD be injected from the inbound's parent context
        assert HEADER_KEY in mock_input.headers
        mock_outbound_next.start_activity.assert_called_once()

        # --- Activity inbound: tracing_context with extracted parent ---
        MockRunTree.reset_mock()
        mock_tracing_ctx.reset_mock()

        mock_act_next = MagicMock()
        mock_act_next.execute_activity = AsyncMock(return_value="result")
        act_interceptor = config.intercept_activity(mock_act_next)

        mock_act_input = MagicMock()
        mock_extracted_parent = _make_mock_run()

        with patch(f"{_MOD}._extract_context", return_value=mock_extracted_parent):
            await act_interceptor.execute_activity(mock_act_input)

        # No RunTree should be created (add_temporal_runs=False)
        MockRunTree.assert_not_called()
        # tracing_context SHOULD be called with the client and extracted parent
        # (unconditionally, before _maybe_run)
        mock_tracing_ctx.assert_called_once_with(
            client=config._client,
            enabled=True,
            project_name=None,
            parent=mock_extracted_parent,
        )
        mock_act_next.execute_activity.assert_called_once()

    @pytest.mark.asyncio
    @patch(_PATCH_TRACING_CTX)
    @patch(_PATCH_RUNTREE)
    @patch(f"{_MOD}.temporalio.activity.info")
    async def test_false_activity_no_parent_no_context(
        self,
        mock_act_info: Any,
        MockRunTree: Any,
        mock_tracing_ctx: Any,
    ) -> None:
        """With add_temporal_runs=False and no parent in headers, tracing_context
        is still called with the client (so @traceable can use it), but no parent.
        """
        mock_act_info.return_value = _mock_activity_info()
        config = LangSmithInterceptor(client=MagicMock(), add_temporal_runs=False)

        mock_act_next = MagicMock()
        mock_act_next.execute_activity = AsyncMock(return_value="result")
        act_interceptor = config.intercept_activity(mock_act_next)

        mock_act_input = MagicMock()

        with patch(f"{_MOD}._extract_context", return_value=None):
            await act_interceptor.execute_activity(mock_act_input)

        MockRunTree.assert_not_called()
        # tracing_context called with client and enabled (no parent)
        mock_tracing_ctx.assert_called_once_with(
            client=config._client, enabled=True, project_name=None, parent=None
        )
        mock_act_next.execute_activity.assert_called_once()
