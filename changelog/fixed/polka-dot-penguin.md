- `temporalio.contrib.opentelemetry`: `TracingInterceptor` and `OpenTelemetryInterceptor` no longer
  log `Failed to detach context` when a context is torn down on a different thread while
  OpenTelemetry's threading instrumentation (enabled by strands, among others) is active; a
  context is now detached exactly when its token is still valid in the current
  `contextvars.Context`, which it stays when a workflow resumes on another pool thread.
