"""Workflow worker."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
from datetime import timezone
from typing import Callable, Dict, List, MutableMapping, Optional, Sequence, Set, Type

import temporalio.activity
import temporalio.api.common.v1
import temporalio.bridge.client
import temporalio.bridge.proto.workflow_activation
import temporalio.bridge.proto.workflow_completion
import temporalio.bridge.worker
import temporalio.client
import temporalio.common
import temporalio.converter
import temporalio.exceptions
import temporalio.workflow

from ._interceptor import (
    Interceptor,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
)
from ._workflow_instance import (
    WorkflowInstance,
    WorkflowInstanceDetails,
    WorkflowRunner,
    _WorkflowExternFunctions,
)

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
        unsandboxed_workflow_runner: WorkflowRunner,
        data_converter: temporalio.converter.DataConverter,
        interceptors: Sequence[Interceptor],
        workflow_failure_exception_types: Sequence[Type[BaseException]],
        debug_mode: bool,
        disable_eager_activity_execution: bool,
        metric_meter: temporalio.common.MetricMeter,
        on_eviction_hook: Optional[
            Callable[
                [str, temporalio.bridge.proto.workflow_activation.RemoveFromCache], None
            ]
        ],
        disable_safe_eviction: bool,
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
        self._unsandboxed_workflow_runner = unsandboxed_workflow_runner
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
        self._extern_functions.update(
            **_WorkflowExternFunctions(__temporal_get_metric_meter=lambda: metric_meter)
        )
        self._workflow_failure_exception_types = workflow_failure_exception_types
        self._running_workflows: Dict[str, WorkflowInstance] = {}
        self._disable_eager_activity_execution = disable_eager_activity_execution
        self._on_eviction_hook = on_eviction_hook
        self._disable_safe_eviction = disable_safe_eviction
        self._throw_after_activation: Optional[Exception] = None

        # If there's a debug mode or a truthy TEMPORAL_DEBUG env var, disable
        # deadlock detection, otherwise set to 2 seconds
        self._deadlock_timeout_seconds = (
            None if debug_mode or os.environ.get("TEMPORAL_DEBUG") else 2
        )

        # Keep track of workflows that could not be evicted
        self._could_not_evict_count = 0

        # Set the worker-level failure exception types into the runner
        workflow_runner.set_worker_level_failure_exception_types(
            workflow_failure_exception_types
        )

        # Validate and build workflow dict
        self._workflows: Dict[str, temporalio.workflow._Definition] = {}
        self._dynamic_workflow: Optional[temporalio.workflow._Definition] = None
        for workflow in workflows:
            defn = temporalio.workflow._Definition.must_from_class(workflow)
            # Confirm name unique
            if defn.name in self._workflows:
                raise ValueError(f"More than one workflow named {defn.name}")
            # Prepare the workflow with the runner (this will error in the
            # sandbox if an import fails somehow)
            try:
                if defn.sandboxed:
                    workflow_runner.prepare_workflow(defn)
                else:
                    unsandboxed_workflow_runner.prepare_workflow(defn)
            except Exception as err:
                raise RuntimeError(f"Failed validating workflow {defn.name}") from err
            if defn.name:
                self._workflows[defn.name] = defn
            elif self._dynamic_workflow:
                raise TypeError("More than one dynamic workflow")
            else:
                self._dynamic_workflow = defn

    async def run(self) -> None:
        # Continually poll for workflow work
        task_tag = object()
        try:
            while True:
                act = await self._bridge_worker().poll_workflow_activation()

                # Schedule this as a task, but we don't need to track it or
                # await it. Rather we'll give it an attribute and wait for it
                # when done.
                task = asyncio.create_task(self._handle_activation(act))
                setattr(task, "__temporal_task_tag", task_tag)
        except temporalio.bridge.worker.PollShutdownError:
            pass
        except Exception as err:
            raise RuntimeError("Workflow worker failed") from err
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

        if self._throw_after_activation:
            raise self._throw_after_activation

    def notify_shutdown(self) -> None:
        if self._could_not_evict_count:
            logger.warn(
                f"Shutting down workflow worker, but {self._could_not_evict_count} "
                + "workflow(s) could not be evicted previously, so the shutdown will hang"
            )

    # Only call this if run() raised an error
    async def drain_poll_queue(self) -> None:
        while True:
            try:
                # Just take all tasks and say we can't handle them
                act = await self._bridge_worker().poll_workflow_activation()
                completion = temporalio.bridge.proto.workflow_completion.WorkflowActivationCompletion(
                    run_id=act.run_id
                )
                completion.failed.failure.message = "Worker shutting down"
                await self._bridge_worker().complete_workflow_activation(completion)
            except temporalio.bridge.worker.PollShutdownError:
                return

    async def _handle_activation(
        self, act: temporalio.bridge.proto.workflow_activation.WorkflowActivation
    ) -> None:
        global LOG_PROTOS

        # Extract a couple of jobs from the activation
        cache_remove_job = None
        start_job = None
        for job in act.jobs:
            if job.HasField("remove_from_cache"):
                cache_remove_job = job.remove_from_cache
            elif job.HasField("start_workflow"):
                start_job = job.start_workflow

        # Build default success completion (e.g. remove-job-only activations)
        completion = (
            temporalio.bridge.proto.workflow_completion.WorkflowActivationCompletion()
        )
        completion.successful.SetInParent()
        try:
            # Decode the activation if there's a codec and not cache remove job
            if self._data_converter.payload_codec and not cache_remove_job:
                await temporalio.bridge.worker.decode_activation(
                    act, self._data_converter.payload_codec
                )

            if LOG_PROTOS:
                logger.debug("Received workflow activation:\n%s", act)

            # If the workflow is not running yet and this isn't a cache remove
            # job, create it. We do not even fetch a workflow if it's a cache
            # remove job and safe evictions are enabled
            workflow = None
            if not cache_remove_job or not self._disable_safe_eviction:
                workflow = self._running_workflows.get(act.run_id)
            if not workflow and not cache_remove_job:
                # Must have a start job to create instance
                if not start_job:
                    raise RuntimeError(
                        "Missing start workflow, workflow could have unexpectedly been removed from cache"
                    )
                workflow = self._create_workflow_instance(act, start_job)
                self._running_workflows[act.run_id] = workflow
            elif start_job:
                # This should never happen
                logger.warn("Cache already exists for activation with start job")

            # Run activation in separate thread so we can check if it's
            # deadlocked
            if workflow:
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
                        f"[TMPRL1101] Potential deadlock detected, workflow didn't yield within {self._deadlock_timeout_seconds} second(s)"
                    )
        except Exception as err:
            # We cannot fail a cache eviction, we must just log and not complete
            # the activation (failed or otherwise). This should only happen in
            # cases of deadlock or tasks not properly completing, and yes this
            # means that a slot is forever taken.
            # TODO(cretz): Should we build a complex mechanism to continually
            # try the eviction until it succeeds?
            if cache_remove_job:
                logger.exception("Failed running eviction job, not evicting")
                self._could_not_evict_count += 1
                return

            logger.exception(
                "Failed handling activation on workflow with run ID %s", act.run_id
            )
            # Set completion failure
            completion.failed.failure.SetInParent()
            try:
                self._data_converter.failure_converter.to_failure(
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

        # If there is a remove-from-cache job, do so. We don't need to log a
        # warning if there's not, because create workflow failing for
        # unregistered workflow still triggers cache remove job
        if cache_remove_job:
            if act.run_id in self._running_workflows:
                logger.debug(
                    "Evicting workflow with run ID %s, message: %s",
                    act.run_id,
                    cache_remove_job.message,
                )
                del self._running_workflows[act.run_id]

        # Encode the completion if there's a codec and not cache remove job
        if self._data_converter.payload_codec and not cache_remove_job:
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

        # If there is a remove job and an eviction hook, run it
        if cache_remove_job and self._on_eviction_hook is not None:
            try:
                self._on_eviction_hook(act.run_id, cache_remove_job)
            except Exception as e:
                self._throw_after_activation = e
                logger.debug("Shutting down worker on eviction")
                self._bridge_worker().initiate_shutdown()

    def _create_workflow_instance(
        self,
        act: temporalio.bridge.proto.workflow_activation.WorkflowActivation,
        start: temporalio.bridge.proto.workflow_activation.StartWorkflow,
    ) -> WorkflowInstance:
        # Get the definition
        defn = self._workflows.get(start.workflow_type, self._dynamic_workflow)
        if not defn:
            workflow_names = ", ".join(sorted(self._workflows.keys()))
            raise temporalio.exceptions.ApplicationError(
                f"Workflow class {start.workflow_type} is not registered on this worker, available workflows: {workflow_names}",
                type="NotFoundError",
            )

        # Build info
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
            typed_search_attributes=temporalio.converter.decode_typed_search_attributes(
                start.search_attributes
            ),
            workflow_id=start.workflow_id,
            workflow_type=start.workflow_type,
        )

        # Create instance from details
        det = WorkflowInstanceDetails(
            payload_converter_class=self._data_converter.payload_converter_class,
            failure_converter_class=self._data_converter.failure_converter_class,
            interceptor_classes=self._interceptor_classes,
            defn=defn,
            info=info,
            randomness_seed=start.randomness_seed,
            extern_functions=self._extern_functions,
            disable_eager_activity_execution=self._disable_eager_activity_execution,
            worker_level_failure_exception_types=self._workflow_failure_exception_types,
        )
        if defn.sandboxed:
            return self._workflow_runner.create_instance(det)
        else:
            return self._unsandboxed_workflow_runner.create_instance(det)

    def nondeterminism_as_workflow_fail(self) -> bool:
        return any(
            issubclass(temporalio.workflow.NondeterminismError, typ)
            for typ in self._workflow_failure_exception_types
        )

    def nondeterminism_as_workflow_fail_for_types(self) -> Set[str]:
        return set(
            k
            for k, v in self._workflows.items()
            if any(
                issubclass(temporalio.workflow.NondeterminismError, typ)
                for typ in v.failure_exception_types
            )
        )
