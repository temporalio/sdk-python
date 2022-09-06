"""Workflow worker."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
from datetime import timezone
from typing import Callable, Dict, List, MutableMapping, Optional, Sequence, Type

import temporalio.activity
import temporalio.api.common.v1
import temporalio.bridge.client
import temporalio.bridge.proto
import temporalio.bridge.proto.activity_result
import temporalio.bridge.proto.child_workflow
import temporalio.bridge.proto.common
import temporalio.bridge.proto.workflow_activation
import temporalio.bridge.proto.workflow_commands
import temporalio.bridge.proto.workflow_completion
import temporalio.bridge.worker
import temporalio.client
import temporalio.common
import temporalio.converter
import temporalio.exceptions
import temporalio.workflow

from .interceptor import (
    Interceptor,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
)
from .workflow_instance import WorkflowInstance, WorkflowInstanceDetails, WorkflowRunner

logger = logging.getLogger(__name__)

# Set to true to log all activations and completions
LOG_PROTOS = False


class _WorkflowWorker:
    def __init__(
        self,
        *,
        bridge_worker: Callable[[], temporalio.bridge.worker.Worker],
        namespace: str,
        task_queue: str,
        workflows: Sequence[Type],
        workflow_task_executor: Optional[concurrent.futures.ThreadPoolExecutor],
        workflow_runner: WorkflowRunner,
        data_converter: temporalio.converter.DataConverter,
        interceptors: Sequence[Interceptor],
        debug_mode: bool,
        fail_on_eviction: bool = False,
    ) -> None:
        self._bridge_worker = bridge_worker
        self._namespace = namespace
        self._task_queue = task_queue
        self._workflow_task_executor = (
            workflow_task_executor
            or concurrent.futures.ThreadPoolExecutor(
                max_workers=max(os.cpu_count() or 4, 4),
                thread_name_prefix="temporal_workflow_",
            )
        )
        self._workflow_task_executor_user_provided = workflow_task_executor is not None
        self._workflow_runner = workflow_runner
        self._data_converter = data_converter
        # Build the interceptor classes and collect extern functions
        self._extern_functions: MutableMapping[str, Callable] = {}
        self._interceptor_classes: List[Type[WorkflowInboundInterceptor]] = []
        interceptor_class_input = WorkflowInterceptorClassInput(
            unsafe_extern_functions=self._extern_functions
        )
        for i in interceptors:
            interceptor_class = i.workflow_interceptor_class(interceptor_class_input)
            if interceptor_class:
                self._interceptor_classes.append(interceptor_class)
        self._running_workflows: Dict[str, WorkflowInstance] = {}
        self._fail_on_eviction = fail_on_eviction
        self._failure_due_to_eviction: Optional[Exception] = None

        # If there's a debug mode or a truthy TEMPORAL_DEBUG env var, disable
        # deadlock detection, otherwise set to 2 seconds
        self._deadlock_timeout_seconds = (
            None if debug_mode or os.environ.get("TEMPORAL_DEBUG") else 2
        )

        # Validate and build workflow dict
        self._workflows: Dict[str, temporalio.workflow._Definition] = {}
        for workflow in workflows:
            defn = temporalio.workflow._Definition.must_from_class(workflow)
            # Confirm name unique
            if defn.name in self._workflows:
                raise ValueError(f"More than one workflow named {defn.name}")
            self._workflows[defn.name] = defn

    async def run(self) -> None:
        # Continually poll for workflow work
        task_tag = object()
        try:
            while True:
                act = await self._bridge_worker().poll_workflow_activation()

                # If this activation is after we have received a failure due to
                # eviction, we ignore it w/ a failed task every time
                if self._failure_due_to_eviction:
                    completion = temporalio.bridge.proto.workflow_completion.WorkflowActivationCompletion(
                        run_id=act.run_id
                    )
                    completion.failed.failure.message = (
                        "Ignoring post-eviction activation"
                    )
                    await self._bridge_worker().complete_workflow_activation(completion)
                    continue

                # Schedule this as a task, but we don't need to track it or
                # await it. Rather we'll give it an attribute and wait for it
                # when done.
                task = asyncio.create_task(self._handle_activation(act))
                setattr(task, "__temporal_task_tag", task_tag)
        except temporalio.bridge.worker.PollShutdownError:
            pass
        except Exception:
            # Should never happen
            logger.exception(f"Workflow runner failed")
        finally:
            # Collect all tasks and wait for them to complete
            our_tasks = [
                t
                for t in asyncio.all_tasks()
                if getattr(t, "__temporal_task_tag", None) is task_tag
            ]
            if our_tasks:
                await asyncio.wait(our_tasks)
            # Shutdown the thread pool executor if we created it
            if not self._workflow_task_executor_user_provided:
                self._workflow_task_executor.shutdown()

        # If there was a failure due to eviction, raise it
        if self._failure_due_to_eviction:
            raise self._failure_due_to_eviction

    async def _handle_activation(
        self, act: temporalio.bridge.proto.workflow_activation.WorkflowActivation
    ) -> None:
        global LOG_PROTOS

        # Build default success completion (e.g. remove-job-only activations)
        completion = (
            temporalio.bridge.proto.workflow_completion.WorkflowActivationCompletion()
        )
        completion.successful.SetInParent()
        remove_job = None
        try:
            # Decode the activation if there's a codec
            if self._data_converter.payload_codec:
                await temporalio.bridge.worker.decode_activation(
                    act, self._data_converter.payload_codec
                )

            if LOG_PROTOS:
                logger.debug("Received workflow activation:\n%s", act)

            # We only have to run if there are any non-remove-from-cache jobs
            remove_job = next(
                (j for j in act.jobs if j.HasField("remove_from_cache")), None
            )
            if len(act.jobs) > 1 or not remove_job:
                # If the workflow is not running yet, create it
                workflow = self._running_workflows.get(act.run_id)
                if not workflow:
                    workflow = await self._create_workflow_instance(act)
                    self._running_workflows[act.run_id] = workflow
                # Run activation in separate thread so we can check if it's
                # deadlocked
                activate_task = asyncio.get_running_loop().run_in_executor(
                    self._workflow_task_executor,
                    workflow.activate,
                    act,
                )

                # Wait for deadlock timeout and set commands if successful
                try:
                    completion = await asyncio.wait_for(
                        activate_task, self._deadlock_timeout_seconds
                    )
                except asyncio.TimeoutError:
                    raise RuntimeError(
                        f"Potential deadlock detected, workflow didn't yield within {self._deadlock_timeout_seconds} second(s)"
                    )
        except Exception as err:
            logger.exception(
                "Failed handling activation on workflow with run ID %s", act.run_id
            )
            # Set completion failure
            completion.failed.failure.SetInParent()
            try:
                temporalio.exceptions.apply_exception_to_failure(
                    err,
                    self._data_converter.payload_converter,
                    completion.failed.failure,
                )
            except Exception as inner_err:
                logger.exception(
                    "Failed converting activation exception on workflow with run ID %s",
                    act.run_id,
                )
                completion.failed.failure.message = (
                    f"Failed converting activation exception: {inner_err}"
                )

        # Always set the run ID on the completion
        completion.run_id = act.run_id

        # Encode the completion if there's a codec
        if self._data_converter.payload_codec:
            try:
                await temporalio.bridge.worker.encode_completion(
                    completion, self._data_converter.payload_codec
                )
            except Exception as err:
                logger.exception(
                    "Failed encoding completion on workflow with run ID %s", act.run_id
                )
                completion.failed.Clear()
                completion.failed.failure.message = f"Failed encoding completion: {err}"

        # Send off completion
        if LOG_PROTOS:
            logger.debug("Sending workflow completion:\n%s", completion)
        try:
            await self._bridge_worker().complete_workflow_activation(completion)
        except Exception:
            # TODO(cretz): Per others, this is supposed to crash the worker
            logger.exception(
                "Failed completing activation on workflow with run ID %s", act.run_id
            )

        # If there is a remove-from-cache job, do so
        if remove_job:
            if act.run_id in self._running_workflows:
                logger.debug(
                    "Evicting workflow with run ID %s, message: %s",
                    act.run_id,
                    remove_job.remove_from_cache.message,
                )
                del self._running_workflows[act.run_id]
            else:
                logger.debug(
                    "Eviction request on unknown workflow with run ID %s, message: %s",
                    act.run_id,
                    remove_job.remove_from_cache.message,
                )
            # If we are failing on eviction, set the error and shutdown the
            # entire worker
            if self._fail_on_eviction and not self._failure_due_to_eviction:
                logger.debug("Shutting down worker on eviction")
                if (
                    remove_job.remove_from_cache.reason
                    == temporalio.bridge.proto.workflow_activation.RemoveFromCache.EvictionReason.NONDETERMINISM
                ):
                    self._failure_due_to_eviction = (
                        temporalio.workflow.NondeterminismError(
                            remove_job.remove_from_cache.message
                        )
                    )
                else:
                    self._failure_due_to_eviction = RuntimeError(
                        f"{remove_job.remove_from_cache.reason}: {remove_job.remove_from_cache.message}"
                    )
                asyncio.create_task(self._bridge_worker().shutdown())

    async def _create_workflow_instance(
        self, act: temporalio.bridge.proto.workflow_activation.WorkflowActivation
    ) -> WorkflowInstance:
        # First find the start workflow job
        start_job = next((j for j in act.jobs if j.HasField("start_workflow")), None)
        if not start_job:
            raise RuntimeError(
                "Missing start workflow, workflow could have unexpectedly been removed from cache"
            )

        # Get the definition
        defn = self._workflows.get(start_job.start_workflow.workflow_type)
        if not defn:
            workflow_names = ", ".join(sorted(self._workflows.keys()))
            raise temporalio.exceptions.ApplicationError(
                f"Workflow class {start_job.start_workflow.workflow_type} is not registered on this worker, available workflows: {workflow_names}",
                type="NotFoundError",
            )

        # Build info
        start = start_job.start_workflow
        parent: Optional[temporalio.workflow.ParentInfo] = None
        if start.HasField("parent_workflow_info"):
            parent = temporalio.workflow.ParentInfo(
                namespace=start.parent_workflow_info.namespace,
                run_id=start.parent_workflow_info.run_id,
                workflow_id=start.parent_workflow_info.workflow_id,
            )
        info = temporalio.workflow.Info(
            attempt=start.attempt,
            continued_run_id=start.continued_from_execution_run_id or None,
            cron_schedule=start.cron_schedule or None,
            execution_timeout=start.workflow_execution_timeout.ToTimedelta()
            if start.HasField("workflow_execution_timeout")
            else None,
            headers=dict(start.headers),
            namespace=self._namespace,
            parent=parent,
            raw_memo=dict(start.memo.fields),
            retry_policy=temporalio.common.RetryPolicy.from_proto(start.retry_policy)
            if start.HasField("retry_policy")
            else None,
            run_id=act.run_id,
            run_timeout=start.workflow_run_timeout.ToTimedelta()
            if start.HasField("workflow_run_timeout")
            else None,
            search_attributes=temporalio.converter.decode_search_attributes(
                start.search_attributes
            ),
            start_time=act.timestamp.ToDatetime().replace(tzinfo=timezone.utc),
            task_queue=self._task_queue,
            task_timeout=start.workflow_task_timeout.ToTimedelta(),
            workflow_id=start.workflow_id,
            workflow_type=start.workflow_type,
        )

        # Create instance from details
        return await self._workflow_runner.create_instance(
            WorkflowInstanceDetails(
                payload_converter_class=self._data_converter.payload_converter_class,
                interceptor_classes=self._interceptor_classes,
                defn=defn,
                info=info,
                randomness_seed=start.randomness_seed,
                extern_functions=self._extern_functions,
            )
        )
