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

## Branch Naming

Temporal convention: prepend `maplexu/` to branch names.
