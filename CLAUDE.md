# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository Overview

This is the Temporal Python SDK, a framework for building resilient distributed applications with durable execution. The SDK enables authoring workflows and activities using Python, providing type-safe, async-first APIs backed by Temporal's distributed orchestration engine.

## Architecture

The project consists of several key components:

- **temporalio/**: Main Python package
  - **api/**: Generated protobuf definitions for Temporal service APIs
  - **bridge/**: Rust-Python bridge layer wrapping sdk-core
    - **sdk-core/**: Core Temporal SDK implementation in Rust
  - **client.py**: Temporal client for starting workflows, queries, signals
  - **worker/**: Worker implementation for executing workflows and activities
  - **workflow.py**: Workflow definition and execution framework
  - **activity.py**: Activity definition and execution framework
  - **testing/**: Testing utilities with time-skipping capabilities
  - **contrib/**: Integrations (OpenAI Agents, OpenTelemetry, Pydantic)

The SDK uses a hybrid Rust/Python architecture where:
- Core workflow state management and server communication is handled in Rust (temporalio/bridge/sdk-core/)
- Python provides the developer-facing APIs and workflow/activity execution environment
- Communication between layers happens via PyO3 bindings

## Common Development Commands

### Testing
```bash
# Run all tests
poe test

# Run specific test with debug output
poe test -s --log-cli-level=DEBUG -k test_name

# Run against time-skipping test server
poe test --workflow-environment time-skipping

# Run single test file
poe test tests/test_client.py
```

### Building
```bash
# Development build (faster, for local development)
poe build-develop

# Release build (slower, for distribution)
uv build
```

### Code Quality
```bash
# Format code
poe format

# Lint code (includes type checking)
poe lint

# Just type checking
poe lint-types

# Just Rust linting
poe bridge-lint
```

### Protocol Buffer Generation
```bash
# Generate protobuf code (requires Docker)
poe gen-protos-docker

# Generate without Docker (Python <= 3.10 required)
poe gen-protos
```

## Key Development Patterns

### Workflow Development
- Workflows must be deterministic - no I/O, threading, or randomness
- Use `workflow.execute_activity()` for external calls
- Import activities with `workflow.unsafe.imports_passed_through()` for sandbox compatibility
- Use async/await patterns with `workflow.sleep()` for timers
- Signal and update handlers run concurrently with main workflow

### Activity Development
- Activities can be sync or async functions
- For sync activities, set `activity_executor` on Worker
- Use `activity.heartbeat()` for long-running activities
- Handle cancellation via `activity.is_cancelled()` or exception catching

### Testing Workflows
- Use `temporalio.testing.WorkflowEnvironment.start_time_skipping()` for deterministic time control
- Mock activities by providing different implementations to test worker
- Use `env.sleep()` to advance time in tests

### Bridge/Rust Development
- Rust code lives in `temporalio/bridge/sdk-core/`
- Use `poe build-develop` after Rust changes
- Rust implements state machines, networking, and core orchestration logic