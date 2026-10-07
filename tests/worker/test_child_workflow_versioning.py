"""Serverless tests of child workflow versioning commands and start failures."""

from datetime import datetime, timedelta, timezone

import pytest

import temporalio.api.common.v1
import temporalio.api.deployment.v1
import temporalio.api.enums.v1
import temporalio.api.workflow.v1
import temporalio.bridge.proto.child_workflow as child_workflow
import temporalio.bridge.proto.workflow_activation as workflow_activation
import temporalio.common as common
import temporalio.converter as converter
import temporalio.exceptions as exceptions
import temporalio.worker
import temporalio.workflow as workflow


@workflow.defn
class ParentWorkflow:
    @workflow.run
    async def run(self, execute: bool, override_kind: str, error_name: str) -> str:
        version = common.WorkerDeploymentVersion("deployment", "build")
        override = {
            "absent": None,
            "PinnedVersioningOverride": common.PinnedVersioningOverride(version),
            "AutoUpgradeVersioningOverride": common.AutoUpgradeVersioningOverride(),
            "OneTimeVersioningOverride": common.OneTimeVersioningOverride(version),
        }[override_kind]
        try:
            if execute:
                await workflow.execute_child_workflow(
                    "ChildWorkflow", id="child-id", versioning_override=override
                )
            else:
                await workflow.start_child_workflow(
                    "ChildWorkflow", id="child-id", versioning_override=override
                )
        except (
            exceptions.InvalidVersioningOverrideError,
            exceptions.WorkflowAlreadyStartedError,
        ) as err:
            assert type(err).__name__ == error_name
            assert isinstance(err, exceptions.FailureError)
            if isinstance(err, exceptions.WorkflowAlreadyStartedError):
                assert err.workflow_id == "child-id"
                assert err.workflow_type == "ChildWorkflow"
            return type(err).__name__
        raise AssertionError("Child start should have failed")


def _create_instance(workflow_class: type) -> temporalio.worker.WorkflowInstance:
    defn = workflow._Definition.must_from_class(workflow_class)
    start_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return temporalio.worker.UnsandboxedWorkflowRunner().create_instance(
        temporalio.worker.WorkflowInstanceDetails(
            payload_converter_factory=converter.DefaultPayloadConverter,
            failure_converter_class=converter.DefaultFailureConverter,
            interceptor_classes=[],
            defn=defn,
            info=workflow.Info(
                attempt=1,
                continued_run_id=None,
                cron_schedule=None,
                execution_timeout=None,
                first_execution_run_id="parent-run",
                headers={},
                namespace="test-namespace",
                original_execution_run_id="parent-run",
                parent=None,
                root=None,
                priority=common.Priority(),
                raw_memo={},
                retry_policy=None,
                run_id="parent-run",
                run_timeout=None,
                search_attributes={},
                start_time=start_time,
                task_queue="test-task-queue",
                task_timeout=timedelta(seconds=10),
                typed_search_attributes=common.TypedSearchAttributes.empty,
                workflow_id="parent-id",
                workflow_start_time=start_time,
                workflow_type=defn.name or "",
            ),
            randomness_seed=1,
            extern_functions={},
            disable_eager_activity_execution=False,
            worker_level_failure_exception_types=[],
            patch_activation_callback=None,
            last_completion_result=temporalio.api.common.v1.Payloads(),
            last_failure=None,
        )
    )


@pytest.mark.parametrize("execute", [False, True], ids=["start", "execute"])
@pytest.mark.parametrize(
    "override,expected_override",
    [
        pytest.param(None, None, id="absent"),
        pytest.param(
            common.PinnedVersioningOverride(
                common.WorkerDeploymentVersion("deployment", "build")
            ),
            temporalio.api.workflow.v1.VersioningOverride(
                behavior=temporalio.api.enums.v1.VersioningBehavior.VERSIONING_BEHAVIOR_PINNED,
                pinned_version="deployment.build",
                pinned=temporalio.api.workflow.v1.VersioningOverride.PinnedOverride(
                    version=temporalio.api.deployment.v1.WorkerDeploymentVersion(
                        deployment_name="deployment", build_id="build"
                    ),
                    behavior=temporalio.api.workflow.v1.VersioningOverride.PinnedOverrideBehavior.PINNED_OVERRIDE_BEHAVIOR_PINNED,
                ),
            ),
            id="pinned",
        ),
        pytest.param(
            common.AutoUpgradeVersioningOverride(),
            temporalio.api.workflow.v1.VersioningOverride(
                behavior=temporalio.api.enums.v1.VersioningBehavior.VERSIONING_BEHAVIOR_AUTO_UPGRADE,
                auto_upgrade=True,
            ),
            id="auto-upgrade",
        ),
        pytest.param(
            common.OneTimeVersioningOverride(
                common.WorkerDeploymentVersion("deployment", "build")
            ),
            temporalio.api.workflow.v1.VersioningOverride(
                one_time=temporalio.api.workflow.v1.VersioningOverride.OneTimeOverride(
                    target_deployment_version=temporalio.api.deployment.v1.WorkerDeploymentVersion(
                        deployment_name="deployment", build_id="build"
                    )
                )
            ),
            id="one-time",
        ),
    ],
)
@pytest.mark.parametrize(
    "cause,error_type",
    [
        pytest.param(
            child_workflow.StartChildWorkflowExecutionFailedCause.START_CHILD_WORKFLOW_EXECUTION_FAILED_CAUSE_INVALID_VERSIONING_OVERRIDE,
            exceptions.InvalidVersioningOverrideError,
            id="invalid-versioning-override",
        ),
        pytest.param(
            child_workflow.StartChildWorkflowExecutionFailedCause.START_CHILD_WORKFLOW_EXECUTION_FAILED_CAUSE_WORKFLOW_ALREADY_EXISTS,
            exceptions.WorkflowAlreadyStartedError,
            id="already-started",
        ),
    ],
)
async def test_child_workflow_versioning(
    execute: bool,
    override: common.VersioningOverride | None,
    expected_override: temporalio.api.workflow.v1.VersioningOverride | None,
    cause: child_workflow.StartChildWorkflowExecutionFailedCause.ValueType,
    error_type: type[exceptions.FailureError],
) -> None:
    instance = _create_instance(ParentWorkflow)
    try:
        completion = instance.activate(
            workflow_activation.WorkflowActivation(
                run_id="parent-run",
                jobs=[
                    workflow_activation.WorkflowActivationJob(
                        initialize_workflow=workflow_activation.InitializeWorkflow(
                            workflow_type="ParentWorkflow",
                            workflow_id="parent-id",
                            randomness_seed=1,
                            arguments=converter.DefaultPayloadConverter().to_payloads(
                                [
                                    execute,
                                    type(override).__name__ if override else "absent",
                                    error_type.__name__,
                                ]
                            ),
                        )
                    )
                ],
            )
        )
        assert completion.HasField("successful")
        assert len(completion.successful.commands) == 1
        command = completion.successful.commands[0]
        assert command.HasField("start_child_workflow_execution")
        start = command.start_child_workflow_execution
        assert start.workflow_id == "child-id"
        assert start.workflow_type == "ChildWorkflow"
        if expected_override is None:
            assert not start.HasField("versioning_override")
        else:
            assert start.HasField("versioning_override")
            assert start.versioning_override == expected_override

        completion = instance.activate(
            workflow_activation.WorkflowActivation(
                run_id="parent-run",
                jobs=[
                    workflow_activation.WorkflowActivationJob(
                        resolve_child_workflow_execution_start=workflow_activation.ResolveChildWorkflowExecutionStart(
                            seq=start.seq,
                            failed=workflow_activation.ResolveChildWorkflowExecutionStartFailure(
                                workflow_id=start.workflow_id,
                                workflow_type=start.workflow_type,
                                cause=cause,
                            ),
                        )
                    )
                ],
            )
        )
        assert completion.HasField("successful")
        assert len(completion.successful.commands) == 1
        command = completion.successful.commands[0]
        assert command.HasField("complete_workflow_execution")
        assert converter.DefaultPayloadConverter().from_payloads(
            [command.complete_workflow_execution.result]
        ) == [error_type.__name__]
    finally:
        instance.activate(
            workflow_activation.WorkflowActivation(
                run_id="parent-run",
                jobs=[
                    workflow_activation.WorkflowActivationJob(
                        remove_from_cache=workflow_activation.RemoveFromCache()
                    )
                ],
            )
        )
