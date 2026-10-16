Cancelling a workflow, or the task awaiting `ExternalWorkflowHandle.cancel()`, before the external cancel request resolves no longer fails every following workflow task with `InvalidStateError`.
