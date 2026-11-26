**Experimental**: `@temporalio.nexus.temporal_operation` handlers no longer receive `ctx`: start handlers take `(self, client, input)`, and typed cancel overrides take `(self, options)`.
`TemporalStartOperationContext` and `TemporalCancelOperationContext` are removed. Read `service`, `operation`, and `request_id` from `temporalio.nexus.info()`, and task cancellation from `temporalio.nexus.task_cancellation()`.
To read incoming Nexus headers, use `input.ctx.headers` in a `NexusOperationInboundInterceptor`.
