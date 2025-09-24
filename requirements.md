# Serialization Context Requirements

## Simple Workflow Context

**Sequence:** `client --start--> workflow --result--> client`

**Rule:** All operations use `WorkflowSerializationContext` with the target workflow's ID and namespace.

| Operation | Stage | Context Type | .NET | Java | Python |
|-----------|-------|--------------|------|------|--------|
| **1. Client Starts Workflow** | | | | | |
| Serialize | DataConverter | WorkflowSerializationContext | [TemporalClient.Workflow.cs:693-696](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Client/TemporalClient.Workflow.cs#L693-L696) | [RootWorkflowClientInvoker.java:62-66](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/client/RootWorkflowClientInvoker.java#L62-L66) | [client.py:5993-5998](https://github.com/temporalio/sdk-python/blob/main/temporalio/client.py#L5993-L5998) |
| Encode | PayloadCodec | WorkflowSerializationContext | [TemporalClient.Workflow.cs:699-700](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Client/TemporalClient.Workflow.cs#L699-L700) | [RootWorkflowClientInvoker.java:69](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/client/RootWorkflowClientInvoker.java#L69) | [client.py:6005](https://github.com/temporalio/sdk-python/blob/main/temporalio/client.py#L6005) |
| **2. Workflow Receives Input** | | | | | |
| Decode | PayloadCodec | WorkflowSerializationContext | [WorkflowCodecHelper.cs:125-126](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Worker/WorkflowCodecHelper.cs#L125-L126) | [ReplayWorkflowTaskHandler.java:160-162](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/replay/ReplayWorkflowTaskHandler.java#L160-L162) | [_workflow.py:289-293](https://github.com/temporalio/sdk-python/blob/main/temporalio/worker/_workflow.py#L289-L293) |
| Deserialize | DataConverter | WorkflowSerializationContext | [WorkflowInstance.cs:395-397](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Worker/WorkflowInstance.cs#L395-L397) | [SyncWorkflow.java:75-77](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/sync/SyncWorkflow.java#L75-L77) | [_workflow.py:278-283](https://github.com/temporalio/sdk-python/blob/main/temporalio/worker/_workflow.py#L278-L283) |
| **3. Workflow Returns Result** | | | | | |
| Serialize | DataConverter | WorkflowSerializationContext | [WorkflowInstance.cs:1182-1184](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Worker/WorkflowInstance.cs#L1182-L1184) | [POJOWorkflowImplementationFactory.java:290-292](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/sync/POJOWorkflowImplementationFactory.java#L290-L292) | [_workflow.py:340-346](https://github.com/temporalio/sdk-python/blob/main/temporalio/worker/_workflow.py#L340-L346) |
| Encode | PayloadCodec | WorkflowSerializationContext | [WorkflowCodecHelper.cs:217-220](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Worker/WorkflowCodecHelper.cs#L217-L220) | [WorkflowWorker.java:702-704](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/worker/WorkflowWorker.java#L702-L704) | [_workflow.py:363-367](https://github.com/temporalio/sdk-python/blob/main/temporalio/worker/_workflow.py#L363-L367) |
| **4. Client Receives Result** | | | | | |
| Decode | PayloadCodec | WorkflowSerializationContext | [WorkflowHandle.cs:84-87](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Client/WorkflowHandle.cs#L84-L87) | [RootWorkflowClientInvoker.java:324-327](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/client/RootWorkflowClientInvoker.java#L324-L327) | [client.py:1601-1605](https://github.com/temporalio/sdk-python/blob/main/temporalio/client.py#L1601-L1605) |
| Deserialize | DataConverter | WorkflowSerializationContext | [WorkflowHandle.cs:129-130](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Client/WorkflowHandle.cs#L129-L130) | [RootWorkflowClientInvoker.java:318-322](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/client/RootWorkflowClientInvoker.java#L318-L322) | [client.py:1715-1718](https://github.com/temporalio/sdk-python/blob/main/temporalio/client.py#L1715-L1718) |

## Activity Context

**Sequence:** `workflow --scheduleActivity--> activity --result--> workflow`

**Rule:** All operations use `ActivitySerializationContext` with activity type, task queue, and is_local flag.

| Operation | Stage | Context Type | .NET | Java | Python |
|-----------|-------|--------------|------|------|--------|
| **1. Workflow Schedules Activity** | | | | | |
| Serialize | DataConverter | ActivitySerializationContext | [WorkflowInstance.cs:2084-2093](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Worker/WorkflowInstance.cs#L2084-L2093) | [SyncWorkflowContext.java:257-267](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/sync/SyncWorkflowContext.java#L257-L267) | [_workflow_instance.py:2968-2972](https://github.com/temporalio/sdk-python/blob/main/temporalio/worker/_workflow_instance.py#L2968-L2972) |
| Encode | PayloadCodec | ActivitySerializationContext | [WorkflowCodecHelper.cs:243-253](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Worker/WorkflowCodecHelper.cs#L243-L253) | [SyncWorkflowContext.java:268-270](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/sync/SyncWorkflowContext.java#L268-L270) | [_workflow.py:716-733](https://github.com/temporalio/sdk-python/blob/main/temporalio/worker/_workflow.py#L716-L733) |
| **2. Activity Receives Input** | | | | | |
| Decode | PayloadCodec | ActivitySerializationContext | Worker-side (not in workflow) | Worker-side (not in workflow) | [_activity.py:317-318](https://github.com/temporalio/sdk-python/blob/main/temporalio/worker/_activity.py#L317-L318) |
| Deserialize | DataConverter | ActivitySerializationContext | Worker-side (not in workflow) | Worker-side (not in workflow) | [_activity.py:526-530](https://github.com/temporalio/sdk-python/blob/main/temporalio/worker/_activity.py#L526-L530) |
| **3. Activity Returns Result** | | | | | |
| Serialize | DataConverter | ActivitySerializationContext | Worker-side (not in workflow) | Worker-side (not in workflow) | [_activity.py:322-323](https://github.com/temporalio/sdk-python/blob/main/temporalio/worker/_activity.py#L322-L323) |
| Encode | PayloadCodec | ActivitySerializationContext | Worker-side (not in workflow) | Worker-side (not in workflow) | [_activity.py:317-318](https://github.com/temporalio/sdk-python/blob/main/temporalio/worker/_activity.py#L317-L318) |
| **4. Workflow Receives Result** | | | | | |
| Decode | PayloadCodec | ActivitySerializationContext | [WorkflowCodecHelper.cs:68-76](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Worker/WorkflowCodecHelper.cs#L68-L76) | [SyncWorkflowContext.java:273-274](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/sync/SyncWorkflowContext.java#L273-L274) | [_workflow.py:716-745](https://github.com/temporalio/sdk-python/blob/main/temporalio/worker/_workflow.py#L716-L745) |
| Deserialize | DataConverter | ActivitySerializationContext | [WorkflowInstance.cs:2720](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Worker/WorkflowInstance.cs#L2720) | [SyncWorkflowContext.java:286-288](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/sync/SyncWorkflowContext.java#L286-L288) | [_workflow_instance.py:804-810](https://github.com/temporalio/sdk-python/blob/main/temporalio/worker/_workflow_instance.py#L804-L810) |

## Child Workflow Context

**Sequence:** `workflow --startChildWorkflow--> childWorkflow --result--> workflow`

**Rule:** Child operations use `WorkflowSerializationContext` with the child workflow's ID.

| Operation | Stage | Context Type | .NET | Java | Python |
|-----------|-------|--------------|------|------|--------|
| **1. Workflow Starts Child** | | | | | |
| Serialize | DataConverter | WorkflowSerializationContext (child ID) | [WorkflowInstance.cs:2319-2326](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Worker/WorkflowInstance.cs#L2319-L2326) | [SyncWorkflowContext.java:687-690](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/sync/SyncWorkflowContext.java#L687-L690) | [_workflow_instance.py:3124-3128](https://github.com/temporalio/sdk-python/blob/main/temporalio/worker/_workflow_instance.py#L3124-L3128) |
| Encode | PayloadCodec | WorkflowSerializationContext (child ID) | [WorkflowCodecHelper.cs:301-310](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Worker/WorkflowCodecHelper.cs#L301-L310) | [SyncWorkflowContext.java:690](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/sync/SyncWorkflowContext.java#L690) | [_workflow.py:716-745](https://github.com/temporalio/sdk-python/blob/main/temporalio/worker/_workflow.py#L716-L745) |
| **2. Child Receives Input** | | | | | |
| Decode | PayloadCodec | WorkflowSerializationContext (child ID) | [WorkflowCodecHelper.cs:125-126](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Worker/WorkflowCodecHelper.cs#L125-L126) | [ReplayWorkflowTaskHandler.java:160-162](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/replay/ReplayWorkflowTaskHandler.java#L160-L162) | [_workflow.py:289-293](https://github.com/temporalio/sdk-python/blob/main/temporalio/worker/_workflow.py#L289-L293) |
| Deserialize | DataConverter | WorkflowSerializationContext (child ID) | [WorkflowInstance.cs:395-397](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Worker/WorkflowInstance.cs#L395-L397) | [SyncWorkflow.java:75-77](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/sync/SyncWorkflow.java#L75-L77) | [_workflow.py:278-283](https://github.com/temporalio/sdk-python/blob/main/temporalio/worker/_workflow.py#L278-L283) |
| **3. Child Returns Result** | | | | | |
| Serialize | DataConverter | WorkflowSerializationContext (child ID) | [WorkflowInstance.cs:1182-1184](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Worker/WorkflowInstance.cs#L1182-L1184) | [POJOWorkflowImplementationFactory.java:290-292](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/sync/POJOWorkflowImplementationFactory.java#L290-L292) | [_workflow.py:340-346](https://github.com/temporalio/sdk-python/blob/main/temporalio/worker/_workflow.py#L340-L346) |
| Encode | PayloadCodec | WorkflowSerializationContext (child ID) | [WorkflowCodecHelper.cs:217-220](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Worker/WorkflowCodecHelper.cs#L217-L220) | [WorkflowWorker.java:702-704](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/worker/WorkflowWorker.java#L702-L704) | [_workflow.py:363-367](https://github.com/temporalio/sdk-python/blob/main/temporalio/worker/_workflow.py#L363-L367) |
| **4. Workflow Receives Child Result** | | | | | |
| Decode | PayloadCodec | WorkflowSerializationContext (child ID) | [WorkflowCodecHelper.cs:78-85](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Worker/WorkflowCodecHelper.cs#L78-L85) | [SyncWorkflowContext.java:1245-1248](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/sync/SyncWorkflowContext.java#L1245-L1248) | [_workflow.py:716-745](https://github.com/temporalio/sdk-python/blob/main/temporalio/worker/_workflow.py#L716-L745) |
| Deserialize | DataConverter | WorkflowSerializationContext (child ID) | [WorkflowInstance.cs:2808](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Worker/WorkflowInstance.cs#L2808) | [SyncWorkflowContext.java:1245-1248](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/sync/SyncWorkflowContext.java#L1245-L1248) | [_workflow_instance.py:842-858](https://github.com/temporalio/sdk-python/blob/main/temporalio/worker/_workflow_instance.py#L842-L858) |

## Nexus Operation Context

**Sequence:** `workflow --scheduleNexusOperation--> nexusOperation --result--> workflow`

**Rule:** Nexus operations have **no serialization context** (null/none).

| Operation | Stage | Context Type | .NET | Java | Python |
|-----------|-------|--------------|------|------|--------|
| **1. Workflow Schedules Nexus Op** | | | | | |
| Serialize | DataConverter | None | [WorkflowInstance.cs:2525](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Worker/WorkflowInstance.cs#L2525) | [SyncWorkflowContext.java:790](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/sync/SyncWorkflowContext.java#L790) | N/A (not yet implemented) |
| Encode | PayloadCodec | None | [WorkflowCodecHelper.cs:355-356](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Worker/WorkflowCodecHelper.cs#L355-L356) | [SyncWorkflowContext.java:791-794](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/sync/SyncWorkflowContext.java#L791-L794) | N/A (not yet implemented) |
| **2. Nexus Operation Receives Input** | | | | | |
| Decode | PayloadCodec | None | N/A (handler-side) | N/A (handler-side) | N/A (handler-side) |
| Deserialize | DataConverter | None | N/A (handler-side) | N/A (handler-side) | N/A (handler-side) |
| **3. Nexus Operation Returns Result** | | | | | |
| Serialize | DataConverter | None | N/A (handler-side) | N/A (handler-side) | N/A (handler-side) |
| Encode | PayloadCodec | None | N/A (handler-side) | N/A (handler-side) | N/A (handler-side) |
| **4. Workflow Receives Result** | | | | | |
| Decode | PayloadCodec | None | [WorkflowCodecHelper.cs:112-113](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Worker/WorkflowCodecHelper.cs#L112-L113) | N/A (codec not applied) | N/A (not yet implemented) |
| Deserialize | DataConverter | None | [WorkflowInstance.cs:2954](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Worker/WorkflowInstance.cs#L2954) | [SyncWorkflowContext.java:855-856](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/sync/SyncWorkflowContext.java#L855-L856) | N/A (not yet implemented) |

## Memo and Search Attribute Context

### Memos

**Rule:** Memos always use `WorkflowSerializationContext` with the workflow's ID when set or accessed.

| Operation | Context Type | .NET | Java | Python |
|-----------|--------------|------|------|--------|
| **Client sets memo on start** | WorkflowSerializationContext | [TemporalClient.Workflow.cs:827](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Client/TemporalClient.Workflow.cs#L827) | [RootWorkflowClientInvoker.java:292-294](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/client/RootWorkflowClientInvoker.java#L292-L294) | [client.py:6027-6028](https://github.com/temporalio/sdk-python/blob/main/temporalio/client.py#L6027-L6028) |
| **Workflow upserts memo** | WorkflowSerializationContext | [WorkflowInstance.cs:472](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Worker/WorkflowInstance.cs#L472) | [SyncWorkflowContext.java:1416](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/sync/SyncWorkflowContext.java#L1416) | [_workflow_instance.py:908-912](https://github.com/temporalio/sdk-python/blob/main/temporalio/worker/_workflow_instance.py#L908-L912) |
| **Child workflow memo** | WorkflowSerializationContext (child ID) | [WorkflowInstance.cs:2359-2360](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Worker/WorkflowInstance.cs#L2359-L2360) | [SyncWorkflowContext.java:693-700](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/sync/SyncWorkflowContext.java#L693-L700) | [_workflow_instance.py:3156-3159](https://github.com/temporalio/sdk-python/blob/main/temporalio/worker/_workflow_instance.py#L3156-L3159) |
| **Schedule sets memo** | WorkflowSerializationContext | [ScheduleActionStartWorkflow.cs:199](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Client/Schedules/ScheduleActionStartWorkflow.cs#L199) | [ScheduleProtoUtil.java:134](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/client/ScheduleProtoUtil.java#L134) | [client.py:4220-4229](https://github.com/temporalio/sdk-python/blob/main/temporalio/client.py#L4220-L4229) |

### Search Attributes

**Rule:** Search attributes do NOT use serialization context - they use specialized converters for indexing.

| Operation | Context Type | .NET | Java | Python |
|-----------|--------------|------|------|--------|
| **All operations** | None (direct proto conversion) | Uses `ToProto()` | Uses `toSearchAttributes()` | [converter.py:1358-1363](https://github.com/temporalio/sdk-python/blob/main/temporalio/converter.py#L1358-L1363) |

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

## Async Activity Completion

**Rule:** Async activity completion uses `ActivitySerializationContext` with available activity information.

| Operation | Context Type | .NET | Java | Python |
|-----------|--------------|------|------|--------|
| **Complete** | ActivitySerializationContext | [AsyncActivityHandle.cs:42](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Client/AsyncActivityHandle.cs#L42) | [ActivityCompletionClientImpl.java:51-56](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/client/ActivityCompletionClientImpl.java#L51-L56) | [client.py:6477-6481](https://github.com/temporalio/sdk-python/blob/main/temporalio/client.py#L6477-L6481) |
| **Fail** | ActivitySerializationContext | [AsyncActivityHandle.cs:51](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Client/AsyncActivityHandle.cs#L51) | [ActivityCompletionClientImpl.java:63-65](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/client/ActivityCompletionClientImpl.java#L63-L65) | [client.py:6511-6514](https://github.com/temporalio/sdk-python/blob/main/temporalio/client.py#L6511-L6514) |
| **Report Cancellation** | ActivitySerializationContext | [AsyncActivityHandle.cs:62](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Client/AsyncActivityHandle.cs#L62) | [ActivityCompletionClientImpl.java:74](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/client/ActivityCompletionClientImpl.java#L74) | [client.py:6588-6600](https://github.com/temporalio/sdk-python/blob/main/temporalio/client.py#L6588-L6600) |
| **WithContext Method** | Creates context-aware handle | [AsyncActivityHandle.cs:71-73](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Client/AsyncActivityHandle.cs#L71-L73) | [ActivityCompletionClient.java:107-108](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/client/ActivityCompletionClientImpl.java#L107-L108) | N/A (context applied internally) |

### Notes
- Python applies partial context internally in `_async_activity_data_converter()` with workflow ID when available
- .NET and Java allow explicit context application via `WithSerializationContext()`/`withContext()` methods
- Context may be incomplete for async completion (e.g., missing activity type) when using task token

## Activity Heartbeating

**Rule:** Heartbeating uses `ActivitySerializationContext` with full activity information when available.

| Operation | Context Source | .NET | Java | Python |
|-----------|---------------|------|------|--------|
| **During Execution** | From running activity info | Same as async completion | [HeartbeatContextImpl.java:67-75](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/internal/activity/HeartbeatContextImpl.java#L67-L75) | [_activity.py:257-265](https://github.com/temporalio/sdk-python/blob/main/temporalio/worker/_activity.py#L257-L265) |
| **Async Heartbeat** | From task token or activity ID | [AsyncActivityHandle.cs:30-32](https://github.com/temporalio/sdk-dotnet/blob/main/src/Temporalio/Client/AsyncActivityHandle.cs#L30-L32) | [ActivityCompletionClientImpl.java:94-102](https://github.com/temporalio/sdk-java/blob/master/temporal-sdk/src/main/java/io/temporal/client/ActivityCompletionClientImpl.java#L94-L102) | [client.py:6422-6426](https://github.com/temporalio/sdk-python/blob/main/temporalio/client.py#L6422-L6426) |

### Notes
- Heartbeating during activity execution has full context information
- Async heartbeating (outside activity execution) may have partial context
- All SDKs apply context to ensure proper data conversion for heartbeat details

## Summary of Context Rules

1. **Workflow operations**: Always use `WorkflowSerializationContext` with the target workflow's ID
2. **Activity operations**: Use `ActivitySerializationContext` with activity details and `is_local` flag
3. **Child workflow operations**: Use `WorkflowSerializationContext` with the child's workflow ID
4. **Nexus operations**: No serialization context (null/none)
5. **Memos**: Always use workflow context
6. **Search attributes**: Never use context (indexing-specific conversion)
7. **User-exposed converters**: Have appropriate context pre-applied
8. **Async activity completion**: Use activity context with available information
9. **Heartbeating**: Use activity context with full info during execution, partial for async

## Important Note About .NET

⚠️ **Current .NET Bug** ([temporalio/sdk-dotnet#523](https://github.com/temporalio/sdk-dotnet/issues/523) - **OPEN**)

The .NET SDK currently has a bug where it incorrectly applies `WorkflowSerializationContext` to **all** data conversion operations within a workflow context, including:
- Activities (should use `ActivitySerializationContext`)
- Nexus operations (should use no context)

**This document shows the intended/correct behavior**, which is:
- How Java currently works ✅
- How Python should work after alignment ✅
- How .NET **should** work (but doesn't yet) ⚠️

The tables above reflect the desired state where each SDK applies context selectively based on the operation type.
