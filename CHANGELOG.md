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

### Fixed

- Removed the lazy-connect lock from the per-RPC hot path. It was previously
  acquired on every RPC, putting an event-loop-bound primitive on the hot path;
  it is now skipped once the client is connected. This reduces the client's
  coupling to the event loop it connected on, which can help when reusing a
  single long-lived `Client` across event loops or threads (e.g. the
  dedicated-loop pattern used with gevent/gunicorn and synchronous services).
  Note this does not make a `Client` fully thread- or loop-agnostic; reusing one
  long-lived loop is still the recommended pattern.
