import socket
import uuid
from contextlib import closing
from urllib.request import urlopen

from temporalio import workflow
from temporalio.client import Client
from temporalio.runtime import PrometheusConfig, Runtime, TelemetryConfig
from temporalio.worker import Worker


def find_free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


@workflow.defn
class HelloWorkflow:
    @workflow.run
    async def run(self, name: str) -> str:
        return f"Hello, {name}!"


async def test_different_runtimes(client: Client):
    # Create two workers in separate runtimes and run workflows on them.
    # Confirm they each have different Prometheus addresses.
    prom_addr1 = f"127.0.0.1:{find_free_port()}"
    client1 = await Client.connect(
        client.service_client.config.target_host,
        namespace=client.namespace,
        runtime=Runtime(
            telemetry=TelemetryConfig(metrics=PrometheusConfig(bind_address=prom_addr1))
        ),
    )

    prom_addr2 = f"127.0.0.1:{find_free_port()}"
    client2 = await Client.connect(
        client.service_client.config.target_host,
        namespace=client.namespace,
        runtime=Runtime(
            telemetry=TelemetryConfig(metrics=PrometheusConfig(bind_address=prom_addr2))
        ),
    )

    async def run_workflow(client: Client):
        task_queue = f"task-queue-{uuid.uuid4()}"
        async with Worker(client, task_queue=task_queue, workflows=[HelloWorkflow]):
            assert "Hello, World!" == await client.execute_workflow(
                HelloWorkflow.run,
                "World",
                id=f"workflow-{uuid.uuid4()}",
                task_queue=task_queue,
            )

    await run_workflow(client1)
    await run_workflow(client2)

    # Get prom metrics on each
    with urlopen(url=f"http://{prom_addr1}/metrics") as f:
        assert "long_request" in f.read().decode("utf-8")
    with urlopen(url=f"http://{prom_addr2}/metrics") as f:
        assert "long_request" in f.read().decode("utf-8")
