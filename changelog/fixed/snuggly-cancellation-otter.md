Fix `workflow.wait_condition` raising cancellation in shielded tasks after a workflow cancellation request, while preserving task cancellation when a condition becomes ready concurrently.
Preserve an already-available child workflow outcome when parent cancellation interrupts its await, so the parent can catch cancellation and retrieve the child outcome by awaiting the handle again.
Preserve already-available Nexus operation outcomes when the caller is cancelled or explicitly cancels the handle, allowing completed outcomes to be retrieved afterward.
Preserve already-available activity outcomes under single-batch activation processing when caller cancellation interrupts the await, allowing the caller to retrieve the outcome by awaiting the handle again.
