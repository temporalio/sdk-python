`TracingInterceptor` no longer emits a `CompleteWorkflow` span when an update handler or validator raises `FailureError`; failing or rejecting an update does not complete the workflow.
