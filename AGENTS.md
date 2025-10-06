# AGENTS.md

Use these concise rules when automating changes in this repo.

## Build, Lint, Test
- Build (release): `uv build`
- Dev build (Rust ext): `poe build-develop` or `uv run maturin develop --uv`
- Lint fast: `uv run ruff check --select F401,F841 --quiet`
- Format: `poe format`
- Types: `poe lint-types` (pyright + mypy)
- Docs style: `poe lint-docs`
- All tests: `uv run pytest`
- Single test: `uv run pytest -s --log-cli-level=DEBUG -k <test_name>`
- Debug failures: add `--tb=full --full-stack-trace`
- Workflow env: default Temporalite; time-skipping `--workflow-environment time-skipping`; external `--workflow-environment host:port`

## Code Style and Conventions
- Imports/formatting: managed by ruff + `poe format`; keep imports sorted; no unused vars/imports.
- Types: add precise type hints; keep public APIs typed; fix pyright/mypy warnings.
- Naming: snake_case for functions/vars, PascalCase for classes, UPPER_SNAKE for constants; prefer explicit names over abbreviations.
- Errors: use `logger.exception` for unexpected failures; fail fast rather than add fallback paths; keep logs simple (no emojis).
- Determinism: workflows must avoid random, wall-clock, I/O, network; use `workflow.sleep()` not `asyncio.sleep()`; put nondeterminism in activities.
- Structure: separate workflows, activities, client code; follow existing patterns under `temporalio/`.
- Generated code: do not modify files in `temporalio/api/**` or `temporalio/bridge/proto/**`; regen with `poe gen-protos` (Python ≤3.10, protobuf 3.x).
- Tools: always prefer `uv run` instead of direct `python`.

## Cursor/Copilot/Claude
- No Cursor rules or Copilot instructions found.
- See `CLAUDE.md` for deeper agent guidance; mirror its practices here.
