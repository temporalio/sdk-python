from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

import temporalio.bridge.worker
import temporalio.client
import temporalio.common
import temporalio.converter
from temporalio.worker import (
    ExperimentalWorkerAffinityOptions,
    SessionWorkerOptions,
    Worker,
)


@pytest.mark.parametrize(
    "session_options,affinity_options",
    [
        (None, None),
        (SessionWorkerOptions(), None),
        (
            None,
            ExperimentalWorkerAffinityOptions(sticky_queue_name="stable-worker"),
        ),
        (
            SessionWorkerOptions(),
            ExperimentalWorkerAffinityOptions(
                sticky_queue_name="stable-worker", single_workflow_mode=True
            ),
        ),
    ],
)
async def test_worker_affinity_config_forwarding(
    session_options: SessionWorkerOptions | None,
    affinity_options: ExperimentalWorkerAffinityOptions | None,
) -> None:
    client = Mock(spec=temporalio.client.Client)
    client.namespace = "default"
    client.config.return_value = {
        "plugins": [],
        "interceptors": [],
        "data_converter": temporalio.converter.DataConverter.default,
        "header_codec_behavior": temporalio.common.HeaderCodecBehavior.NO_CODEC,
    }
    with (
        patch("temporalio.worker._worker._extract_bridge_client_for_worker"),
        patch("temporalio.worker._worker._ActivityWorker"),
        patch("temporalio.worker._worker._WorkflowWorker"),
        patch.object(temporalio.bridge.worker.Worker, "create") as create,
    ):
        worker = Worker(
            client,
            task_queue="affinity-config",
            build_id="test-build",
            activities=[Mock()],
            workflows=[Mock()],
            session_worker_options=session_options,
            experimental_worker_affinity_options=affinity_options,
        )

    assert worker.config().get("session_worker_options") is session_options
    assert (
        worker.config().get("experimental_worker_affinity_options") is affinity_options
    )
    bridge_config = create.call_args.args[1]
    if session_options is None:
        assert bridge_config.session_worker_options is None
    else:
        assert isinstance(
            bridge_config.session_worker_options,
            temporalio.bridge.worker.SessionWorkerOptions,
        )
    if affinity_options is None:
        assert bridge_config.experimental_worker_affinity_options is None
    else:
        assert (
            bridge_config.experimental_worker_affinity_options
            == temporalio.bridge.worker.ExperimentalWorkerAffinityOptions(
                sticky_queue_name=affinity_options.sticky_queue_name,
                single_workflow_mode=affinity_options.single_workflow_mode,
            )
        )
