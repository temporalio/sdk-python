from collections.abc import Sequence

import temporalio.api.common.v1
import temporalio.api.enums.v1
import temporalio.api.history.v1
from temporalio.client import WorkflowHistory


def make_nexus_endpoint_name(task_queue: str) -> str:
    # Create endpoints for different task queues without name collisions.
    return f"nexus-endpoint-{task_queue}"


def events_of_type(
    history: WorkflowHistory,
    event_type: temporalio.api.enums.v1.EventType.ValueType,
) -> list[temporalio.api.history.v1.HistoryEvent]:
    return [event for event in history.events if event.event_type == event_type]


def links_from_workflow_execution_started_event(
    event: temporalio.api.history.v1.HistoryEvent,
) -> list[temporalio.api.common.v1.Link]:
    callback_links = [
        link
        for callback in event.workflow_execution_started_event_attributes.completion_callbacks
        for link in callback.links
    ]
    if callback_links:
        return list(callback_links)
    return list(event.links)


def workflow_event_link_event_type(
    workflow_event: temporalio.api.common.v1.Link.WorkflowEvent,
) -> temporalio.api.enums.v1.EventType.ValueType:
    if workflow_event.HasField("request_id_ref"):
        return workflow_event.request_id_ref.event_type
    return workflow_event.event_ref.event_type


def expected_nexus_operation_link(
    *,
    namespace: str,
    operation_id: str,
    run_id: str,
) -> temporalio.api.common.v1.Link:
    return temporalio.api.common.v1.Link(
        nexus_operation=temporalio.api.common.v1.Link.NexusOperation(
            namespace=namespace,
            operation_id=operation_id,
            run_id=run_id,
        )
    )


def expected_workflow_event_link(
    *,
    namespace: str,
    workflow_id: str,
    run_id: str,
    event_type: temporalio.api.enums.v1.EventType.ValueType,
    event_id: int = 0,
    request_id: str | None = None,
) -> temporalio.api.common.v1.Link:
    if request_id is not None:
        return temporalio.api.common.v1.Link(
            workflow_event=temporalio.api.common.v1.Link.WorkflowEvent(
                namespace=namespace,
                workflow_id=workflow_id,
                run_id=run_id,
                request_id_ref=temporalio.api.common.v1.Link.WorkflowEvent.RequestIdReference(
                    request_id=request_id,
                    event_type=event_type,
                ),
            )
        )

    return temporalio.api.common.v1.Link(
        workflow_event=temporalio.api.common.v1.Link.WorkflowEvent(
            namespace=namespace,
            workflow_id=workflow_id,
            run_id=run_id,
            event_ref=temporalio.api.common.v1.Link.WorkflowEvent.EventReference(
                event_id=event_id,
                event_type=event_type,
            ),
        )
    )


def assert_links_match(
    links: Sequence[temporalio.api.common.v1.Link],
    *expected_links: temporalio.api.common.v1.Link,
) -> None:
    actual = sorted(list(links), key=_link_sort_key)
    expected = sorted(list(expected_links), key=_link_sort_key)
    assert actual == expected


def _link_sort_key(link: temporalio.api.common.v1.Link) -> bytes:
    return link.SerializeToString(deterministic=True)
