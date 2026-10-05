# Changelog fragments

For each PR with user-facing changes, add a Markdown file in each applicable
category folder. Write high-level release notes describing what users can observe.
Keep entries concise, ideally one or two sentences.
Use fun, whimsical, unique lowercase kebab-case filenames, such as
`fixed/giggling-teapot.md` or `added/dancing-marshmallow.md`. PR numbers are optional.

| Folder | Changes |
| --- | --- |
| `added/` | New features |
| `changed/` | Changes in existing functionality |
| `deprecated/` | Soon-to-be-removed features |
| `breaking-changes/` | Removed or backwards-incompatible features |
| `fixed/` | Notable bug fixes |
| `security/` | Notable security fixes |

Create a category folder when first needed. Place files directly inside it;
use one fragment per category for your PR. The folder supplies the category, so
the file contains only the entry, without a category or version heading or a
leading list marker (`-`). Release tooling adds the list marker:

```markdown
Reject unsupported activity executors when creating a worker, instead of
failing during activity execution.
```

Bodies can include paragraphs, examples, and documentation links. Do not edit
`CHANGELOG.md` for pending changes: it contains completed releases only. Maintainers
can apply `skip-changelog` to PRs that need no release note.

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

Assembly creates a dated section at the top of the changelog, groups notes in the
category order above, and sorts filenames within each category. Empty categories
are omitted. Each fragment becomes one list item, with continuation lines indented
to keep paragraphs and examples inside it. Older releases are preserved. Empty releases and
duplicate versions are rejected. Fragments are retained if validation or lockfile
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
