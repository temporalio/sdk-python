# Changelog fragments

For each PR with user-facing changes, add a Markdown file in each applicable
category folder. Write high-level release notes describing what users can observe.
Keep entries concise, ideally one or two sentences. Write each entry entirely on
one line: release tooling turns each nonempty line into a separate list item.
A fragment may contain multiple entries; blank lines are ignored.
Use fun, whimsical, unique lowercase kebab-case filenames, such as
`fixed/giggling-teapot.md` or `added/dancing-marshmallow.md`. PR numbers are optional.

| Folder | Changes |
| --- | --- |
| `added/` | New features |
| `stabilized/` | Features that are no longer experimental |
| `changed/` | Changes in existing functionality |
| `deprecated/` | Soon-to-be-removed features |
| `breaking-changes/` | Removed or backwards-incompatible features |
| `fixed/` | Notable bug fixes |
| `security/` | Notable security fixes |

Create a category folder when first needed. Place files directly inside it;
use one fragment per category for your PR. The folder supplies the category, so
the file contains only the entries, without a category or version heading or
leading list markers (`-`). Release tooling adds a list marker to each nonempty line:

```markdown
Reject unsupported activity executors when creating a worker, instead of failing during activity execution.
Preserve cancellation details when reporting activity failures.
```

Entries can include inline Markdown and documentation links. Do not edit
`CHANGELOG.md` for pending changes: it contains completed releases only. Maintainers
can apply `skip-changelog` to PRs that need no release note.

## Updating SDK Core

Initialize the checked-in submodule, then use its shared update command:

```bash
git submodule update --init
poe update-core
```

This fetches and updates Core to `origin/main`, importing user-facing entries from
`crates/sdk-core/CHANGELOG.md` over the old-to-new revision range. Use
`poe update-core --revision <ref>` to select a particular locally available revision instead.
Each affected category gets a fragment with an automatically generated whimsical
filename. Core entries join the language entries in the same categories, without
a Core prefix. Wrapped Core prose becomes one line per entry; unsupported block
Markdown must be rewritten before importing.

Start with a clean Core checkout. Backward and divergent updates are rejected.
Repeated updates import only the range since the previous checkout; an unchanged
pin creates no fragments. Review and commit the updated pin and generated fragments
together. The command does not stage or commit changes, refresh the bridge lockfile,
or resolve bridge compatibility issues.

If Core protobuf definitions changed, run `poe gen-protos-docker` and include the
regenerated bindings in the update PR. Refresh the bridge lockfile when needed.

## Preparing a release

Release tooling requires Rust/Cargo and the checked-in sdk-rust submodule:

```bash
git submodule update --init
python scripts/prepare_release.py 1.35.0 --date 2026-10-02
```

Run preparation from a clean worktree. The script creates a release branch from
`origin/main`, bumps Python versions and refreshes `uv.lock`, then tells the
shared tool to write the dated changelog and consume its fragments. It commits
the resulting version, lockfile, changelog, and fragment-deletion changes. It pushes the branch and opens a PR with the `skip-changelog` label.
Review the assembled notes before merging the release PR.

Preparation consumes both language and imported Core fragments. Publishing calls
the shared `release-notes` command to read that completed release and append Core
commit links under `### SDK Core Commits`. Core changelog entries are not collected
again during publishing. Both publishing jobs check out the pinned Core tool;
the commit range comes from the previous release tag and the current release's pin.

Assembly creates a dated section at the top of the changelog, groups notes in the
category order above, and sorts filenames within each category. Empty categories
are omitted. Each nonempty line becomes one list item, in the order it appears
in the fragment. Breaking changes use `### :boom: Breaking Changes`. Older releases
are preserved. Empty releases are allowed; duplicate versions are rejected.
Fragments are retained if validation or lockfile
refresh fails. A changelog validation failure leaves the version and lockfile
updates in the local worktree; inspect or restore those changes before retrying.
File-system errors can leave partial changelog preparation as well; preparation
stops without committing so those changes can be inspected.

Fragments merged after preparation remain pending for the next release. To
include them in the current release, update the release PR and regenerate its
notes from the intended fragment set before publishing.

The shared CLI lives in sdk-rust's `changelog-release-notes` crate. To validate
pending notes without making changes:

```bash
cargo run --manifest-path temporalio/bridge/sdk-core/crates/changelog-release-notes/Cargo.toml \
  --bin changelog-tool -- check --repo "$PWD"
```

See that crate's README for the reusable CLI. SDK-specific version and lockfile
updates happen before its `prepare` command; no hook or release-plan exchange is
required.
