# Contributor Guidance for `sdk-python`

This repository provides the Temporal Python SDK, including Python packages,
tests, optional integrations, and the Rust bridge used by the SDK. Use this
document as a quick reference when submitting pull requests.

## Requirements for coding agents

* Prefer the repo's Poe tasks over invoking underlying tools directly. Use
  `poe test`, `poe lint`, `poe format`, `poe build-develop`, and
  `poe bridge-lint` unless there is a specific reason to run a lower-level
  command.
* If you are about to run tests, you do not need to run a build separately first
  unless Rust bridge changes need a fresh editable extension. For bridge changes,
  run `poe build-develop` before Python tests that import `temporalio`.
* Use targeted tests while iterating. `poe test -s -k <pattern>` is preferred for
  a small behavioral change; run broader tests only when the change affects
  shared behavior.
* Do not use `--log-cli-level` by default. The pytest configuration shows logs
  for failed tests at the end without streaming all logs for passing tests.
* Tests that use the workflow environment may start a local Temporal dev server
  and may download a test server binary on first run. Unit tests that do not use
  the workflow environment do not start a server.
* Time-skipping tests are run with `poe test -s --workflow-environment
  time-skipping`. Time-skipping does not work on Linux or Windows ARM.
* It is extremely important that comments explain why something is necessary,
  not what the code already says. Avoid comments unless they clarify nonobvious
  behavior.
* Avoid broad refactors, style churn, or unrelated cleanups in behavior changes.
* Avoid unqualified imports from `temporalio` packages except `temporalio.types`.
  Relative imports are acceptable for private packages.
* Do not commit `uv.lock` or `pyproject.toml` changes created only for temporary
  protobuf downgrade workflows.

## Repo Specific Utilities

* Poe tasks are defined in `pyproject.toml`:
  * `poe build-develop` - build the Rust extension in editable debug mode.
  * `poe test` - run pytest in parallel with the default workflow environment.
  * `poe lint` - run import checks, formatting checks, type checks, and
    docstyle.
  * `poe lint-types` - run pyright, mypy, and basedpyright.
  * `poe bridge-lint` - run clippy for the Rust bridge.
  * `poe format` - run Ruff import sorting, Ruff formatting, and `cargo fmt` for
    the bridge.
  * `poe gen-protos-docker` - regenerate protobuf-related files using Docker.
  * `poe gen-protos` - regenerate protobuf-related files without Docker, with
    the Python/protobuf constraints documented in `README.md`.

## Building and Testing

The common local commands are:

```bash
uv sync --all-extras
poe build-develop
poe lint
poe test
poe test -s --workflow-environment time-skipping
```

For focused iteration, prefer:

```bash
poe test -s -k <test_or_pattern>
uv run pytest tests/path/test_file.py::test_name
```

For release artifacts, use `uv build`. Documentation can be generated with
`poe gen-docs`.

## Expectations for Pull Requests

* Format and lint code before submitting when practical.
* Include tests for behavior changes.
* Update public API documentation or doc comments for public behavior changes.
* Add a high-level changelog entry for user-facing changes according to the
  existing `CHANGELOG.md` convention.
* Keep commit messages short and in the imperative mood.
* Provide a clear PR description outlining what changed, why it changed, and
  what validation was run.

## Review Checklist

Reviewers will look for:

* CI passing, including build, lint, type checks, unit tests, and workflow
  environment tests.
* Tests covering behavior changes.
* Clear and concise code following existing style.
* Public API documentation updates when behavior changes.
* No unrelated generated files, lockfile churn, or broad rewrites.

## Where Things Are

* `temporalio/` - Python SDK source.
  * `temporalio/worker/` - worker implementation.
  * `temporalio/converter/` - payload and failure conversion.
  * `temporalio/testing/` - testing utilities.
  * `temporalio/nexus/` - Nexus support.
  * `temporalio/contrib/` - optional integrations.
  * `temporalio/bridge/` - Rust bridge and generated bridge bindings.
* `tests/` - pytest suites mirroring SDK areas.
* `scripts/` - generation, documentation, and helper scripts.
* `build/apidocs/` - generated API documentation.
* `dist/` - built wheels and source distributions.
* `temporalio/bridge/target/` - Rust build output. You should not need to inspect
  this directory.

## Notes

* The SDK supports Python 3.10 and newer.
* Generated protobuf and bridge files have specific regeneration workflows; see
  `README.md` before changing them.
* The Rust bridge uses SDK Core from `temporalio/bridge/sdk-core`.
* `__pycache__`, `build`, `dist`, and Rust `target` outputs are generated
  artifacts and should not be reviewed as source changes.
