"""One pipeline exercising the whole payload-handle feature.

The per-mechanism tests each isolate one behavior. This one runs a single
workflow that uses all of them together, and asserts the resulting ledger of
external-storage operations -- because the counts *are* the feature: each large
payload is uploaded once, moves through the workflow, a child workflow, and
several activities by reference, and is downloaded only at the boundaries that
actually read it. No workflow ever downloads anything.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.api.common.v1 import Payload
from temporalio.common import ValueHandle
from temporalio.converter._extstore import (
    StorageDriverClaim,
    StorageDriverRetrieveContext,
)
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer
from tests.helpers import new_worker
from tests.test_extstore import InMemoryTestDriver
from tests.worker.test_payload_handle import _BIG, _client, _data_converter

_STTC = timedelta(seconds=30)
_SECTION = "section\n"


@dataclass
class PipelineResult:
    size: int
    summary_sections: int
    published: str
    archived: str


@activity.defn
async def measure_document(document: str) -> int:
    """Handle-unaware consumer: declares the value type and gets the value."""
    return len(document)


@activity.defn
async def render_report() -> str:
    """Handle-unaware producer: returns a large value, offloaded on completion."""
    return _BIG + "-report"


@activity.defn
async def summarize(document: ValueHandle[str]) -> ValueHandle[str]:
    """Handle-aware: reads its input on demand, and produces a tagged handle."""
    text = await document.get_value()
    # Large enough to be offloaded, so the deferred store is real, and tagged so
    # a consumer can route on it without downloading.
    summary = _SECTION * len(text)
    return await activity.create_value_handle(
        summary, metadata={"sections": str(len(text))}
    )


@activity.defn
async def publish(summary: ValueHandle[str]) -> str:
    """Reads a handle another activity produced."""
    return f"published-{len(await summary.get_value())}"


@activity.defn
async def archive(document: ValueHandle[str]) -> str:
    """Terminal consumer: materializes at the last hop."""
    return f"archived-{len(await document.get_value())}"


@workflow.defn
class ArchiveChild:
    @workflow.run
    async def run(self, report: ValueHandle[str]) -> str:
        # A child workflow that only routes: it never sees the bytes.
        return await workflow.execute_activity(
            archive, report, start_to_close_timeout=_STTC
        )


@workflow.defn
class DocumentPipeline:
    # The declaration is an immutable value, so it can be named once and reused.
    RENDER_AS_HANDLE = workflow.as_value_handle(render_report)

    @workflow.run
    async def run(self, document: ValueHandle[str]) -> PipelineResult:
        # 1. The caller sent a real value; this workflow consumes it as a handle,
        #    so the payload is never downloaded into the workflow.
        # 2. Forward it to an activity that knows nothing about handles: the
        #    reference resolves at that boundary, where I/O is allowed.
        size = await workflow.execute_activity(
            measure_document, document, start_to_close_timeout=_STTC
        )

        # 3. Consume an unchanged activity's large result as a handle: no
        #    download here either.
        report = await workflow.execute_activity(
            self.RENDER_AS_HANDLE, start_to_close_timeout=_STTC
        )

        # 4. An activity that produces a handle with metadata, deferring its
        #    upload until the activity result is committed.
        summary = await workflow.execute_activity(
            summarize, document, start_to_close_timeout=_STTC
        )

        # 5. Route on metadata: read descriptive data about the value without
        #    downloading the value itself.
        sections = int(summary.metadata["sections"])

        # 6. Forward that handle onward to an activity that does read it.
        published = await workflow.execute_activity(
            publish, summary, start_to_close_timeout=_STTC
        )

        # 7. Hand the large report to a child workflow that only routes it, and
        #    on to a final activity that actually reads it.
        archived = await workflow.execute_child_workflow(ArchiveChild.run, report)

        return PipelineResult(
            size=size,
            summary_sections=sections,
            published=published,
            archived=archived,
        )


class TrackingDriver(InMemoryTestDriver):
    """Records which stored payload each retrieval was for.

    The driver assigns keys in store order, so the keys name the payloads:
    payload-0 is the caller's document, payload-1 the rendered report, payload-2
    the summary the activity created.
    """

    def __init__(self) -> None:
        super().__init__()
        self.retrieved: Counter[str] = Counter()

    async def retrieve(
        self,
        context: StorageDriverRetrieveContext,
        claims: Sequence[StorageDriverClaim],
    ) -> list[Payload]:
        self.retrieved.update(claim.claim_data["key"] for claim in claims)
        return await super().retrieve(context, claims)


async def test_payload_handle_pipeline(env: WorkflowEnvironment) -> None:
    driver = TrackingDriver()
    client = await _client(env, driver)
    async with new_worker(
        client,
        DocumentPipeline,
        ArchiveChild,
        activities=[measure_document, render_report, summarize, publish, archive],
    ) as worker:
        handle = await client.start_workflow(
            DocumentPipeline.run,
            args=[_BIG],
            id=f"wf-{uuid.uuid4()}",
            task_queue=worker.task_queue,
        )
        result = await handle.result()

    summary_length = len(_SECTION) * len(_BIG)
    assert result == PipelineResult(
        size=len(_BIG),
        summary_sections=len(_BIG),
        published=f"published-{summary_length}",
        archived=f"archived-{len(_BIG) + len('-report')}",
    )

    # Three uploads: the caller's document, the report an unchanged activity
    # returned, and the summary an activity created (its store deferred until the
    # activity result was committed).
    assert driver._store_calls == 3

    # And this is the feature: three large payloads crossed two workflows and
    # five activities, and each was downloaded only where it was read.
    assert driver.retrieved == Counter(
        {
            # The document: read by measure_document (value-typed, handle-unaware)
            # and again by summarize (which acquires it on demand).
            "payload-0": 2,
            # The report: produced by an unchanged activity, consumed as a handle
            # at the call site, forwarded to a child workflow by reference, and
            # read only at the archive activity.
            "payload-1": 1,
            # The summary: created with metadata, routed on that metadata by the
            # workflow without a download, and read only at the publish activity.
            "payload-2": 1,
        }
    )

    # Nothing was downloaded inside a workflow, so replaying the whole history
    # downloads nothing at all: every payload a workflow touched, it touched by
    # reference.
    history = await handle.fetch_history()
    before = driver.retrieved.copy()
    await Replayer(
        workflows=[DocumentPipeline, ArchiveChild],
        data_converter=_data_converter(driver),
    ).replay_workflow(history, raise_on_replay_failure=True)
    assert driver.retrieved == before
