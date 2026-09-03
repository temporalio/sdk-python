<!--
High-level release notes.
Loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

When your PR includes a user-facing change, add an entry below under the
appropriate heading. Within each heading content can be free-form. Feel free
to include examples, links to docs, or any other relevant information.

### Added            — new features
### Changed          — changes in existing functionality
### Deprecated       — soon-to-be-removed features
### :boom: Breaking Changes — removed or backwards-incompatible features
### Fixed            — notable bug fixes
### Security         — notable security fixes
-->

# Changelog

## [Unreleased]

### Added

### Changed

- System Nexus Signal-with-Start Workflow operations now use the typed
  `WorkflowOutboundInterceptor.start_signal_with_start_workflow` interception point instead of
  the generic `WorkflowOutboundInterceptor.start_nexus_operation` method.

### Deprecated

### :boom: Breaking Changes

- Experimental external storage: `ExternalStorage.driver_selector` is now called with a
  `StorageDriverSelectContext` instead of a `StorageDriverStoreContext`. Update the annotation;
  the new type carries the same `target` field. Since selectors are plain callables, a stale
  annotation fails type checking rather than at runtime.

### Fixed

- Experimental Workflow Streams background publishing now retries transient
  signal delivery failures without delaying payload conversion errors.
- `StrandsPlugin` now disables Botocore retries for its default Bedrock model so
  model request retries are handled exclusively by Temporal.
- `temporalio.contrib.openai_agents` now honors the `retry-after-ms` and
  `retry-after` headers when OpenAI returns `x-should-retry: true`. Previously
  the delay the server asked for was discarded on that path and the activity
  retried on its configured interval instead.
- Nexus-context workflow/activity starts no longer set `on_conflict_options` when there are no links
  or callbacks to attach.

### Security

## [1.32.0] - 2026-08-24

### Added

- Added `temporalio.converter.create_payload_validation_error` to create the
  non-retryable application error used when a converted payload fails validation.
- Added experimental `temporalio.contrib.opentelemetry.ReplaySafeMeterProvider` and
  `ReplaySafeLoggerProvider` (and exported `ReplaySafeTracerProvider`): wrap an
  OpenTelemetry provider so metrics and log events recorded from workflow code (e.g. by
  Google ADK) are not duplicated on replay. `GoogleAdkPlugin` warns when a global OTel
  provider is not replay-safe.
- Added `LoggingConfig.format` to select compact, pretty, or newline-delimited JSON output for
  Core logs written to the console.

- Added the `Runtime(disable_environment_info=...)` option to control whether
  runtime, hosting, and platform information is included in worker heartbeats.

- `temporalio.workflow.uuid7()` generates a determinism-safe, time-sortable
  UUIDv7 (RFC 9562) from workflow time and the workflow's deterministic random
  generator, complementing the existing `workflow.uuid4()`
  ([#1450](https://github.com/temporalio/sdk-python/issues/1450)). The
  workflow sandbox now also restricts the non-deterministic `uuid.uuid7()`
  added to the standard library in Python 3.14, matching the existing
  `uuid.uuid1()`/`uuid.uuid4()` restrictions.
- **Experimental**: `TemporalOperationHandler` can now use Standalone Activities as asynchronous
  Nexus Operation backing executions through `TemporalNexusClient.start_activity`.
- **Experimental**: `temporalio.contrib.openai_agents.temporal_worker_env_ref` names an environment
  variable the worker reads for a hosted tool credential, keeping it out of workflow history.
- **Experimental**: `temporalio.contrib.openai_agents.TemporalWorkerEnvValue` names an environment
  variable the worker reads for a sandbox environment value, keeping it out of workflow history.
- **Experimental**: `OpenAIAgentsPlugin(resolvable_worker_env_vars=...)` allowlists the environment
  variable names a worker will read.
- **Experimental**: `temporalio.contrib.openai_agents.AllowAllWorkerEnvVars` allowlists every
  environment variable name on the worker.
- Added Nexus operation link propagation for Workflow Queries issued from operation handlers. The
  queried Workflow link returned by the server is attached to the caller's Nexus operation event.

### Changed

- The `opentelemetry` and `lambda-worker-otel` extras now require
  `opentelemetry-api`/`opentelemetry-sdk` `>= 1.26`, matching what
  `temporalio.contrib.opentelemetry` already required in practice.
- `temporalio.contrib.pydantic` converters now reuse Pydantic type adapters
  for repeated type hints instead of rebuilding their schemas for every
  payload, greatly speeding up decode of non-model hints such as discriminated
  unions ([#1695](https://github.com/temporalio/sdk-python/issues/1695)). Up
  to 1024 type adapters are cached per converter instance by default, with
  least-recently-used eviction. To change the bound, pass
  ``max_cached_type_adapters`` to ``PydanticPayloadConverter`` (or
  ``PydanticJSONPlainPayloadConverter``) from a nullary subclass used as the
  ``DataConverter.payload_converter_class``; ``None`` makes the cache
  unbounded and zero disables caching.
- A data converter can now report that it understood a Nexus operation's input
  but considers it invalid by raising a non-retryable `ApplicationError` of type
  `PayloadValidationError` while decoding it. Such a failure is reported to the
  caller as a `BAD_REQUEST` Nexus handler error with the message
  `Invalid operation input`, retaining the original error as its cause. Raised
  from a payload codec, that replaces a handler-side `INTERNAL` error; raised
  from a payload converter, the type was already `BAD_REQUEST` and only the
  message becomes specific to validation. Any other decode failure, and a
  retryable `PayloadValidationError`, keep their existing treatment.

### :boom: Breaking Changes

- The `openai-agents` extra now requires `openai-agents>=0.19.2,<0.20`, up from `>=0.17.5` with no
  upper bound.
- `temporalio.contrib.openai_agents` now rejects a sandbox `SandboxPathGrant` bound to a
  `host_path`.
- `temporalio.contrib.openai_agents` now rejects `run_config.sandbox.session`.

### Fixed

- `create_payload_validation_error(None)` now creates an application error with no
  details instead of encoding `None` as a detail.
- Client header encoding no longer mutates interceptor-provided payloads, preventing
  update-with-start from encoding a shared header twice when
  `HeaderCodecBehavior.CODEC` is enabled ([#1769](https://github.com/temporalio/sdk-python/issues/1769)).
- `temporalio.contrib.opentelemetry` replay-safe spans now delegate
  `Span.add_link` to the wrapped span. Previously the wrapper inherited
  OpenTelemetry's non-abstract no-op default, silently dropping links added
  after span creation.
- Standalone activity start requests now include a unique request ID so RPC retries are deduplicated.
- OpenTelemetry trace and span IDs propagated by concurrent workers no longer
  interfere with each other, preserving the correct parent-child hierarchy.
- The `google-adk` extra now depends on `mcp`, so fresh installs of
  `temporalio[google-adk]` can import `temporalio.contrib.google_adk_agents`
  without separately installing `mcp`. Previously the import failed with an
  `ImportError` because `google.adk.tools.mcp_tool` only exports `McpToolset`
  when `mcp` is installed.
- `temporalio.contrib.openai_agents` no longer crashes when a plain `dict`
  is passed for `run_config`. (openai-agents >= 0.19.0 accepts `dict` run
  configs at its public runner API)

## [1.31.0] - 2026-07-29

### Added

- Added the `Worker` `max_eager_activity_reservations_per_workflow_task` option for configuring
  the number of activity slots reserved for eager execution per workflow task. Configured values
  must be positive; use `disable_eager_activity_execution` to disable eager activity execution.
- Added experimental SDK payload converter support for values and type hints
  decorated with `@transfer_type_convertible(...)` using a `TransferTypeConverter` class.
  This lets types with transfer type converters delegate their wire representation to the
  configured payload converter, preserving SDK behavior such as serialization
  contexts.
- Added `TLSConfig.verification_server_name` to verify the server certificate against a fixed name
  instead of the connection's server name. Unlike `domain`, it does not change the TLS SNI or
  HTTP/2 authority values, which keep following the connected host, so it can be used when the
  server's certificate does not carry the dialed name but on-path infrastructure (e.g. an
  SNI-inspecting egress proxy) needs the SNI to remain resolvable. Requires
  `server_root_ca_cert`.

- Added the experimental `Worker` `patch_activation_callback` option, allowing workers
  to decide whether a first non-replay `workflow.patched` call should activate a patch
  during rolling deployments.
- Added external storage support to Nexus task handling.

### Changed

- Prepared replay-safe workflow activation scheduling that prevents cancellation
  from being lost when another event becomes ready in the same workflow task. The
  behavior is guarded by internal workflow logic flag 2 and remains disabled by
  default during its compatibility rollout.
  **Maintainer reminder:** keep flag 2 default-disabled for the first two published
  SDK releases that recognize it; enable it in the third release, remove the explicit
  overrides for this flag from `tests/worker/test_workflow.py`, and replace this rollout
  note with a `Fixed` entry announcing the behavior change.

### :boom: Breaking Changes

- Custom workflow runners that construct `WorkflowInstanceDetails` must now pass
  `payload_converter_factory` instead of `payload_converter_class`. The factory
  returns the already wrapped payload converter that workflow instances should
  use.
- System Nexus payload converter helpers added for generated bindings are now
  private implementation details, and the remaining public `temporalio.nexus.system`
  APIs are marked experimental and subject to change.
- Payload size limits have moved from `DataConverter` to `Client.connect`. Pass
  `payload_limits=PayloadLimitsConfig(...)` (now exported from
  `temporalio.client`) instead of setting `payload_limits` on `DataConverter`.
  Config fields were renamed to `payloads_warn_size` and `memo_warn_size`, and
  the deprecated `PayloadSizeWarning` was removed.

### Fixed

- Continue-as-new requests from workflow update handlers now fail the workflow
  task instead of leaving the update unresolved.
- Marked system Nexus envelope payloads so nested payloads can be detected and
  visited after the envelope is already stored as a payload.

## [1.30.0] - 2026-07-01

### Added

- Nexus operation link propagation for signals. When a Nexus operation handler signals a workflow
  (including signal-with-start), the inbound Nexus request links are now forwarded onto the signaled
  workflow so its history events link back to the caller, and the link the server returns for the
  signaled event is attached to the caller workflow's Nexus operation history event. This makes the
  caller and callee mutually navigable in the UI for signal-based Nexus operations.
- Exposed `backoff_start_interval` for continue-as-new, to allow the new workflow to start after a delay.

### Changed

- AWS Lambda worker `configure` parameter supports sync, async, and async
  generator style functions. This callback is invoked on the asyncio event
  loop.
- Relaxed the protobuf dependency bounds to allow protobuf 7 where compatible
  with the selected optional dependencies.
- Standalone Nexus operation links are now forwarded on start workflow and signal requests.

### :boom: Breaking Changes

- AWS Lambda worker `configure` parameter has been changed to be invoked
  per-invocation of the worker instead of only at startup. It is advised that
  any shared, heavy-weight operations are performed outside of the callback
  before `run_worker` is invoked.

## [1.29.0] - 2026-06-17

### Added

- Added experimental `temporalio.workflow.signal_with_start_workflow`, backed by
  generated system Nexus bindings for
  `WorkflowService.SignalWithStartWorkflowExecution`.
- Added OpenAI Agents plugin support for `CustomTool` dispatch, including lazy
  tool discovery through `defer_loading`.

### Changed

- Client connections now use gzip transport-level gRPC compression by default.
  Pass `grpc_compression=GrpcCompression.NONE` to `Client.connect` or
  `CloudOperationsClient.connect` to disable it.

### :boom: Breaking Changes

- `StartWorkflowUpdateWithStartInput` now owns the authoritative
  `rpc_metadata` and `rpc_timeout` fields for
  `OutboundInterceptor.start_update_with_start_workflow`. These fields were
  removed from the nested update-with-start input objects, so custom
  interceptors that accessed them there should read or update the top-level
  fields instead.

### Fixed

- Fixed `breakpoint()` and `pdb.set_trace()` inside workflow code when a worker
  runs with `debug_mode=True` or `TEMPORAL_DEBUG=1`; sandboxed workflows without
  debug mode now get a clearer error pointing to `debug_mode=True`.
- Fixed `start_update_with_start_workflow` interceptor handling so RPC metadata
  and timeouts are forwarded to the underlying `execute_multi_operation` call.
- Fixed OpenAI Agents plugin streamed event serialization when pydantic had not
  yet built deferred schemas, and fixed terminal sandbox errors retrying
  forever.
- Removed the lazy-connect lock from the per-RPC hot path. It was previously
  acquired on every RPC, putting an event-loop-bound primitive on the hot path;
  it is now skipped once the client is connected. This reduces the client's
  coupling to the event loop it connected on, which can help when reusing a
  single long-lived `Client` across event loops or threads (e.g. the
  dedicated-loop pattern used with gevent/gunicorn and synchronous services).
  Note this does not make a `Client` fully thread- or loop-agnostic; reusing one
  long-lived loop is still the recommended pattern.
