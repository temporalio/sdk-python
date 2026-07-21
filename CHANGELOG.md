<!--
High-level release notes.
Loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

When your PR includes a user-facing change, add an entry below under the
appropriate heading. Within each heading content can be free-form. Feel free
to include examples, links to docs, or any other relevant information.

### Added            — new features
### Changed          — changes in existing functionality
### Deprecated       — soon-to-be-removed features
### Breaking Changes — removed or backwards-incompatible features
### Fixed            — notable bug fixes
### Security         — notable security fixes
-->

# Changelog

## [Unreleased]

### Added

- Added the experimental `Worker` `patch_activation_callback` option, allowing workers
  to decide whether a first non-replay `workflow.patched` call should activate a patch
  during rolling deployments.

### Changed

### Deprecated

### Breaking Changes

### Fixed

### Security

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

### Breaking Changes

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

### Breaking Changes

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
