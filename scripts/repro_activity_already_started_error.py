#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["temporalio"]
# ///
"""Repro: ActivityAlreadyStartedError contains wrong error message.

The error message says "Workflow execution already started" when it should say
"Activity execution already started".

This script demonstrates the bug without needing a Temporal server by simply
constructing the exception and printing its message.
"""

from temporalio.exceptions import ActivityAlreadyStartedError, WorkflowAlreadyStartedError

print("=== ActivityAlreadyStartedError message bug repro ===\n")

wf_err = WorkflowAlreadyStartedError("wf-id", "MyWorkflow", run_id="run-1")
act_err = ActivityAlreadyStartedError("act-id", "MyActivity", run_id="run-1")

print(f"WorkflowAlreadyStartedError message: {wf_err}")
print(f"ActivityAlreadyStartedError message: {act_err}")
print()

if str(act_err) == str(wf_err):
    print("BUG: Both errors have the same message!")
    print(f"     Both say: \"{act_err}\"")
    print("     ActivityAlreadyStartedError should say \"Activity execution already started\"")
else:
    print("OK: Error messages are distinct (bug is fixed).")
