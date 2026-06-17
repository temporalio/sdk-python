<!--
High-level release notes.
Loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

When your PR includes a user-facing change, add an entry below under the
appropriate heading (create the heading if it does not yet exist). Within
each heading content can be free-form. Feel free to include examples, links
to docs, or any other relevant information.

### Added            — new features
### Changed          — changes in existing functionality
### Deprecated       — soon-to-be-removed features
### Breaking Changes — removed or backwards-incompatible features
### Fixed            — notable bug fixes
### Security         — notable security fixes
-->

# Changelog

## [Unreleased]

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
