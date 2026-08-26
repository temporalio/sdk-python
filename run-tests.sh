#!/usr/bin/env bash
# Runs the SAA operator-command tests on gmt/operator-commands-steps.
#
# No server setup: the harness downloads and starts the CLI release pinned in
# tests/__init__.py (DEV_SERVER_DOWNLOAD_VERSION) — the same path CI takes.
#
# NOTE: not added to git (per working conventions).
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

uv run pytest -q \
  tests/test_activity_operator_commands.py \
  tests/test_activity_operator_commands_interceptor.py \
  tests/test_activity_operator_commands_requests.py
