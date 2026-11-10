# Contributing to Temporal SDKs

Thanks for your interest in contributing to Temporal SDKs.

This guide describes expectations that apply across Temporal SDK repositories. Each
repository may have additional local conventions, but the guidance below should help
you open issues and pull requests that maintainers can evaluate efficiently.

> [!IMPORTANT]
> Non-Temporal contributors must not solicit user or customer information, including but not limited to workload, use-case, or failure information. Doing so will result in a ban from all Temporal repositories at Temporal's sole discretion.

## Before You Open an Issue

Search the existing issues first. If you find an issue that describes the same bug,
feature request, or design topic, add any relevant details there instead of opening a
duplicate. Use an upvote on the issue to show that it affects you too.

Issues are assigned to people when they are actively working on them. Before taking
on an issue, check whether it is already assigned so you do not duplicate someone
else's work.

Use GitHub issues for actionable bugs and feature work. For usage questions, help
debugging an application, or general discussion, join the relevant
language-specific channel in the
[Temporal community Slack](https://temporal.io/slack) or use the support channel
available to you.

## Bug Reports

When reporting a bug, include enough detail for someone else to reproduce or
understand the problem:

* A short summary of the problem.
* A minimal reproduction, preferably as code that can be copied into a small
  project or test.
* What you expected to happen and what actually happened.
* The SDK version.
* The language runtime version.
* The operating system and architecture.
* Temporal Server or Temporal Cloud details, if the issue depends on service
  behavior.
* Logs, stack traces, workflow histories, or other diagnostics that show the
  failure.
* Whether the behavior is a regression, and the last version where it worked if
  known.

## Feature Requests and Design Changes

Open or join a GitHub issue before starting substantial feature work, behavior
changes, or API design changes. This gives maintainers and other SDK users a chance
to discuss the approach before you invest in a larger implementation.

The relevant language-specific channel in Temporal community Slack is also a good
place for early discussion, but important decisions should still be captured in a
GitHub issue so they are visible and searchable.

Small bug fixes, documentation fixes, and narrowly scoped maintenance changes can go
straight to a pull request.

## Pull Requests

Good pull requests are focused and easy to review:

* Keep each pull request scoped to one logical change.
* Include tests for behavior changes.
* Update public API documentation or doc comments when public behavior changes.
* For user-facing changes, add a Markdown fragment in each applicable
  `changelog/<category>/` folder. Use `stabilized/` for features that are no longer
  experimental. Choose fun, whimsical, unique lowercase
  kebab-case filenames, such as `giggling-teapot.md`. Include only the release-note
  body without a leading list marker; the folder supplies its category. Keep
  entries concise, ideally one or two sentences. Write each entry entirely on
  one line: release tooling turns each nonempty line into a separate list item.
  A fragment may contain multiple entries. See [the fragment guide](changelog/README.md).
  Release tooling assembles `CHANGELOG.md` from these fragments.
* Describe what changed, why it changed, and what validation you ran.

Run the relevant local checks when practical. CI must pass before a pull request can
be merged.

## Updating SDK Core

Initialize the checked-in submodule, then use the shared update command:

```bash
git submodule update --init
poe update-core
```

This fetches and updates Core to the head of `origin/main`, importing user-facing
entries from `crates/sdk-core/CHANGELOG.md` over the old-to-new revision range.
Use `poe update-core --latest-tag` to fetch tags and select the highest stable
`core-vX.Y.Z` version, or `poe update-core --revision <ref>` to select a particular
locally available revision. These two options are mutually exclusive.
Each affected category gets a fragment with an automatically generated whimsical
filename. Core entries join the language entries in the same categories with a
`Core: ` prefix. Wrapped Core prose becomes one line per entry; unsupported block
Markdown must be rewritten before importing.

Start with a clean Core checkout. Backward and divergent updates are rejected.
Repeated updates import only the range since the previous checkout; an unchanged
pin creates no fragments. Review and commit the updated pin and generated fragments
together with the bridge manifest and lockfile. Python copies Core path dependency
versions from the updated checkout into `temporalio/bridge/Cargo.toml` and runs
`cargo fetch` to refresh `Cargo.lock`, preserving existing valid registry pins.
The command does not stage or commit changes or resolve Rust API
compatibility issues. If dependency refresh fails, inspect the updated pin,
fragments, and manifest before retrying.

The command then runs `poe gen-protos`. Include regenerated bindings in the update
PR.

## Preparing a Release

Run `poe prepare-release <version>` (optionally with `--date YYYY-MM-DD`) from a
clean worktree. This creates `chore/release-<version>` from `origin/main`, runs the
Core update with `--latest-tag`, and then prepares the Python versions, lockfile, and
changelog. It commits the Core and Python changes together, pushes the release
branch, and opens a PR. A failed Core update or proto generation stops preparation
before changelog fragments are consumed. Review any partial changes before retrying.
Rerunning for the same version rebuilds the local release branch from `origin/main`,
replacing prior release-branch commits, and updates the remote branch using an
explicit force-with-lease. If the remote branch changes during preparation, the
push fails rather than overwriting that change. An existing open release PR is
reused, preserving its description and discussion.
The optional `--date` sets the date in the changelog release heading and defaults
to today; it does not schedule publication.

## Things to Avoid

Avoid changes that make review harder without improving the contribution:

* Unrelated refactors mixed into a behavior change.
* Style-only churn.
* Large feature pull requests that were not discussed first.
* License, copyright, or other legal changes without maintainer discussion.

## AI-Generated Contributions

Using AI tools while contributing is acceptable. You are responsible for the
correctness, quality, and maintainability of everything you submit.

Contributors must fully understand the issue they are fixing and be able to explain
the proposed change. We expect that human understanding to be evident in pull
request responses and design discussions. If a contribution's interaction appears
entirely AI-driven, maintainers may close it: it does not provide a benefit over
maintainers using AI tooling themselves.

Thoroughly self-review AI-generated code and documentation before opening a pull
request. Make sure it is correct, tested where appropriate, and consistent with the
style and patterns of the codebase.

Keep AI-assisted changes concise and scoped. Avoid verbose generated prose,
unnecessary comments, or broad rewrites that make the change harder to review.

## Contributor License Agreement

All contributors must complete the Temporal Contributor License Agreement (CLA)
before changes can be merged. A link to the CLA will be posted in the pull request.

## Security Issues

Do not open public GitHub issues for suspected security vulnerabilities. Report them
to security@temporal.io instead.

## Review and CI

Maintainers review pull requests for correctness, compatibility, test coverage,
documentation, and long-term maintainability. Review may require changes before a
pull request can be merged, and it may take maintainers some time to review a
contribution.

CI is the final validation gate. If CI fails, update the pull request or ask for help
if the failure appears unrelated to your change. Some CI gates may wait for a
maintainer to approve or run them.

## Inactive Pull Requests

Maintainers may close inactive pull requests after follow-up if they are no longer
moving forward. If that happens, you are welcome to reopen the pull request or open a
new one when you are ready to continue.

## Community Conduct

Keep discussions respectful, constructive, and focused on the work. Clear context,
specific examples, and patience with review feedback help everyone move faster.
