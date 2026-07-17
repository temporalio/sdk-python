# Temporal Google Cloud integration

`temporalio.contrib.gcp` provides an OpenTelemetry plugin with defaults for
Temporal Python SDK workers running on Google Cloud Run. Cloud Run worker pools
are the recommended deployment because Temporal workers are continuous,
pull-based background workloads.

> **Collector required by default:** The plugin exports metrics and traces to an
> OTLP collector at `http://localhost:4317`. It does not export directly to
> Google Cloud. Deploy the Google-Built OpenTelemetry Collector as a sidecar,
> configure another collector endpoint, or provide an application-owned runtime
> and tracer provider. Without a collector at the configured endpoint,
> telemetry is not delivered to Google Cloud.

This integration is for container-based Cloud Run workloads. It does not
implement a Cloud Run functions invocation lifecycle.

A Cloud Run service can also host a Temporal worker, but it must use
instance-based billing so CPU is available outside request handling, keep at
least one instance active through minimum instances or manual scaling, and run
an ingress container that listens on `PORT`. These are deployment requirements;
the plugin cannot configure them from inside the worker process.

## Installation

Install the SDK with the GCP OpenTelemetry dependencies:

```bash
python -m pip install 'temporalio[gcp-opentelemetry]'
```

## Usage

Create one plugin and install it when connecting the Temporal client. Client
plugins automatically propagate to workers created from that client.

```python
import asyncio
from datetime import timedelta

from temporalio.client import Client
from temporalio.contrib.gcp import OpenTelemetryPlugin
from temporalio.worker import Worker

otel_plugin = OpenTelemetryPlugin()
client = await Client.connect(
    "localhost:7233",
    plugins=[otel_plugin],
)
worker = Worker(
    client,
    task_queue="my-task-queue",
    workflows=[MyWorkflow],
    activities=[my_activity],
)

try:
    await worker.run()
finally:
    # Run this only after every worker using the plugin has stopped.
    await asyncio.to_thread(otel_plugin.shutdown, timedelta(seconds=2))
```

The plugin configures Temporal Core metrics through a
`temporalio.runtime.Runtime` and delegates tracing to
`temporalio.contrib.opentelemetry.OpenTelemetryPlugin`. Do not install both GCP
and generic OpenTelemetry plugins on the same client.

## Shutdown lifecycle

`Worker.shutdown()` waits for worker shutdown to complete. Stop every worker
using the plugin, then call `OpenTelemetryPlugin.shutdown()` with the time
remaining before Cloud Run sends `SIGKILL`. The call force-flushes Python traces
and shuts down a tracer provider created by the plugin. An application-owned
provider is force-flushed but remains the application's responsibility.

The Python SDK Core runtime exports metrics periodically and currently has no
explicit metrics-flush API. The default metric periodicity is 60 seconds,
matching the upstream OpenTelemetry SDK default. This avoids sending multiple
cumulative snapshots of the same Temporal series too frequently. Tune it with
`metric_periodicity` when the metrics backend semantics support a different
interval.

Do not use a collector batch processor on the Google Managed Service for
Prometheus metrics pipeline. A runtime shutdown can force a metrics export
immediately after a periodic export, regardless of the periodicity. If the
collector combines both cumulative snapshots into one write, Google Managed
Service for Prometheus can reject it with `Duplicate TimeSeries`. Send each
OTLP metrics export directly through the memory limiter, GCP resource detection,
and any required resource/metric transforms to the Google Managed Prometheus
exporter. Keep a separate batch processor on the traces pipeline; trace batching
does not have the cumulative time-series collision.

`flush_on_worker_stop=True` enables a best-effort trace flush after an
individual worker stops. It is disabled by default because one plugin can be
used by multiple workers and the remaining workers can emit telemetry after the
first worker stops.

The OTLP endpoint is resolved in this order:

1. `OpenTelemetryPlugin(endpoint=...)`.
2. `OTEL_EXPORTER_OTLP_ENDPOINT`.
3. `http://localhost:4317`.

The OpenTelemetry service name is resolved in this order:

1. `OpenTelemetryPlugin(service_name=...)`.
2. `OTEL_SERVICE_NAME`.
3. `CLOUD_RUN_WORKER_POOL` for a Cloud Run worker pool.
4. `K_SERVICE` for a Cloud Run service.
5. `temporal-worker`.

The plugin owns the runtime and replay-safe tracer provider it creates. To use
an application-owned runtime or provider, pass `runtime=` or `tracer_provider=`.
An application-owned provider must be created with
`temporalio.contrib.opentelemetry.create_tracer_provider`. Use
`build_metrics_telemetry_config()` to compose GCP metrics defaults with custom
runtime logging or other telemetry settings. Do not also pass a different
`runtime` to `Client.connect()`.

OpenTelemetry has one global tracer provider per process. Create the GCP plugin
before installing another provider, or pass the already-installed replay-safe
provider through `tracer_provider=`. The plugin raises instead of silently using
a different global provider.

The collector should use its GCP resource detector to add the Google Cloud
attributes it recognizes. Do not rely on the detector to infer Cloud Run
worker-pool-specific location or revision attributes; configure those explicitly
with a collector resource processor if they are required. This module does not
call the Google Cloud metadata server and adds no Google Cloud client libraries
or exporters to the worker process.

## Collector sidecar

Google publishes the Google-Built OpenTelemetry Collector as a container image.
Configure it as a second Cloud Run container, listen for OTLP gRPC on
`localhost:4317`, and use its GCP exporters for metrics and traces. Configure
separate pipelines: metrics without a batch processor, and traces with a
dedicated batch processor. For the image, collector configuration, IAM roles,
health check, and Secret Manager mount, see [Deploy Google-Built OpenTelemetry
Collector on Cloud Run](https://cloud.google.com/stackdriver/docs/instrumentation/opentelemetry-collector-cloud-run).
That guide demonstrates a Cloud Run service; adapt its collector container and
configuration when deploying a worker pool.

Cloud Run worker pools support sidecar containers over localhost and are
intended for continuous background work. Start the collector before the
Temporal worker and use the collector health extension as its startup probe.

To use an external collector instead, set `OTEL_EXPORTER_OTLP_ENDPOINT` or pass
`endpoint=` to the plugin.
