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

## Activity Context

**Sequence:** `workflow --scheduleActivity--> activity --result--> workflow`

**Rule:** All operations use `ActivitySerializationContext` with activity type, task queue, and is_local flag.

| Operation | Stage | Context Type | .NET | Java |
|-----------|-------|--------------|------|------|
| **1. Workflow Schedules Activity** | | | | |
| Serialize | DataConverter | ActivitySerializationContext | [WorkflowInstance.cs:2084-2093](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Worker/WorkflowInstance.cs#L2084-L2093) | [SyncWorkflowContext.java:257-267](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/sync/SyncWorkflowContext.java#L257-L267) |
| Encode | PayloadCodec | ActivitySerializationContext | [WorkflowCodecHelper.cs:243-253](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Worker/WorkflowCodecHelper.cs#L243-L253) | [SyncWorkflowContext.java:268-270](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/sync/SyncWorkflowContext.java#L268-L270) |
| **2. Activity Receives Input** | | | | |
| Decode | PayloadCodec | ActivitySerializationContext | Worker-side (not in workflow) | Worker-side (not in workflow) |
| Deserialize | DataConverter | ActivitySerializationContext | Worker-side (not in workflow) | Worker-side (not in workflow) |
| **3. Activity Returns Result** | | | | |
| Serialize | DataConverter | ActivitySerializationContext | Worker-side (not in workflow) | Worker-side (not in workflow) |
| Encode | PayloadCodec | ActivitySerializationContext | Worker-side (not in workflow) | Worker-side (not in workflow) |
| **4. Workflow Receives Result** | | | | |
| Decode | PayloadCodec | ActivitySerializationContext | [WorkflowCodecHelper.cs:68-76](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Worker/WorkflowCodecHelper.cs#L68-L76) | [SyncWorkflowContext.java:273-274](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/sync/SyncWorkflowContext.java#L273-L274) |
| Deserialize | DataConverter | ActivitySerializationContext | [WorkflowInstance.cs:2720](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Worker/WorkflowInstance.cs#L2720) | [SyncWorkflowContext.java:286-288](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/sync/SyncWorkflowContext.java#L286-L288) |

## Child Workflow Context

**Sequence:** `workflow --startChildWorkflow--> childWorkflow --result--> workflow`

**Rule:** Child operations use `WorkflowSerializationContext` with the child workflow's ID.

| Operation | Stage | Context Type | .NET | Java |
|-----------|-------|--------------|------|------|
| **1. Workflow Starts Child** | | | | |
| Serialize | DataConverter | WorkflowSerializationContext (child ID) | [WorkflowInstance.cs:2319-2326](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Worker/WorkflowInstance.cs#L2319-L2326) | [SyncWorkflowContext.java:687-690](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/sync/SyncWorkflowContext.java#L687-L690) |
| Encode | PayloadCodec | WorkflowSerializationContext (child ID) | [WorkflowCodecHelper.cs:301-310](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Worker/WorkflowCodecHelper.cs#L301-L310) | [SyncWorkflowContext.java:690](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/sync/SyncWorkflowContext.java#L690) |
| **2. Child Receives Input** | | | | |
| Decode | PayloadCodec | WorkflowSerializationContext (child ID) | [WorkflowCodecHelper.cs:125-126](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Worker/WorkflowCodecHelper.cs#L125-L126) | [ReplayWorkflowTaskHandler.java:160-162](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/replay/ReplayWorkflowTaskHandler.java#L160-L162) |
| Deserialize | DataConverter | WorkflowSerializationContext (child ID) | [WorkflowInstance.cs:395-397](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Worker/WorkflowInstance.cs#L395-L397) | [SyncWorkflow.java:75-77](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/sync/SyncWorkflow.java#L75-L77) |
| **3. Child Returns Result** | | | | |
| Serialize | DataConverter | WorkflowSerializationContext (child ID) | [WorkflowInstance.cs:1182-1184](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Worker/WorkflowInstance.cs#L1182-L1184) | [POJOWorkflowImplementationFactory.java:290-292](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/sync/POJOWorkflowImplementationFactory.java#L290-L292) |
| Encode | PayloadCodec | WorkflowSerializationContext (child ID) | [WorkflowCodecHelper.cs:217-220](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Worker/WorkflowCodecHelper.cs#L217-L220) | [WorkflowWorker.java:702-704](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/worker/WorkflowWorker.java#L702-L704) |
| **4. Workflow Receives Child Result** | | | | |
| Decode | PayloadCodec | WorkflowSerializationContext (child ID) | [WorkflowCodecHelper.cs:78-85](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Worker/WorkflowCodecHelper.cs#L78-L85) | [SyncWorkflowContext.java:1245-1248](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/sync/SyncWorkflowContext.java#L1245-L1248) |
| Deserialize | DataConverter | WorkflowSerializationContext (child ID) | [WorkflowInstance.cs:2808](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Worker/WorkflowInstance.cs#L2808) | [SyncWorkflowContext.java:1245-1248](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/sync/SyncWorkflowContext.java#L1245-L1248) |

## Nexus Operation Context

**Sequence:** `workflow --scheduleNexusOperation--> nexusOperation --result--> workflow`

**Rule:** Nexus operations have **no serialization context** (null/none).

| Operation | Stage | Context Type | .NET | Java |
|-----------|-------|--------------|------|------|
| **1. Workflow Schedules Nexus Op** | | | | |
| Serialize | DataConverter | None | [WorkflowInstance.cs:2525](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Worker/WorkflowInstance.cs#L2525) | [SyncWorkflowContext.java:790](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/sync/SyncWorkflowContext.java#L790) |
| Encode | PayloadCodec | None | [WorkflowCodecHelper.cs:355-356](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Worker/WorkflowCodecHelper.cs#L355-L356) | [SyncWorkflowContext.java:791-794](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/sync/SyncWorkflowContext.java#L791-L794) |
| **2. Nexus Operation Receives Input** | | | | |
| Decode | PayloadCodec | None | N/A (handler-side) | N/A (handler-side) |
| Deserialize | DataConverter | None | N/A (handler-side) | N/A (handler-side) |
| **3. Nexus Operation Returns Result** | | | | |
| Serialize | DataConverter | None | N/A (handler-side) | N/A (handler-side) |
| Encode | PayloadCodec | None | N/A (handler-side) | N/A (handler-side) |
| **4. Workflow Receives Result** | | | | |
| Decode | PayloadCodec | None | [WorkflowCodecHelper.cs:112-113](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Worker/WorkflowCodecHelper.cs#L112-L113) | N/A (codec not applied) |
| Deserialize | DataConverter | None | [WorkflowInstance.cs:2954](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Worker/WorkflowInstance.cs#L2954) | [SyncWorkflowContext.java:855-856](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/sync/SyncWorkflowContext.java#L855-L856) |

## Memo and Search Attribute Context

### Memos

**Rule:** Memos always use `WorkflowSerializationContext` with the workflow's ID when set or accessed.

| Operation | Context Type | .NET | Java |
|-----------|--------------|------|------|
| **Client sets memo on start** | WorkflowSerializationContext | [TemporalClient.Workflow.cs:827](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Client/TemporalClient.Workflow.cs#L827) | [RootWorkflowClientInvoker.java:292-294](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/client/RootWorkflowClientInvoker.java#L292-L294) |
| **Workflow upserts memo** | WorkflowSerializationContext | [WorkflowInstance.cs:472](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Worker/WorkflowInstance.cs#L472) | [SyncWorkflowContext.java:1416](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/sync/SyncWorkflowContext.java#L1416) |
| **Child workflow memo** | WorkflowSerializationContext (child ID) | [WorkflowInstance.cs:2359-2360](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Worker/WorkflowInstance.cs#L2359-L2360) | [SyncWorkflowContext.java:693-700](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/sync/SyncWorkflowContext.java#L693-L700) |
| **Schedule sets memo** | WorkflowSerializationContext | [ScheduleActionStartWorkflow.cs:199](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Client/Schedules/ScheduleActionStartWorkflow.cs#L199) | [ScheduleProtoUtil.java:134](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/client/ScheduleProtoUtil.java#L134) |

### Search Attributes

**Rule:** Search attributes do NOT use serialization context - they use specialized converters for indexing.

| Operation | Context Type | .NET | Java |
|-----------|--------------|------|------|
| **All operations** | None (direct proto conversion) | Uses `ToProto()` | Uses `toSearchAttributes()` |

## User-Accessible Data Converter

### Workflow Context

**Rule:** Data converters exposed to workflow code have `WorkflowSerializationContext` applied.

| SDK | API | Context | Reference |
|-----|-----|---------|-----------|
| **.NET** | `Workflow.PayloadConverter` | WorkflowSerializationContext | [Workflow.cs:185](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Workflows/Workflow.cs#L185) |
| **Java** | Not directly exposed | N/A | Workflow code cannot access data converter |
| **Python** | `workflow.payload_converter()` | WorkflowSerializationContext | [workflow.py:1148](https://github.com/temporalio/sdk-python/blob/main/temporalio/workflow.py#L1148) |

### Activity Context

**Rule:** Data converters exposed to activity code have `ActivitySerializationContext` applied.

| SDK | API | Context | Reference |
|-----|-----|---------|-----------|
| **.NET** | `ActivityExecutionContext.PayloadConverter` | ActivitySerializationContext | [ActivityExecutionContext.cs:49](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Activities/ActivityExecutionContext.cs#L49) |
| **Java** | Not directly exposed | N/A | Activity code cannot access data converter |
| **Python** | `activity.payload_converter()` | ActivitySerializationContext | [activity.py:470](https://github.com/temporalio/sdk-python/blob/main/temporalio/activity.py#L470) |

## Summary of Context Rules

1. **Workflow operations**: Always use `WorkflowSerializationContext` with the target workflow's ID
2. **Activity operations**: Use `ActivitySerializationContext` with activity details and `is_local` flag
3. **Child workflow operations**: Use `WorkflowSerializationContext` with the child's workflow ID
4. **Nexus operations**: No serialization context (null/none)
5. **Memos**: Always use workflow context
6. **Search attributes**: Never use context (indexing-specific conversion)
7. **User-exposed converters**: Have appropriate context pre-applied
