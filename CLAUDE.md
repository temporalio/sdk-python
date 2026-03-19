# CLAUDE.md — Temporal Python SDK

## CI Pipeline

CI is defined in `.github/workflows/ci.yml`. The main jobs are:

### `build-lint-test` (matrix: Python 3.10/3.14 x multiple OS)
1. `poe build-develop` — builds the Rust bridge via maturin
2. `poe lint` — runs ALL of the following (defined in `pyproject.toml [tool.poe.tasks]`):
   - `uv run ruff check --select I` — import sorting
   - `uv run ruff format --check` — code formatting
   - `uv run pyright` — type checking (whole repo)
   - `uv run mypy --namespace-packages --check-untyped-defs .` — type checking (whole repo)
   - `uv run basedpyright` — stricter type checking (whole repo, catches more than pyright)
   - `uv run pydocstyle --ignore-decorators=overload` — docstring style
3. `poe test` — runs `uv run pytest`
4. Time-skipping tests (non-ARM only)

### `test-latest-deps` (ubuntu, Python 3.13, upgraded deps)
Same as above but with `uv lock --upgrade` first.

### `features-tests`
Runs the `temporalio/features` repo tests against this branch.

## Before Pushing

Always run the full lint suite locally before pushing:
```
uv run ruff check --select I
uv run ruff format --check
uv run pyright
uv run mypy --namespace-packages --check-untyped-defs .
uv run basedpyright
uv run pydocstyle --ignore-decorators=overload
```

Or equivalently: `poe lint` (requires `poe build-develop` first).

To auto-fix formatting: `poe format` (runs `ruff check --select I --fix` + `ruff format`).

## Dev Commands

All commands use `uv run` prefix. Key poe tasks:
- `poe build-develop` — build Rust bridge (required before lint/test)
- `poe format` — auto-fix formatting
- `poe lint` — run all linters
- `poe test` — run pytest

## Team Workflow

This repo uses **agent teams** (not subagents with worktrees). Delegate coding tasks to the `coder` teammate via `SendMessage`.

**All agents (team-lead and teammates) must load the `temporal-developer` skill** at the start of any task. This provides Temporal-specific guidance for workflows, activities, signals, queries, updates, Nexus, and SDK patterns.

### What coder CAN do
- Read/explore code (Glob, Grep, Read)
- Edit and write files (Edit, Write)
- Spawn sub-agents for exploration

### What coder CANNOT do — team-lead must handle
- **Run tests** — `uv run pytest` has no `--prefix` equivalent, and `cd` doesn't persist across Bash calls.
- **Run lints** — same reason (`uv run ruff`, `uv run pyright`, etc.).
- **Git operations** — commits, pushes, branch management.

### Writing teammate prompts
Be thorough and explicit upfront — don't rely on correcting teammates after launch. Every prompt to coder should include:
- **What to do** — the specific task, relevant file paths, and expected outcome.
- **What NOT to do** — explicitly state that coder cannot run tests or lints. Don't let them try and fail.
- **Operational constraints** — remind them: no compound Bash commands, no `git` commands, no `uv run`. Use `Edit`/`Write`/`Read`/`Glob`/`Grep` only.
- **Load the `temporal-developer` skill** — remind teammates to invoke it at the start of their task.
- **Dev environment context** — whether the Rust bridge is built, which branch they're on, any known lint pitfalls (e.g., basedpyright strictness).
- **Reference material** — point to existing patterns in the codebase (file paths and line numbers) rather than describing from memory.

### Workflow
1. **Team-lead** sends task to coder with a thorough prompt (see above).
2. **Coder** explores, writes code, reports back.
3. **Team-lead** runs all lints and tests, reports failures back to coder for fixes.
4. **Team-lead** commits and pushes after user approval.

### Context management
- Delegate aggressively to preserve your context window.
- Do not duplicate work your teammate is doing (don't read the same files they're exploring).
- When coder reports back, trust their findings — don't re-verify unless something seems off.

## CI Lint Details

`basedpyright` is the strictest linter and the most common source of CI failures. It catches things the others miss:
- `reportDeprecated` — flags use of deprecated APIs
- `reportUnusedParameter` — unused function parameters
- `reportMissingSuperCall` — missing `super().__init__()` calls
- `reportUninitializedInstanceVariable` — instance vars not set in `__init__`

Always run `uv run basedpyright` locally before pushing. If it passes, the other type checkers will almost certainly pass too.

## Time-Skipping Tests

CI runs tests twice: `poe test` (normal mode) and `poe test --workflow-environment time-skipping` (non-ARM only). The time-skipping test server has a **known limitation: it does not persist headers**. This means any test that depends on header propagation (e.g., tracing context) will fail in time-skipping mode. The established pattern for handling this is:

```python
if env.supports_time_skipping:
    pytest.skip("Time skipping server doesn't persist headers.")
```

See `tests/worker/test_workflow.py:8249` for the existing precedent.

## Branch Naming

Temporal convention: prepend `maplexu/` to branch names.
