from typing import MutableSequence

from google.protobuf.duration_pb2 import Duration

import temporalio.bridge.worker
from temporalio.api.common.v1.message_pb2 import (
    Payload,
    Payloads,
    Priority,
    SearchAttributes,
)
from temporalio.api.sdk.v1.user_metadata_pb2 import UserMetadata
from temporalio.bridge._visitor import PayloadVisitor, VisitorFunctions
from temporalio.bridge.proto.workflow_activation.workflow_activation_pb2 import (
    InitializeWorkflow,
    WorkflowActivation,
    WorkflowActivationJob,
)
from temporalio.bridge.proto.workflow_commands.workflow_commands_pb2 import (
    ContinueAsNewWorkflowExecution,
    ScheduleActivity,
    ScheduleLocalActivity,
    SignalExternalWorkflowExecution,
    StartChildWorkflowExecution,
    UpdateResponse,
    WorkflowCommand,
)
from temporalio.bridge.proto.workflow_completion.workflow_completion_pb2 import (
    Success,
    WorkflowActivationCompletion,
)
from tests.worker.test_workflow import SimpleCodec


class Visitor(VisitorFunctions):
    async def visit_payload(self, payload: Payload) -> None:
        payload.metadata["visited"] = b"True"

    async def visit_payloads(self, payloads: MutableSequence[Payload]) -> None:
        for payload in payloads:
            payload.metadata["visited"] = b"True"


async def test_workflow_activation_completion():
    comp = WorkflowActivationCompletion(
        run_id="1",
        successful=Success(
            commands=[
                WorkflowCommand(
                    schedule_activity=ScheduleActivity(
                        seq=1,
                        activity_id="1",
                        activity_type="",
                        task_queue="",
                        headers={"foo": Payload(data=b"bar")},
                        arguments=[Payload(data=b"baz")],
                        schedule_to_close_timeout=Duration(seconds=5),
                        priority=Priority(),
                    ),
                    user_metadata=UserMetadata(summary=Payload(data=b"Summary")),
                )
            ],
        ),
    )

    await PayloadVisitor().visit(Visitor(), comp)

    cmd = comp.successful.commands[0]
    sa = cmd.schedule_activity
    assert sa.headers["foo"].metadata["visited"]
    assert len(sa.arguments) == 1 and sa.arguments[0].metadata["visited"]

    assert cmd.user_metadata.summary.metadata["visited"]


async def test_workflow_activation():
    original = WorkflowActivation(
        jobs=[
            WorkflowActivationJob(
                initialize_workflow=InitializeWorkflow(
                    arguments=[
                        Payload(data=b"repeated1"),
                        Payload(data=b"repeated2"),
                    ],
                    headers={"header": Payload(data=b"map")},
                    last_completion_result=Payloads(
                        payloads=[
                            Payload(data=b"obj1"),
                            Payload(data=b"obj2"),
                        ]
                    ),
                    search_attributes=SearchAttributes(
                        indexed_fields={
                            "sakey": Payload(data=b"saobj"),
                        }
                    ),
                ),
            )
        ]
    )

    async def visitor(payload: Payload) -> Payload:
        # Mark visited by prefixing data
        new_payload = Payload()
        new_payload.metadata.update(payload.metadata)
        new_payload.metadata["visited"] = b"True"
        new_payload.data = payload.data
        return new_payload

    act = original.__deepcopy__()
    await PayloadVisitor().visit(Visitor(), act)
    assert act.jobs[0].initialize_workflow.arguments[0].metadata["visited"]
    assert act.jobs[0].initialize_workflow.arguments[1].metadata["visited"]
    assert act.jobs[0].initialize_workflow.headers["header"].metadata["visited"]
    assert (
        act.jobs[0]
        .initialize_workflow.last_completion_result.payloads[0]
        .metadata["visited"]
    )
    assert (
        act.jobs[0]
        .initialize_workflow.last_completion_result.payloads[1]
        .metadata["visited"]
    )
    assert (
        act.jobs[0]
        .initialize_workflow.search_attributes.indexed_fields["sakey"]
        .metadata["visited"]
    )

    act = original.__deepcopy__()
    await PayloadVisitor(skip_search_attributes=True).visit(Visitor(), act)
    assert (
        not act.jobs[0]
        .initialize_workflow.search_attributes.indexed_fields["sakey"]
        .metadata["visited"]
    )

    act = original.__deepcopy__()
    await PayloadVisitor(skip_headers=True).visit(Visitor(), act)
    assert not act.jobs[0].initialize_workflow.headers["header"].metadata["visited"]


async def test_visit_payloads_on_other_commands():
    comp = WorkflowActivationCompletion(
        run_id="2",
        successful=Success(
            commands=[
                # Continue as new
                WorkflowCommand(
                    continue_as_new_workflow_execution=ContinueAsNewWorkflowExecution(
                        arguments=[Payload(data=b"a1")],
                        headers={"h1": Payload(data=b"a2")},
                        memo={"m1": Payload(data=b"a3")},
                    )
                ),
                # Start child
                WorkflowCommand(
                    start_child_workflow_execution=StartChildWorkflowExecution(
                        input=[Payload(data=b"b1")],
                        headers={"h2": Payload(data=b"b2")},
                        memo={"m2": Payload(data=b"b3")},
                    )
                ),
                # Signal external
                WorkflowCommand(
                    signal_external_workflow_execution=SignalExternalWorkflowExecution(
                        args=[Payload(data=b"c1")],
                        headers={"h3": Payload(data=b"c2")},
                    )
                ),
                # Schedule local activity
                WorkflowCommand(
                    schedule_local_activity=ScheduleLocalActivity(
                        arguments=[Payload(data=b"d1")],
                        headers={"h4": Payload(data=b"d2")},
                    )
                ),
                # Update response completed
                WorkflowCommand(
                    update_response=UpdateResponse(
                        completed=Payload(data=b"e1"),
                    )
                ),
            ]
        ),
    )

    await PayloadVisitor().visit(Visitor(), comp)

    cmds = comp.successful.commands
    can = cmds[0].continue_as_new_workflow_execution
    assert can.arguments[0].metadata["visited"]
    assert can.headers["h1"].metadata["visited"]
    assert can.memo["m1"].metadata["visited"]

    sc = cmds[1].start_child_workflow_execution
    assert sc.input[0].metadata["visited"]
    assert sc.headers["h2"].metadata["visited"]
    assert sc.memo["m2"].metadata["visited"]

    se = cmds[2].signal_external_workflow_execution
    assert se.args[0].metadata["visited"]
    assert se.headers["h3"].metadata["visited"]

    sla = cmds[3].schedule_local_activity
    assert sla.arguments[0].metadata["visited"]
    assert sla.headers["h4"].metadata["visited"]

    ur = cmds[4].update_response
    assert ur.completed.metadata["visited"]


async def test_bridge_encoding():
    comp = WorkflowActivationCompletion(
        run_id="1",
        successful=Success(
            commands=[
                WorkflowCommand(
                    schedule_activity=ScheduleActivity(
                        seq=1,
                        activity_id="1",
                        activity_type="",
                        task_queue="",
                        headers={"foo": Payload(data=b"bar")},
                        arguments=[
                            Payload(data=b"repeated1"),
                            Payload(data=b"repeated2"),
                        ],
                        schedule_to_close_timeout=Duration(seconds=5),
                        priority=Priority(),
                    ),
                    user_metadata=UserMetadata(summary=Payload(data=b"Summary")),
                )
            ],
        ),
    )

    await temporalio.bridge.worker.encode_completion(comp, SimpleCodec(), True)

    cmd = comp.successful.commands[0]
    sa = cmd.schedule_activity
    assert sa.headers["foo"].metadata["simple-codec"]
    assert len(sa.arguments) == 1
    assert sa.arguments[0].metadata["simple-codec"]

    assert cmd.user_metadata.summary.metadata["simple-codec"]
