"""Unit tests for _ReplaySafeRunTree and _RootReplaySafeRunTreeFactory.

Covers create_child propagation, executor-backed post/patch,
replay suppression, and post-shutdown fallback.
"""

from __future__ import annotations

import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from langsmith.run_trees import RunTree

from temporalio.contrib.langsmith._interceptor import (
    _ReplaySafeRunTree,
    _RootReplaySafeRunTreeFactory,
    _uuid_from_random,
)

# Common patch targets
_MOD = "temporalio.contrib.langsmith._interceptor"
_PATCH_IN_WORKFLOW = f"{_MOD}.temporalio.workflow.in_workflow"
_PATCH_IS_REPLAYING = f"{_MOD}.temporalio.workflow.unsafe.is_replaying_history_events"
_PATCH_WF_NOW = f"{_MOD}.temporalio.workflow.now"
_PATCH_GET_WF_RANDOM = f"{_MOD}._get_workflow_random"


def _make_executor() -> ThreadPoolExecutor:
    """Create a single-worker executor for tests."""
    return ThreadPoolExecutor(max_workers=1)


def _make_mock_run(**kwargs: Any) -> MagicMock:
    """Create a mock RunTree."""
    mock = MagicMock(spec=RunTree)
    mock.to_headers.return_value = {"langsmith-trace": "test"}
    mock.ls_client = kwargs.get("ls_client", MagicMock())
    mock.session_name = kwargs.get("session_name", "test-session")
    mock.replicas = kwargs.get("replicas", [])
    mock.id = kwargs.get("id", uuid.uuid4())
    mock.name = kwargs.get("name", "test-run")
    # create_child returns another mock RunTree by default
    child_mock = MagicMock(spec=RunTree)
    child_mock.id = uuid.uuid4()
    child_mock.ls_client = mock.ls_client
    child_mock.session_name = mock.session_name
    child_mock.replicas = mock.replicas
    mock.create_child.return_value = child_mock
    return mock


# ===================================================================
# TestCreateChildPropagation
# ===================================================================


class TestCreateChildPropagation:
    """Tests for _ReplaySafeRunTree.create_child() override."""

    def test_create_child_returns_replay_safe_run_tree(self) -> None:
        """create_child() must return a _ReplaySafeRunTree wrapping the child."""
        executor = _make_executor()
        mock_run = _make_mock_run()
        parent = _ReplaySafeRunTree(mock_run, executor=executor)

        child = parent.create_child(name="child-op", run_type="chain")

        assert isinstance(child, _ReplaySafeRunTree)
        # The wrapped child should be the result of the inner run's create_child
        mock_run.create_child.assert_called_once()

    @patch(_PATCH_GET_WF_RANDOM)
    @patch(_PATCH_WF_NOW)
    @patch(_PATCH_IN_WORKFLOW, return_value=True)
    def test_create_child_injects_deterministic_ids_in_workflow(
        self,
        _mock_in_wf: Any,
        mock_now: Any,
        mock_get_random: Any,
    ) -> None:
        """In workflow context, create_child injects deterministic run_id and start_time."""
        import random as stdlib_random

        rng = stdlib_random.Random(42)
        mock_get_random.return_value = rng
        fake_now = datetime(2025, 1, 1, tzinfo=timezone.utc)
        mock_now.return_value = fake_now

        expected_id = _uuid_from_random(stdlib_random.Random(42))  # same seed

        executor = _make_executor()
        mock_run = _make_mock_run()
        parent = _ReplaySafeRunTree(mock_run, executor=executor)

        # Simulate what _setup_run does: passes run_id=None explicitly
        child = parent.create_child(name="child-op", run_type="chain", run_id=None)

        assert isinstance(child, _ReplaySafeRunTree)
        # Verify the kwargs passed to inner create_child had deterministic values
        call_kwargs = mock_run.create_child.call_args.kwargs
        assert call_kwargs["run_id"] == expected_id
        assert call_kwargs["start_time"] == fake_now

    def test_create_child_passes_through_kwargs(self) -> None:
        """create_child passes through all kwargs to the inner run's create_child."""
        executor = _make_executor()
        mock_run = _make_mock_run()
        parent = _ReplaySafeRunTree(mock_run, executor=executor)

        child = parent.create_child(
            name="child-op",
            run_type="llm",
            inputs={"prompt": "hello"},
            tags=["test"],
            extra_kwarg="future-proof",
        )

        assert isinstance(child, _ReplaySafeRunTree)
        call_kwargs = mock_run.create_child.call_args.kwargs
        assert call_kwargs["name"] == "child-op"
        assert call_kwargs["run_type"] == "llm"
        assert call_kwargs["inputs"] == {"prompt": "hello"}
        assert call_kwargs["tags"] == ["test"]
        assert call_kwargs["extra_kwarg"] == "future-proof"

    def test_create_child_propagates_executor_to_child(self) -> None:
        """The child _ReplaySafeRunTree must receive the same executor reference."""
        executor = _make_executor()
        mock_run = _make_mock_run()
        parent = _ReplaySafeRunTree(mock_run, executor=executor)

        child = parent.create_child(name="child-op", run_type="chain")

        assert isinstance(child, _ReplaySafeRunTree)
        # Child should have the same executor
        assert child._executor is executor

    @patch(_PATCH_IN_WORKFLOW, return_value=False)
    def test_create_child_no_deterministic_ids_outside_workflow(
        self, _mock_in_wf: Any
    ) -> None:
        """Outside workflow context, create_child does NOT inject deterministic IDs."""
        executor = _make_executor()
        mock_run = _make_mock_run()
        parent = _ReplaySafeRunTree(mock_run, executor=executor)

        child = parent.create_child(name="child-op", run_type="chain", run_id=None)

        assert isinstance(child, _ReplaySafeRunTree)
        # run_id should remain None (not overridden)
        call_kwargs = mock_run.create_child.call_args.kwargs
        assert call_kwargs.get("run_id") is None

    @patch(_PATCH_GET_WF_RANDOM)
    @patch(_PATCH_WF_NOW)
    @patch(_PATCH_IN_WORKFLOW, return_value=True)
    def test_create_child_respects_explicit_run_id(
        self,
        _mock_in_wf: Any,
        mock_now: Any,
        mock_get_random: Any,
    ) -> None:
        """If run_id is explicitly provided (not None), create_child preserves it."""
        import random as stdlib_random

        mock_get_random.return_value = stdlib_random.Random(42)
        mock_now.return_value = datetime(2025, 1, 1, tzinfo=timezone.utc)

        executor = _make_executor()
        mock_run = _make_mock_run()
        parent = _ReplaySafeRunTree(mock_run, executor=executor)

        explicit_id = uuid.uuid4()
        child = parent.create_child(
            name="child-op", run_type="chain", run_id=explicit_id
        )

        assert isinstance(child, _ReplaySafeRunTree)
        call_kwargs = mock_run.create_child.call_args.kwargs
        assert call_kwargs["run_id"] == explicit_id


# ===================================================================
# TestExecutorBackedPostPatch
# ===================================================================


class TestExecutorBackedPostPatch:
    """Tests for executor-backed post()/patch() in _ReplaySafeRunTree."""

    @patch(_PATCH_IS_REPLAYING, return_value=False)
    @patch(_PATCH_IN_WORKFLOW, return_value=True)
    def test_post_submits_to_executor_in_workflow(
        self, _mock_in_wf: Any, _mock_replaying: Any
    ) -> None:
        """In workflow context, post() submits to executor, not inline."""
        executor = _make_executor()
        mock_run = _make_mock_run()
        calling_thread = threading.current_thread()
        post_thread: list[threading.Thread] = []

        def record_thread(*_args: Any, **_kwargs: Any) -> None:
            post_thread.append(threading.current_thread())

        mock_run.post.side_effect = record_thread
        tree = _ReplaySafeRunTree(mock_run, executor=executor)

        tree.post()

        # Wait for executor to finish
        executor.shutdown(wait=True)

        # post should have been called on the inner run via executor
        mock_run.post.assert_called_once()
        # Verify it ran on the executor thread, not the calling thread
        assert len(post_thread) == 1
        assert post_thread[0] is not calling_thread

    @patch(_PATCH_IS_REPLAYING, return_value=False)
    @patch(_PATCH_IN_WORKFLOW, return_value=True)
    def test_patch_submits_to_executor_in_workflow(
        self, _mock_in_wf: Any, _mock_replaying: Any
    ) -> None:
        """In workflow context, patch() submits to executor, not inline."""
        executor = _make_executor()
        mock_run = _make_mock_run()
        tree = _ReplaySafeRunTree(mock_run, executor=executor)

        tree.patch()

        executor.shutdown(wait=True)
        mock_run.patch.assert_called_once()

    @patch(_PATCH_IN_WORKFLOW, return_value=False)
    def test_post_delegates_directly_outside_workflow(self, _mock_in_wf: Any) -> None:
        """Outside workflow, post() delegates directly to the inner run."""
        executor = _make_executor()
        mock_run = _make_mock_run()
        tree = _ReplaySafeRunTree(mock_run, executor=executor)

        tree.post()

        mock_run.post.assert_called_once()

    @patch(_PATCH_IN_WORKFLOW, return_value=False)
    def test_patch_delegates_directly_outside_workflow(self, _mock_in_wf: Any) -> None:
        """Outside workflow, patch() delegates directly to the inner run."""
        executor = _make_executor()
        mock_run = _make_mock_run()
        tree = _ReplaySafeRunTree(mock_run, executor=executor)

        tree.patch(exclude_inputs=True)

        mock_run.patch.assert_called_once_with(exclude_inputs=True)

    @patch(_PATCH_IS_REPLAYING, return_value=False)
    @patch(_PATCH_IN_WORKFLOW, return_value=True)
    def test_post_error_logged_via_done_callback(
        self,
        _mock_in_wf: Any,
        _mock_replaying: Any,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Errors from fire-and-forget post() are logged via Future.add_done_callback."""
        executor = _make_executor()
        mock_run = _make_mock_run()
        mock_run.post.side_effect = RuntimeError("LangSmith API error")
        tree = _ReplaySafeRunTree(mock_run, executor=executor)

        with caplog.at_level(logging.ERROR):
            tree.post()
            executor.shutdown(wait=True)

        # The error should have been logged
        assert any("LangSmith API error" in record.message for record in caplog.records)

    @patch(_PATCH_IS_REPLAYING, return_value=False)
    @patch(_PATCH_IN_WORKFLOW, return_value=True)
    def test_patch_error_logged_via_done_callback(
        self,
        _mock_in_wf: Any,
        _mock_replaying: Any,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Errors from fire-and-forget patch() are logged via Future.add_done_callback."""
        executor = _make_executor()
        mock_run = _make_mock_run()
        mock_run.patch.side_effect = RuntimeError("LangSmith patch error")
        tree = _ReplaySafeRunTree(mock_run, executor=executor)

        with caplog.at_level(logging.ERROR):
            tree.patch()
            executor.shutdown(wait=True)

        # The error should have been logged
        assert any(
            "LangSmith patch error" in record.message for record in caplog.records
        )


# ===================================================================
# TestReplaySuppression
# ===================================================================


class TestReplaySuppression:
    """Tests for _is_replaying() check before executor submission."""

    @patch(_PATCH_IS_REPLAYING, return_value=True)
    @patch(_PATCH_IN_WORKFLOW, return_value=True)
    def test_post_noop_during_replay(
        self, _mock_in_wf: Any, _mock_replaying: Any
    ) -> None:
        """post() is a no-op during replay — no executor submission."""
        executor = _make_executor()
        mock_run = _make_mock_run()
        tree = _ReplaySafeRunTree(mock_run, executor=executor)

        tree.post()

        executor.shutdown(wait=True)
        mock_run.post.assert_not_called()

    @patch(_PATCH_IS_REPLAYING, return_value=True)
    @patch(_PATCH_IN_WORKFLOW, return_value=True)
    def test_patch_noop_during_replay(
        self, _mock_in_wf: Any, _mock_replaying: Any
    ) -> None:
        """patch() is a no-op during replay."""
        executor = _make_executor()
        mock_run = _make_mock_run()
        tree = _ReplaySafeRunTree(mock_run, executor=executor)

        tree.patch()

        executor.shutdown(wait=True)
        mock_run.patch.assert_not_called()

    @patch(_PATCH_IS_REPLAYING, return_value=True)
    @patch(_PATCH_IN_WORKFLOW, return_value=True)
    def test_end_noop_during_replay(
        self, _mock_in_wf: Any, _mock_replaying: Any
    ) -> None:
        """end() is a no-op during replay."""
        executor = _make_executor()
        mock_run = _make_mock_run()
        tree = _ReplaySafeRunTree(mock_run, executor=executor)

        tree.end(outputs={"result": "done"})

        mock_run.end.assert_not_called()

    @patch(_PATCH_WF_NOW, return_value=datetime.now(timezone.utc))
    @patch(_PATCH_IS_REPLAYING, return_value=False)
    @patch(_PATCH_IN_WORKFLOW, return_value=True)
    def test_end_delegates_during_normal_execution(
        self, _mock_in_wf: Any, _mock_replaying: Any, _mock_now: Any
    ) -> None:
        """end() delegates to self._run.end() during normal (non-replay) execution."""
        executor = _make_executor()
        mock_run = _make_mock_run()
        tree = _ReplaySafeRunTree(mock_run, executor=executor)

        tree.end(outputs={"result": "done"}, error="some error")

        mock_run.end.assert_called_once()
        call_kwargs = mock_run.end.call_args.kwargs
        assert call_kwargs["outputs"] == {"result": "done"}
        assert call_kwargs["error"] == "some error"
        assert "end_time" in call_kwargs


# ===================================================================
# TestRootReplaySafeRunTreeFactory
# ===================================================================


class TestRootReplaySafeRunTreeFactory:
    """Tests for _RootReplaySafeRunTreeFactory subclass."""

    def _make_factory(self, **kwargs: Any) -> _RootReplaySafeRunTreeFactory:
        """Create a _RootReplaySafeRunTreeFactory for testing."""
        from temporalio.contrib.langsmith._interceptor import (
            _RootReplaySafeRunTreeFactory,
        )

        executor = kwargs.pop("executor", _make_executor())
        mock_client = kwargs.pop("ls_client", MagicMock())
        return _RootReplaySafeRunTreeFactory(
            ls_client=mock_client, executor=executor, **kwargs
        )

    def test_post_raises_runtime_error(self) -> None:
        """Factory's post() raises RuntimeError — factory must never be posted."""
        factory = self._make_factory()
        with pytest.raises(RuntimeError, match="must never be posted"):
            factory.post()

    def test_patch_raises_runtime_error(self) -> None:
        """Factory's patch() raises RuntimeError — factory must never be patched."""
        factory = self._make_factory()
        with pytest.raises(RuntimeError, match="must never be patched"):
            factory.patch()

    def test_end_raises_runtime_error(self) -> None:
        """Factory's end() raises RuntimeError — factory must never be ended."""
        factory = self._make_factory()
        with pytest.raises(RuntimeError, match="must never be ended"):
            factory.end(outputs={"status": "ok"})

    @patch(_PATCH_GET_WF_RANDOM)
    @patch(_PATCH_WF_NOW)
    @patch(_PATCH_IN_WORKFLOW, return_value=True)
    def test_create_child_returns_root_replay_safe_run_tree(
        self,
        _mock_in_wf: Any,
        mock_now: Any,
        mock_get_random: Any,
    ) -> None:
        """Factory's create_child creates a root _ReplaySafeRunTree (no parent_run_id)."""
        import random as stdlib_random

        mock_get_random.return_value = stdlib_random.Random(42)
        mock_now.return_value = datetime(2025, 1, 1, tzinfo=timezone.utc)

        executor = _make_executor()
        mock_client = MagicMock()
        factory = self._make_factory(ls_client=mock_client, executor=executor)

        child = factory.create_child(name="traceable-fn", run_type="chain")

        assert isinstance(child, _ReplaySafeRunTree)
        # Child should be a root run — no parent_run_id
        assert child._run.parent_run_id is None

    def test_create_child_inherits_client_session_and_replicas(self) -> None:
        """Factory's children inherit ls_client, session_name, replicas."""
        executor = _make_executor()
        mock_client = MagicMock()
        mock_replicas = [MagicMock(), MagicMock()]
        factory = self._make_factory(
            ls_client=mock_client,
            executor=executor,
            session_name="my-project",
            replicas=mock_replicas,
        )

        with patch(_PATCH_IN_WORKFLOW, return_value=False):
            child = factory.create_child(name="traceable-fn", run_type="chain")

        assert isinstance(child, _ReplaySafeRunTree)
        # Child should have the factory's ls_client, session_name, and replicas
        assert child.ls_client is mock_client
        assert child.session_name == "my-project"
        assert child.replicas is mock_replicas

    def test_create_child_propagates_executor(self) -> None:
        """Factory propagates executor to children."""
        executor = _make_executor()
        factory = self._make_factory(executor=executor)

        with patch(_PATCH_IN_WORKFLOW, return_value=False):
            child = factory.create_child(name="traceable-fn", run_type="chain")

        assert isinstance(child, _ReplaySafeRunTree)
        assert child._executor is executor

    def test_create_child_maps_run_id_to_id(self) -> None:
        """Factory's create_child maps run_id kwarg to id on the resulting RunTree.

        The run_id kwarg is mapped to id, matching LangSmith's
        RunTree.create_child convention (run_trees.py:545).
        """
        executor = _make_executor()
        factory = self._make_factory(executor=executor)
        explicit_id = uuid.uuid4()

        with patch(_PATCH_IN_WORKFLOW, return_value=False):
            child = factory.create_child(
                name="traceable-fn", run_type="chain", run_id=explicit_id
            )

        assert isinstance(child, _ReplaySafeRunTree)
        # The underlying RunTree should have id set to the passed run_id
        assert child._run.id == explicit_id

    def test_factory_not_in_collected_runs(self) -> None:
        """Factory's post/patch/end raise RuntimeError — factory is never traced."""
        factory = self._make_factory()

        with pytest.raises(RuntimeError):
            factory.post()
        with pytest.raises(RuntimeError):
            factory.patch()
        with pytest.raises(RuntimeError):
            factory.end()


# ===================================================================
# TestPostTimingDelayedExecution
# ===================================================================


class TestPostTimingDelayedExecution:
    """Tests for post() timing when executor is busy.

    When post() is delayed (executor busy), create_run includes finalized data
    (outputs/end_time), and the subsequent update_run from patch() is idempotent.
    """

    @patch(_PATCH_IS_REPLAYING, return_value=False)
    @patch(_PATCH_IN_WORKFLOW, return_value=True)
    def test_post_patch_fifo_ordering(
        self, _mock_in_wf: Any, _mock_replaying: Any
    ) -> None:
        """post() always completes before patch() starts (FIFO via single-worker executor)."""
        executor = _make_executor()
        mock_run = _make_mock_run()
        call_order: list[str] = []

        def record_post(*_args: Any, **_kwargs: Any) -> None:
            call_order.append("post")

        def record_patch(*_args: Any, **_kwargs: Any) -> None:
            call_order.append("patch")

        mock_run.post.side_effect = record_post
        mock_run.patch.side_effect = record_patch

        tree = _ReplaySafeRunTree(mock_run, executor=executor)

        tree.post()
        tree.patch()

        executor.shutdown(wait=True)

        assert call_order == ["post", "patch"]

    @patch(_PATCH_IS_REPLAYING, return_value=False)
    @patch(_PATCH_IN_WORKFLOW, return_value=True)
    def test_delayed_post_reads_finalized_fields(
        self, _mock_in_wf: Any, _mock_replaying: Any
    ) -> None:
        """When post() is delayed, create_run sees finalized outputs/end_time.

        Simulates: block executor → submit post() (queued) → call end() on
        "workflow thread" to set outputs/end_time → release blocker → verify
        post() saw the finalized fields via _get_dicts_safe().
        """
        executor = _make_executor()
        mock_run = _make_mock_run()

        # Barrier to block executor so post() is delayed
        blocker = threading.Event()
        post_saw_outputs: list[Any] = []
        post_saw_end_time: list[Any] = []

        # Block the executor with a dummy task
        def blocking_task() -> None:
            blocker.wait(timeout=5.0)

        executor.submit(blocking_task)

        # Record what fields post() sees when it finally runs
        def capturing_post(*_args: Any, **_kwargs: Any) -> None:
            post_saw_outputs.append(getattr(mock_run, "outputs", None))
            post_saw_end_time.append(getattr(mock_run, "end_time", None))

        mock_run.post.side_effect = capturing_post

        tree = _ReplaySafeRunTree(mock_run, executor=executor)

        # Submit post() — it's queued behind the blocker
        tree.post()

        # Simulate end() on the "workflow thread" while post() is still queued
        finalized_outputs = {"result": "done"}
        finalized_end_time = datetime(2025, 6, 1, tzinfo=timezone.utc)
        mock_run.outputs = finalized_outputs
        mock_run.end_time = finalized_end_time

        # Release the blocker — post() now runs and reads finalized fields
        blocker.set()
        executor.shutdown(wait=True)

        # post() should have seen the finalized outputs and end_time
        assert len(post_saw_outputs) == 1
        assert post_saw_outputs[0] == finalized_outputs
        assert len(post_saw_end_time) == 1
        assert post_saw_end_time[0] == finalized_end_time


# ===================================================================
# TestPostShutdownRaises
# ===================================================================


class TestPostShutdownRaises:
    """Tests that post/patch raise after executor shutdown."""

    @patch(_PATCH_IS_REPLAYING, return_value=False)
    @patch(_PATCH_IN_WORKFLOW, return_value=True)
    def test_post_raises_after_shutdown(
        self, _mock_in_wf: Any, _mock_replaying: Any
    ) -> None:
        """After executor.shutdown(), post() raises RuntimeError."""
        executor = _make_executor()
        executor.shutdown(wait=True)

        mock_run = _make_mock_run()
        tree = _ReplaySafeRunTree(mock_run, executor=executor)

        with pytest.raises(RuntimeError):
            tree.post()

    @patch(_PATCH_IS_REPLAYING, return_value=False)
    @patch(_PATCH_IN_WORKFLOW, return_value=True)
    def test_patch_raises_after_shutdown(
        self, _mock_in_wf: Any, _mock_replaying: Any
    ) -> None:
        """After executor.shutdown(), patch() raises RuntimeError."""
        executor = _make_executor()
        executor.shutdown(wait=True)

        mock_run = _make_mock_run()
        tree = _ReplaySafeRunTree(mock_run, executor=executor)

        with pytest.raises(RuntimeError):
            tree.patch()


# ===================================================================
# Test_ReplaySafeRunTreeConstructor
# ===================================================================


class Test_ReplaySafeRunTreeConstructor:
    """Tests for _ReplaySafeRunTree accepting executor parameter."""

    def test_constructor_stores_executor(self) -> None:
        """The executor is stored and accessible."""
        executor = _make_executor()
        mock_run = _make_mock_run()
        tree = _ReplaySafeRunTree(mock_run, executor=executor)
        assert tree._executor is executor
