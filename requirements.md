# Serialization Context Requirements

## Simple Workflow Context

**Sequence:** `client --start--> workflow --result--> client`

**Rule:** All operations use `WorkflowSerializationContext` with the target workflow's ID and namespace.

| Operation | Stage | Context Type | .NET | Java |
|-----------|-------|--------------|------|------|
| **1. Client Starts Workflow** | | | | |
| Serialize | DataConverter | WorkflowSerializationContext | [TemporalClient.Workflow.cs:693-696](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Client/TemporalClient.Workflow.cs#L693-L696) | [RootWorkflowClientInvoker.java:62-66](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/client/RootWorkflowClientInvoker.java#L62-L66) |
| Encode | PayloadCodec | WorkflowSerializationContext | [TemporalClient.Workflow.cs:699-700](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Client/TemporalClient.Workflow.cs#L699-L700) | [RootWorkflowClientInvoker.java:69](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/client/RootWorkflowClientInvoker.java#L69) |
| **2. Workflow Receives Input** | | | | |
| Decode | PayloadCodec | WorkflowSerializationContext | [WorkflowCodecHelper.cs:125-126](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Worker/WorkflowCodecHelper.cs#L125-L126) | [ReplayWorkflowTaskHandler.java:160-162](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/replay/ReplayWorkflowTaskHandler.java#L160-L162) |
| Deserialize | DataConverter | WorkflowSerializationContext | [WorkflowInstance.cs:395-397](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Worker/WorkflowInstance.cs#L395-L397) | [SyncWorkflow.java:75-77](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/sync/SyncWorkflow.java#L75-L77) |
| **3. Workflow Returns Result** | | | | |
| Serialize | DataConverter | WorkflowSerializationContext | [WorkflowInstance.cs:1182-1184](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Worker/WorkflowInstance.cs#L1182-L1184) | [POJOWorkflowImplementationFactory.java:290-292](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/sync/POJOWorkflowImplementationFactory.java#L290-L292) |
| Encode | PayloadCodec | WorkflowSerializationContext | [WorkflowCodecHelper.cs:217-220](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Worker/WorkflowCodecHelper.cs#L217-L220) | [WorkflowWorker.java:702-704](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/worker/WorkflowWorker.java#L702-L704) |
| **4. Client Receives Result** | | | | |
| Decode | PayloadCodec | WorkflowSerializationContext | [WorkflowHandle.cs:84-87](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Client/WorkflowHandle.cs#L84-L87) | [RootWorkflowClientInvoker.java:324-327](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/client/RootWorkflowClientInvoker.java#L324-L327) |
| Deserialize | DataConverter | WorkflowSerializationContext | [WorkflowHandle.cs:129-130](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Client/WorkflowHandle.cs#L129-L130) | [RootWorkflowClientInvoker.java:318-322](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/client/RootWorkflowClientInvoker.java#L318-L322) |
