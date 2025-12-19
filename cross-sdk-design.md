# Standalone Activities Cross-SDK design (draft)

# Open questions

[Standalone Activities SDK design - open questions](https://www.notion.so/Standalone-Activities-SDK-design-open-questions-2c58fc567738803996b6c43e3332c327?pvs=21)

# Approval list

*Everyone feel free to add your names here.*

.NET (owners: @Chad Retz, @Maciej Dudkowski)

- Approved by:
- Blocked by:

Go (owners: @Quinn Klassen, @Andrew Yuan)

- Approved by: Andrew (defer to Quinn for final approve)
- Blocked by:

Java (owners: @Quinn Klassen, @Maciej Dudkowski)

- Approved by:
- Blocked by:

Ruby (owners: @Chad Retz, @Chris Olszewski)

- Approved by:
- Blocked by:

Python (owners: @Tim Conley, @Alex Mazzeo, @Thomas Hardy)

- Approved by:
- Blocked by:

TypeScript (owners: @James Watkins, @Thomas Hardy, @Chris Olszewski)

- Approved by:
- Blocked by:

# High-level summary of changes

1. New client calls:
    1. Start activity (async)
    2. Execute activity (wait for result)
    3. Get activity execution result
    4. Get activity execution info
    5. List activity executions (no interceptor for now)
    6. Count activity executions
    7. Cancel activity execution
    8. Terminate activity
2. Activity info changes:
    1. **BREAKING CHANGE:** `workflow_id`, `workflow_run_id`, `workflow_type`  and `workflow_namespace` fields become nullable.
        1. Workflow activities: always set
        2. Standalone activities: always null
        3. Exact implementation varies by language.
    2. `workflow_namespace` is deprecated in favor of new field `namespace`.
        1. Both fields always have the same value, regardless of whether activity is in workflow or standalone. This behavior will be stated in documentation for `workflow_namespace` field.
    3. New field: `activity_run_id` (optional string).
        1. Workflow activities: always null
        2. Standalone activities: always set
    4. New field: `is_workflow_activity` (boolean).
        1. Calculated property where possible - details vary by language.
3. Async activities:
    1. Workflow ID becomes optional in client calls.
    2. Run ID refers to either workflow or activity run ID based on presence of workflow ID.
4. Serialization context:
    1. Where applicable, standalone activities will have their own serialization context type separate from workflow activity context type. This is to avoid breaking changes for existing data converters, as converters written for workflow activities are unlikely to work correctly outside of workflow context.

# Core SDK

## 1. Changes to SDK Proto `coresdk.activity_task.Start`

1. `workflow_namespace` is renamed to `namespace`
2. `workflow_type` and `workflow_execution` are empty if activity is standalone.
3. New field: `string activity_run_id = 18;`  - empty if activity is in workflow.

# .NET

## 1. New methods in `Client.ITemporalClient`

A. `StartActivityAsync` and `ExecuteActivityAsync`

Both methods have multiple overloads for different ways to pass the activity name and arguments, same as `Workflows.Workflow.ExecuteActivityAsync`. One overload takes activity name as a string and arguments as a list or arbitrary objects. The other overloads take a lambda expression describing the activity invocation with arguments in place.

`StartActivityAsync` can be called with or without generic argument, returning generic `ActivityHandle<TResult>` or non-generic `ActivityHandle`.

`ExecuteActivityAsync` can be called with or without generic argument. Generic variant returns the activity result deserialized to given type. Non-generic variant waits for completion but doesn’t return result.

- Method signatures

    ```tsx
    public Task<ActivityHandle<TResult>> StartActivityAsync<TResult>(
        Expression<Func<TResult>> activityCall,
        ActivityOptions options)

    public Task<ActivityHandle> StartActivityAsync(
        Expression<Action> activityCall,
        ActivityOptions options)

    public Task<ActivityHandle<TResult>> StartActivityAsync<TActivityInstance, TResult>(
        Expression<Func<TActivityInstance, TResult>> activityCall,
        ActivityOptions options)

    public Task<ActivityHandle> StartActivityAsync<TActivityInstance>(
        Expression<Action<TActivityInstance>> activityCall,
        ActivityOptions options)

    public Task<ActivityHandle<TResult>> StartActivityAsync<TResult>(
        Expression<Func<Task<TResult>>> activityCall,
        ActivityOptions options)

    public Task<ActivityHandle> StartActivityAsync(
        Expression<Func<Task>> activityCall,
        ActivityOptions options)

    public Task<ActivityHandle<TResult>> StartActivityAsync<TActivityInstance, TResult>(
        Expression<Func<TActivityInstance, Task<TResult>>> activityCall,
        ActivityOptions options)

    public Task<ActivityHandle<TResult>> StartActivityAsync<TActivityInstance>(
        Expression<Func<TActivityInstance, Task>> activityCall,
        ActivityOptions options)

    public Task<ActivityHandle<TResult>> StartActivityAsync<TResult>(
        string activity,
        IReadOnlyCollection<object?> args
        ActivityOptions options)

    public Task<ActivityHandle> StartActivityAsync(
        string activity,
        IReadOnlyCollection<object?> args
        ActivityOptions options)

    public Task<TResult> ExecuteActivityAsync<TResult>(
        Expression<Func<TResult>> activityCall,
        ActivityOptions options)

    public Task ExecuteActivityAsync(
        Expression<Action> activityCall,
        ActivityOptions options)

    public Task<TResult> ExecuteActivityAsync<TActivityInstance, TResult>(
        Expression<Func<TActivityInstance, TResult>> activityCall,
        ActivityOptions options)

    public Task ExecuteActivityAsync<TActivityInstance>(
        Expression<Action<TActivityInstance>> activityCall,
        ActivityOptions options)

    public Task<TResult> ExecuteActivityAsync<TResult>(
        Expression<Func<Task<TResult>>> activityCall,
        ActivityOptions options)

    public Task ExecuteActivityAsync(
        Expression<Func<Task>> activityCall,
        ActivityOptions options)

    public Task<TResult> ExecuteActivityAsync<TActivityInstance, TResult>(
        Expression<Func<TActivityInstance, Task<TResult>>> activityCall,
        ActivityOptions options)

    public Task<TResult> ExecuteActivityAsync<TActivityInstance>(
        Expression<Func<TActivityInstance, Task>> activityCall,
        ActivityOptions options)

    public Task<TResult> ExecuteActivityAsync<TResult>(
        string activity,
        IReadOnlyCollection<object?> args
        ActivityOptions options)

    public Task ExecuteActivityAsync(
        string activity,
        IReadOnlyCollection<object?> args
        ActivityOptions options)
    ```


B. Other methods

```csharp
public ActivityHandle GetActivityHandle(
		string activityId, string? activityRunId);

public ActivityHandle<TResult> GetActivityHandle<TResult>(
		string activityId, string? activityRunId);

#if NETCOREAPP3_0_OR_GREATER
public IAsyncEnumerable<ActivityExecution> ListActivitiesAsync(
		string query, ActivityListOptions? options = null);
#endif

public Task<ActivityExecutionCount> CountActivitiesAsync(
		string query, ActivityListOptions? options = null);
```

## 2. New types

```csharp
namespace Temporalio.Client {
		public record ActivityHandle(
		    ITemporalClient client,
		    string activityId,
		    string? activityRunId)
    {
		    public async Task GetResultAsync(
				    ActivityGetResultOptions? options = null);

		    public async Task<TResult> GetResultAsync<TResult>(
				    ActivityGetResultOptions? options = null);

		    public async Task<ActivityExecutionDescription> DescribeAsync(
				    ActivityDescribeOptions? options = null);

		    public async Task CancelAsync(
				    string? reason = null,
				    ActivityCancelOptions? options = null);

		    public async Task TerminateAsync(
				    string? reason = null,
				    ActivityTerminateOptions? options = null);
		}

		public class ActivityHandle<TResult> : ActivityHandle
		{
				public new async Task<TResult> GetResultAsync(
				    ActivityGetResultOptions? options = null);
		}

		public class ActivityOptions : ICloneable
		{
				public ActivityOptions();
				public ActivityOptions(string id, string taskQueue);

				public string? Id { get; set; } // required
		    public string? TaskQueue { get; set; } // required

		    public TimeSpan? ScheduleToCloseTimeout { get; set; }
				public TimeSpan? ScheduleToStartTimeout { get; set; }
		    public TimeSpan? StartToCloseTimeout { get; set; }
		    public TimeSpan? HeartbeatTimeout { get; set; }
		    public RetryPolicy? RetryPolicy { get; set; }
		    public string? Summary { get; set; }
			  public Priority? Priority { get; set; }
		    public SearchAttributeCollection? SearchAttributes { get; set; }
			  public ActivityIdReusePolicy IdReusePolicy { get; set; } =
				    ActivityIdReusePolicy.AllowDuplicate; // imported from proto
		    public ActivityIdConflictPolicy IdConflictPolicy { get; set; } =
				    ActivityIdConflictPolicy.Fail; // imported from proto

        public RpcOptions? Rpc { get; set; }

		    public virtual object Clone();
		}

		public class ActivityGetResultOptions : ICloneable
		{
				public RpcOptions? Rpc { get; set; }
				public virtual object Clone();
		}

		public class ActivityDescribeOptions : ICloneable
		{
				public RpcOptions? Rpc { get; set; }
				public virtual object Clone();
		}

		public class ActivityCancelOptions : ICloneable
		{
				public RpcOptions? Rpc { get; set; }
				public virtual object Clone();
		}

		public class ActivityTerminateOptions : ICloneable
		{
				public RpcOptions? Rpc { get; set; }
				public virtual object Clone();
		}

		public class ActivityListOptions : ICloneable
		{
				public RpcOptions? Rpc { get; set; }
				public virtual object Clone();
		}

		public class ActivityCountOptions : ICloneable
		{
				public RpcOptions? Rpc { get; set; }
				public virtual object Clone();
		}

		// Not a record so that there's a way to seamlessly add
		// lazy deserialization of future fields, e.g. memo.
		public class ActivityExecution
		{
				protected internal ActivityExecution();

				public Api.v1.ActivityExecutionListInfo? RawListInfo { get; internal init; }
				public string ActivityId { get; internal init; }
				public string ActivityRunId { get; internal init; }
				public string ActivityType { get; internal init; }
				public DateTime? ScheduleTime { get; internal init; }
				public DateTime? CloseTime { get; internal init; }
				public ActivityExecutionStatus Status { get; internal init; }
				public SearchAttributeCollection SearchAttributes { get; internal init; }
				public string TaskQueue { get; internal init; }
				public DateTime? ExecutionDuration { get; internal init; }
		}

		public class ActivityExecutionDescription {
				protected internal ActivityExecutionDescription();

				public Api.v1.ActivityExecutionInfo? RawInfo { get; internal init; }
				public DateTime? LastHeartbeatTime { get; internal init; }
				public DateTime? LastStartedTime { get; internal init; }
				public RetryPolicy? RetryPolicy { get; internal init; }
				public DateTime? ExpirationTime { get; internal init; }
				public string? LastWorkerIdentity { get; internal init; }
				public DateTime? CurrentRetryInterval { get; internal init; }
				public DateTime? LastAttemptCompleteTime { get; internal init; }
				public DateTime? NextAttemptScheduleTime { get; internal init; }
				public WorkerDeploymentVersion LastDeploymentVersion { get; internal init; }
				public Priority? Priority { get; internal init; }
				public string? CanceledReason { get; internal init; }
		}

		public record ActivityExecutionCount(
				long Count,
				IReadOnlyCollection<ActivityExecutionCount.AggregationGroup> Groups)
    {
        public record AggregationGroup(
		        long Count,
		        IReadOnlyCollection<object> GroupValues);
    }
}

namespace Temporalio.Client.Interceptors
{
    public record StartActivityInput(
        string Activity,
        IReadOnlyCollection<object?> Args,
        ActivityOptions Options,
        IDictionary<string, Payload>? Headers);

    public record StartActivityOutput(
        string RunId);

    public record GetActivityResultInput(
        string ActivityId,
        string? ActivityRunId,
        ActivityGetResultOptions Options);

    public record DescribeActivityInput(
        string ActivityId,
        string? ActivityRunId,
        ActivityDescribeOptions Options);

    public record CancelActivityInput(
        string ActivityId,
        string? ActivityRunId,
        string? Reason,
        ActivityCancelOptions Options);

    public record TerminateActivityInput(
        string ActivityId,
        string? ActivityRunId,
        string? Reason,
        ActivityTerminateOptions Options);

    public record ListActivitiesInput(
        string query,
        ActivityListOptions Options);

    public record CountActivitiesInput(
        string query,
        ActivityCountOptions Options);
}
```

## 3. Changes to `Converters.ISerializationContext`

The following interface definitions replace the existing definitions in backward-compatible way.

```csharp
namespace Temporalio.Converters
{
    public interface ISerializationContext
    {
		    public interface IHasWorkflow
        {
		        string Namespace { get; }
            string WorkflowId { get; }
        }

				public sealed record Activity(
            string Namespace,
            string WorkflowId,
            string WorkflowType,
            string ActivityType,
            string ActivityTaskQueue,
            bool IsLocal) : IHasWorkflow
        {
		        // Throws if info is not from a workflow activity
            public Activity(Activities.ActivityInfo info);
        }

        public sealed record Workflow(
            string Namespace,
            string WorkflowId) : IHasWorkflow;

        public sealed record NonWorkflowActivity(
            string Namespace,
            string ActivityId,
            string ActivityType,
            string ActivityTaskQueue)
        {
		        // Throws if info is from a workflow activity
            public NonWorkflowActivity(Activities.ActivityInfo info);
        }
    }
}
```

## 4. Other changes to existing types

A. `Activities.ActivityInfo`

```csharp
// new record fields

string? ActivityRunId, // null if in workflow
string Namespace,

// changed record fields

string? WorkflowId, // null if standalone
string? WorkflowNamespace, // deprecated, null if standalone
string? WorkflowRunId, // null if standalone
string? WorkflowType, // null if standalone

// new calculated property

bool IsInWorkflow => WorkflowId is not null;
```

B. `Client.AsyncActivityHandle.IdReference`

```csharp
public record IdReference(
    string? WorkflowId, // null if standalone
    string? RunId, // either workflow run ID or activity run ID or null
    string ActivityId) : Reference;
```

C. `Client.Interceptors.ClientOutboundInterceptor`

New methods:

```csharp
public virtual Task<StartActivityOutput> StartActivityAsync(
		StartActivityInput input);

public virtual Task<Payload> GetActivityResultAsync<TResult>(
		GetActivityResultInput input);

public virtual Task<ActivityExecutionDescription> AsyncDescribeActivity(
		DescribeActivityInput input);

public virtual Task CancelActivityAsync(CancelActivityInput input);

public virtual Task TerminateActivityAsync(TerminateActivityInput input);

public virtual Task ListActivitiesAsync(StartActivityInput input);

#if NETCOREAPP3_0_OR_GREATER
public IAsyncEnumerable<ActivityExecution> ListActivitiesAsync(
		ListActivitiesInput input);
#endif

public virtual Task<ActivityExecutionCount> CountActivitiesAsync(
		CountActivitiesInput input);
```

# Go

## 1. New types in `client` module

```go
import activitypb "go.temporal.io/api/activity/v1"

type (
		ActivityHandle interface {
		    ActivityID() string
		    ActivityRunID() string // can be empty
		    Get(ctx context.Context, valuePtr any) error

		    Describe(
		        ctx     context.Context,
		        options DescribeActivityOptions
        ) (ActivityExecutionDescription, error)

        Cancel(
						ctx     context.Context,
						options CancelActivityOptions
				) error

				Terminate(
						ctx     context.Context,
						options TerminateActivityOptions
				) error
		}

		StartActivityOptions struct {
		    ID                     string
		    TaskQueue              string
		    ScheduleToCloseTimeout time.Duration
		    ScheduleToStartTimeout time.Duration
		    StartToCloseTimeout    time.Duration
		    HeartbeatTimeout       time.Duration
		    IDConflictPolicy       enumspb.ActivityIdConflictPolicy
		    IDReusePolicy          enumspb.ActivityIdReusePolicy
		    RetryPolicy            *RetryPolicy
		    SearchAttributes       SearchAttributes
		    Summary                string
		    Priority               Priority
		}

		DescribeActivityOptions struct {} // for future compatibility

		CancelActivityOptions struct {
		    Reason string
		}

		TerminateActivityOptions struct {
		    Reason string
		}

		ActivityExecutionMetadata struct {
				// nil if part of ActivityExecutionDescription
		    RawExecutionListInfo    *activitypb.ActivityExecutionListInfo
				ActivityID              string
				ActivityRunID           string
				ActivityType            string
				ScheduledTime           time.Duration
				CloseTime               time.Duration
				Status                  enumspb.ActivityExecutionStatus
				SearchAttributes        SearchAttributes
				TaskQueue               string
				HasStateTransitionCount bool
				StateTransitionCount    int64
				StateSizeBytes          int64
				ExecutionDuration       time.Duration
		}

		ActivityExecutionDescription struct {
				ActivityExecutionMetadata
				RawExecutionInfo        *activitypb.ActivityExecutionInfo
				RunState                enumspb.PendingActivityState
				LastHeartbeatTime       time.Duration
				LastStartedTime         time.Duration
				Attempt                 int
				RetryPolicy             *RetryPolicy
				ExpirationTime          time.Duration
				LastWorkerIdentity      string
				CurrentRetryInterval    time.Duration
				LastAttemptCompleteTime time.Duration
				NextAttemptScheduleTime time.Duration
				LastDeploymentVersion   WorkerDeploymentVersion
				Priority                Priority
				EagerExecutionRequested bool
				CanceledReason          string
				dc                      converter.DataConverter
		}
)

// valuePtr must be a pointer to an array
func (a *ActivityExecutionDescription) HeartbeatDetails(valuePtr any) error

func (a *ActivityExecutionDescription) LastFailure() error
func (a *ActivityExecutionDescription) HeaderReader() HeaderReader
func (a *ActivityExecutionDescription) Summary() (string, error)

type (
		ListActivitiesOptions struct {
		    Query string
		}

		CountActivitiesOptions struct {
		    Query string
		}

		CountActivitiesResult struct {
		    Count  int64
		    Groups []ActivityAggregationGroup
		}

		ActivityAggregationGroup struct {
		    GroupValues []any
		    Count       int64
		}
)
`
```

## 2. Changes to `client.Client` interface

```go
// New methods

ExecuteActivity(
    ctx      context.Context,
    options  StartActivityOptions,
    activity any,
    args     ...any
) (ActivityHandle, error)

GetActivityHandle(
    activityID    string,
    activityRunID string // can be empty
) (ActivityHandle, error)

ListActivities(
		ctx     context.Context,
		options ListActivitiesOptions
) iter.Seq2[*ActivityExecutionMetadata, error]

CountActivities(
		ctx context.Context,
		options CountActivitiesOptions
) (*CountActivitiesResult, error)

// Semantic-only changes

// workflowID can be empty. If it's empty, then activityID refers to
// non-workflow activity, and runID refers to activity run ID.
CompleteActivityByID(
    ctx context.Context,
    namespace  string,
    workflowID string,
    runID      string,
    activityID string,
    result     interface{},
    err        error
) error

// workflowID can be empty. If it's empty, then activityID refers to
// non-workflow activity, and runID refers to activity run ID.
RecordActivityHeartbeatByID(
    ctx        context.Context,
    namespace  string,
    workflowID string,
    runID      string,
    activityID string,
    details    ...interface{}
) error
```

## 3. Changes to `activity.Info`  type

```go
// New fields

ActivityRunID string // empty if in workflow
InWorkflow    bool
Namespace     string

// Deprecated fields

WorkflowNamespace string // empty if standalone

// Semantic-only changes

WorkflowExecution WorkflowExecution // both ID and RunID empty if standalone
WorkflowType      *WorkflowType // nil if standalone
```

## 4. Changes to `testsuite` module

```go
// New method:
// SetRunActivitiesInWorkflow sets how activities are run in test environment.
// If set to true, activities are run inside a fake workflow.
// If set to false, activities are run without a workflow.
// Defaults to true.
func (t *TestActivityEnvironment) SetRunActivitiesInWorkflow(
		runActivitiesInWorkflow bool
) t *TestActivityEnvironment

// Documentation change: panics if SetRunActivitiesInWorkflow is set to false
func (t *TestActivityEnvironmentNoWorkflow) ExecuteLocalActivity(
		activityFn interface{}, args ...interface{}
) (converter.EncodedValue, error)
```

## 5. Changes to `interceptor` module

A. New methods in `ClientOutboundInterceptor` interface

```go
ExecuteActivity(
		context.Context, *ClientExecuteActivityInput
) (ActivityHandle, error)

GetActivityResult(
		context.Context, *ClientGetActivityResultInput
) error

DescribeActivity(
		context.Context, *ClientDescribeActivityInput
) (ActivityExecutionDescription, error)

CancelActivity(
		context.Context, *ClientCancelActivityInput
) error

TerminateActivity(
		context.Context, *ClientTerminateActivityInput
) error

ListActivities(
		context.Context, *ClientListActivitiesInput
) iter.Seq2[ActivityExecutionMetadata, error]

CountActivities(
		context.Context, *ClientCountActivitiesInput
) (CountActivitiesResult, error)
```

B. New types

```go
type (
		ClientExecuteActivityInput struct {
		    Options      *StartActivityOptions
		    ActivityType string
		    Args         []any
		}

		ClientGetActivityResultInput struct {
		    ActivityID    string
		    ActivityRunID string
		    valuePtr      any
		}

		ClientDescribeActivityInput struct {
		    ActivityID    string
		    ActivityRunID string
		    Options       *DescribeActivityOptions
		}

		ClientCancelActivityInput struct {
		    ActivityID    string
		    ActivityRunID string
				Options       *CancelActivityOptions
		}

		ClientTerminateActivityInput struct {
		    ActivityID    string
		    ActivityRunID string
				Options       *TerminateActivityOptions
		}

		ClientListActivitiesInput struct {
		    ActivityID    string
		    ActivityRunID string
				Options       *ListActivitiesOptions
		}

		ClientCountActivitiesInput struct {

		    ActivityID    string
		    ActivityRunID string
				Options       *CountActivitiesOptions
		}
)
```

# Java

## 1. New types

- A. `io.temporal.client`

    ```java
    /*
    Example use:

    @ActivityInterface
    interface MyActivity {
      @ActivityMethod
      String activity(int a, int b);
    }

    WorkflowClient client = ...;
    ActivityOptions options = ...;

    // sync execution
    String result = client.newActivityClient().execute(
    	MyActivity.class, MyActivity::activity, options, 1, 2);

    // async execution
    ActivityHandle<String> handle = client.newActivityClient().start(
    	MyActivity.class, MyActivity::activity, options, 1, 2);
    String result = handle.getResult();

    */
    public interface ActivityClient {
    	/// Obtains untyped handle to existing activity execution.
    	UntypedActivityHandle getHandle(
    			String activityId,
    			@Nullable String activityRunId);

    	/// Obtains typed handle to existing activity execution.
    	<R> ActivityHandle<R> getHandle(
    			String activityId,
    			@Nullable String activityRunId,
    			Class<R> resultClass);

    	/// Obtains typed handle to existing activity execution.
    	/// For use with generic return types.
    	<R> ActivityHandle<R> getHandle(
    			String activityId,
    			@Nullable String activityRunId,
    			Class<R> resultClass,
    			@Nullable Type resultType);

    	/// Asynchronously starts activity.
    	UntypedActivityHandle start(
    			String activity,
    			ActivityOptions options,
    			@Nullable Object... args);

    	<R> ActivityHandle<R> start(
    			String activity,
    			Class<R> resultClass,
    			ActivityOptions options,
    			@Nullable Object... args);

    	<R> ActivityHandle<R> start(
    			String activity,
    			Class<R> resultClass,
    			Type resultType,
    			ActivityOptions options,
    			@Nullable Object... args);

    	<I> ActivityHandle<Void> start(
    			Class<I> activityInterface,
    			Functions.Proc1<I> activity,
    			ActivityOptions options);

    	<I, A1> ActivityHandle<Void> start(
    			Class<I> activityInterface,
    			Functions.Proc2<I, A1> activity,
    			ActivityOptions options,
    			A1 arg1);

    	<I, R> ActivityHandle<R> start(
    			Class<I> activityInterface,
    			Functions.Func1<I, R> activity,
    			ActivityOptions options);

    	<I, A1, R> ActivityHandle<R> start(
    			Class<I> activityInterface,
    			Functions.Func2<I, A1, R> activity,
    			ActivityOptions options,
    			A1 arg1);

    	/// Synchronously executes activity. Ignores result.
    	void execute(
    			String activity,
    			ActivityOptions options,
    			@Nullable Object... args);

    	/// Synchronously executes activity.
    	<R> R execute(
    			String activity,
    			Class<R> resultClass,
    			ActivityOptions options,
    			@Nullable Object... args);

    	<R> R execute(
    			String activity,
    			Class<R> resultClass,
    			Type resultType,
    			ActivityOptions options,
    			@Nullable Object... args);

    	<I> void execute(
    			Class<I> activityInterface,
    			Functions.Proc1<I> activity,
    			ActivityOptions options);

    	<I, A1> void execute(
    			Class<I> activityInterface,
    			Functions.Proc2<I, A1> activity,
    			ActivityOptions options,
    			A1 arg1);

    	<I, R> R execute(
    			Class<I> activityInterface,
    			Functions.Func1<I, R> activity,
    			ActivityOptions options);

    	<I, A1, R> R execute(
    			Class<I> activityInterface,
    			Functions.Func2<I, A1, R> activity,
    			ActivityOptions options,
    			A1 arg1);

    	/// Asynchronously executes activity. Returns a void future (ignores result).
    	CompletableFuture<Void> executeAsync(
    			String activity,
    			ActivityOptions options,
    			@Nullable Object... args);

    	/// Asynchronously executes activity. Returns a future with result.
    	<R> CompletableFuture<R> executeAsync(
    			String activity,
    			Class<R> resultClass,
    			ActivityOptions options,
    			@Nullable Object... args);

    	<R> CompletableFuture<R> executeAsync(
    			String activity,
    			Class<R> resultClass,
    			Type resultType,
    			ActivityOptions options,
    			@Nullable Object... args);

    	<I> CompletableFuture<Void> executeAsync(
    			Class<I> activityInterface,
    			Functions.Proc1<I> activity,
    			ActivityOptions options);

    	<I, A1> CompletableFuture<Void> executeAsync(
    			Class<I> activityInterface,
    			Class<I> activityInterface,
    			Functions.Proc2<I, A1> activity,
    			ActivityOptions options,
    			A1 arg1);

    	<I, R> CompletableFuture<R> executeAsync(
    			Class<I> activityInterface,
    			Functions.Func1<I, R> activity,
    			ActivityOptions options);

    	<I, A1, R> CompletableFuture<R> executeAsync(
    			Class<I> activityInterface,
    			Functions.Func2<I, A1, R> activity,
    			ActivityOptions options,
    			A1 arg1);

    	// Additional overloads of start, execute and executeAsync
    	// for Proc3...Proc7, Func3...Func7 (up to 6 activity arguments).



    	Stream<ActivityExecutionMetadata> listExecutions(String query);

    	ActivityExecutionCount countExecutions(String query);
    }

    public interface UntypedActivityHandle {
      String getActivityId();

    	/// Present if the handle was returned by `start` method
    	/// or if it was set when calling `getActivityHandle`.
    	/// Null if `getActivityHandle` was called with null run ID
    	/// - in that case, use `describe` to get current run ID.
    	@Nullable String getActivityRunId();

    	<R> R getResult(Class<R> resultClass);
    	<R> R getResult(Class<R> resultClass, @Nullable Type resultType);
    	<R> CompletableFuture<R> getResultAsync(Class<R> resultClass);
    	<R> CompletableFuture<R> getResultAsync(
    		Class<R> resultClass, @Nullable Type resultType);
    	ActivityExecutionDescription describe();
    	void cancel();
    	void cancel(@Nullable String reason);
    	void terminate();
    	void terminate(@Nullable String reason);
    }

    public interface ActivityHandle<R> extends UntypedActivityHandle {
    	public R getResult();
    	public CompletableFuture<R> getResultAsync();
    }

    public class ActivityOptions {
      private String id;
    	private String taskQueue;
    	private Duration scheduleToCloseTimeout;
    	private Duration scheduleToStartTimeout;
    	private Duration startToCloseTimeout;
    	private Duration heartbeatTimeout;
    	private RetryOptions retryOptions;
    	private String summary;
    	private Priority priority;
    	private SearchAttributes searchAttributes;
    	private ActivityIdReusePolicy idReusePolicy;
    	private ActivityIdConflictPolicy idConflictPolicy;

    	// + public getter for each field

    	private ActivityOptions(...);

    	public Builder newBuilder();

    	public static class Builder {
    		// setter for each field

    		public ActivityOptions build();
    	}
    }

    public class ActivityExecutionMetadata {
    	public ActivityExecutionMetadata(
    			@Nullable ActivityExecutionListInfo info,
    			DataConverter dataConverter /* for future compatibility */);

    	@Nullable
    	public ActivityExecutionListInfo getRawListInfo();

    	public String getActivityId();
    	public String getActivityRunId();
    	public String getActivityType();
    	public Instant getScheduledTime();
    	public Instant getCloseTime();
    	public SearchAttributes getSearchAttributes();
    	public String getTaskQueue();
    	public Instant getExecutionDuration();
    }

    public class ActivityExecutionDescription extends ActivityExecutionMetadata {
    	public ActivityExecutionDescription(
    			@Nonnull ActivityExecutionInfo info,
    			DataConverter dataConverter /* for future compatibility */) {
    			super(null, dataConverter);
    			...
    	}

    	@Nonnull
    	public ActivityExecutionInfo getRawInfo();

    	@Override
    	public String getActivityId();
    	@Override
    	public String getActivityRunId();
    	@Override
    	public String getActivityType();
    	@Override
    	public Instant getScheduledTime();
    	@Override
    	public Instant getCloseTime();
    	@Override
    	public SearchAttributes getSearchAttributes();
    	@Override
    	public String getTaskQueue();
    	@Override
    	public Instant getExecutionDuration();

    	public Instant getLastHeartbeatTime();
    	public Instant getLastStartedTime();
    	public RetryOptions getRetryOptions();
    	public Instant getExpirationTime();
    	public String getLastWorkerIdentity();
    	public Duration getCurrentRetryInterval();
    	public Instant getLastAttemptCompleteTime();
    	public Instant getNextAttemptScheduleTime();
    	public WorkerDeploymentVersion getWorkerDeploymentVersion();
    	public Priority getPriority();
    	public String getCanceledReason();
    }

    public class ActivityExecutonCount {
    	public ActivityExecutonCount(
    			long count, List<AggregationGroup> groups);

    	public long getCount();

    	/// Returns unmodifiable list.
    	public List<AggregationGroup> getGroups();

    	public static class AggregationGroup {
    		public AggregationGroup(long count, List<?> groupValues);

    		public long getCount();

    		/// Returns unmodifiable list.
    		public List<?> getGroupValues();
    	}
    }
    ```


B. `io.temporal.workflow.Functions`

```java
@FunctionalInterface
public interface Proc7<T1, T2, T3, T4, T5, T6, T7>
    extends TemporalFunctionalInterfaceMarker, Serializable {
  void apply(T1 t1, T2 t2, T3 t3, T4 t4, T5 t5, T6 t6, T7 t7);
}

@FunctionalInterface
public interface Func7<T1, T2, T3, T4, T5, T6, T7, R>
    extends TemporalFunctionalInterfaceMarker, Serializable {
  R apply(T1 t1, T2 t2, T3 t3, T4 t4, T5 t5, T6 t6, T7 t7);
}
```

C. `io.temporal.common.interceptors`

```java
public interface ActivityClientCallsInterceptor {
	UntypedActivityHandle start(ActivityStartInput input);
	Payload getResult(ActivityGetResultInput input);
	ActivityExecutionDescription describe(ActivityDescribeInput input);
	void cancel(ActivityCancelInput input);
	void terminate(ActivityTerminateInput input);
	ActivityExecutonCount count(ActivityCountInput input);

	static class ActivityStartInput {
		public ActivityStartInput(
        @Nonnull String activityId,
        @Nonnull String activityType,
        @Nonnull client.ActivityOptions options,
        @Nonnull Object[] arguments,
        @Nonnull Header header);

     public String getActivityId();
     public String getActivityType();
     public client.ActivityOptions getOptions();
     public Object[] getArguments();
     public Header getHeader();
	}

	static class ActivityGetResultInput {
		public ActivityGetResultInput(
      @Nonnull String activityId,
      @Nullable String activityRunId);

    public String getActivityId();
    public String getActivityRunId();
	}

	static class ActivityDescribeInput {
		public ActivityDescribeInput(
      @Nonnull String activityId,
      @Nullable String activityRunId);

    public String getActivityId();
    public String getActivityRunId();
	}

	static class ActivityCancelInput {
		public ActivityCancelInput(
      @Nonnull String activityId,
      @Nullable String activityRunId,
      @Nullable String reason);

    public String getActivityId();
    public String getActivityRunId();
    public @Nullable String getReason();
	}

	static class ActivityTerminateInput {
		public ActivityTerminateInput(
      @Nonnull String activityId,
      @Nullable String activityRunId,
      @Nullable String reason);

    public String getActivityId();
    public String getActivityRunId();
    public @Nullable String getReason();
	}

	static class ActivityCountInput {
		public ActivityCountInput(
      @Nonnull String query);

    public String getQuery();
	}
}

public class ActivityClientCallsInterceptorBase
		implements ActivityClientCallsInterceptor {

	protected final WorkflowClientCallsInterceptor next;

	public ActivityClientCallsInterceptorBase(
			ActivityClientCallsInterceptor next);

  @Override
  public UntypedActivityHandle start(ActivityStartInput input) {
    return next.start(input);
  }

  // etc. for other methods
}
```

## 2. Changes to existing types

A. `io.temporal.client.WorkflowClient`

```java
// New method

ActivityClient newActivityClient();
```

B. `io.temporal.activity.ActivityInfo`

```java
// New methods

@Nullable String getActivityRunId();
@Nullable String getWorkflowRunId();
boolean isInWorkflow();

// Changed methods

@Deprecated @Nullable String getRunId(); // replaced by getWorkflowRunId
@Nullable String getWorkflowId();
@Nullable String getWorkflowType();

```

C. `io.temporal.activity.ActivityOptions`

- Documentation change: options for workflow activities only.

D. `io.temporal.client.ActivityCompletionClient`

```java
// New methods

<R> void complete(
		String activityId,
		Optional<String> activityRunId,
		R result)
		throws ActivityCompletionException;

void completeExceptionally(
		String activityId,
		Optional<String> activityRunId,
		Exception result)
		throws ActivityCompletionException;

<V> void reportCancellation(
		String activityId,
		Optional<String> activityRunId,
		V details)
		throws ActivityCompletionException;

<V> void heartbeat(
		String activityId,
		Optional<String> activityRunId,
		V details)
		throws ActivityCompletionException;
```

E. `io.temporal.common.interceptors.WorkflowClientInterceptor`

```java
// New method

ActivityClientCallsInterceptor activityClientCallsInterceptor(
		ActivityClientCallsInterceptor next);
```

## 3. Changes to `io.temporal.payload.context`

1. In `HasWorkflowSerializationContext`, field `workflowId` is made nullable.
2. In `ActivitySerializationContext`, fields `workflowId` and `workflowType` are made nullable.

# Ruby

The design is written in terms of signature files (.rbs).

## 1. New types

A. `Temporalio`

```ruby
module ActivityIDReusePolicy
	type enum = Integer
  # maps to Api::Enums::V1::ActivityIdReusePolicy
end

module ActivityIDConflictPolicy
	type enum = Integer
  # maps to Api::Enums::V1::ActivityIdConflictPolicy
end
```

B. `Temporalio.Client`

```ruby
class ActivityHandle
  attr_reader activity_id: String
  attr_reader activity_run_id: String?
  attr_reader result_hint: Object?

  def initialize: (
    client: Client,
    activity_id: String,
    activity_run_id: String?,
    result_hint: Object?
  ) -> void

  def result: (
    ?rpc_options: RPCOptions?
  ) -> Object?

  def describe: (
    ?rpc_options: RPCOptions?
  ) -> ActivityExecution::Description

  def cancel: (
    ?String? reason,
    ?rpc_options: RPCOptions?
  ) -> void

  def terminate: (
    ?String? reason,
    ?rpc_options: RPCOptions?
  ) -> void
end

class ActivityExecution
	attr_reader raw: untyped

  def initialize: (
	  untyped raw,
	  Converters::DataConverter data_converter # for future compatibility
  ) -> void

	def activity_id: -> String
	def activity_type: -> String
	def activity_run_id: -> String
	def close_time: -> Time
	def execution_duration: -> duration
	def namespace: -> String # not present in proto, copied from client
	def scheduled_time: -> Time
	def search_attributes: -> SearchAttributes
	def status: -> String
	def task_queue: -> String

	class Description < ActivityExecution
	  def initialize: (
		  untyped raw,
		  Converters::DataConverter data_converter # for future compatibility
	  ) -> void

	  def attempt: -> Integer
		def canceled_reason: -> String?
		def current_retry_interval: -> duration?
		def eager_execution_requested?: bool
		def last_attempt_complete_time: -> Time?
		def last_heartbeat_time: -> Time?
		def last_started_time: -> Time?
		def last_worker_identity: -> String?
		def retry_policy: -> RetryPolicy?
		def next_attempt_schedule_time: -> Time?
		def paused?: bool
	end
end

class ActivityExecutionCount
  attr_reader count: Integer
  attr_reader groups: Array[AggregationGroup]

  def initialize: (
	  Integer count,
	  Array[AggregationGroup] groups
  ) -> void

  class AggregationGroup
    attr_reader count: Integer
    attr_reader group_values: Array[Object?]

    def initialize: (
	    Integer count,
	    Array[Object?] group_values
    ) -> void
  end
end

module ActivityExecutionStatus
	type enum = Integer
  # maps to Api::Enums::V1::ActivityExecutionStatus
end
```

C. `Temporalio.Error`

```ruby
class ActivityAlreadyStartedError < Failure
  attr_reader activity_id: String
  attr_reader activity_type: String
  attr_reader activity_run_id: String?

  # @!visibility private
  def initialize(
	  activity_id: String,
	  activity_type: String,
	  activity_run_id: String?
  ) -> void
end
```

## 2. New methods in `Client`

```ruby
def start_activity(
	singleton(Activity::Definition) | Activity::Definition::Info
	| Symbol | String activity**,
	***Object? args,
	id: String,
	task_queue: String,
	?schedule_to_close_timeout: duration?,
	?schedule_to_start_timeout: duration?,
	?start_to_close_timeout: duration?,
	?heartbeat_timeout: duration?,
	?id_reuse_policy: ActivityIDReusePolicy # default ALLOW_DUPLICATE,
	?id_conflict_policy: ActivityIDConflictPolicy # default FAIL,
	?retry_policy: RetryPolicy?,
****	?search_attributes: SearchAttributes?,
	?summary: String?**,**
	?priority: Priority, # default Priority.default
  ?arg_hints: Array[Object]?,
  ?result_hint: Object?,
  ?rpc_options: RPCOptions?
) -> ActivityHandle

def execute_activity(
	singleton(Activity::Definition) | Activity::Definition::Info
	| Symbol | String activity**,
	***Object? args,
	id: String,
	task_queue: String,
	?schedule_to_close_timeout: duration?,
	?schedule_to_start_timeout: duration?,
	?start_to_close_timeout: duration?,
	?heartbeat_timeout: duration?,
	?id_reuse_policy: ActivityIDReusePolicy # default ALLOW_DUPLICATE,
	?id_conflict_policy: ActivityIDConflictPolicy # default FAIL,
	?retry_policy: RetryPolicy?,
****	?search_attributes: SearchAttributes?,
	?summary: String?**,**
	?priority: Priority, # default Priority.default
  ?arg_hints: Array[Object]?,
  ?result_hint: Object?,
  ?rpc_options: RPCOptions?
) -> Object?

def activity_handle(
	String activity_id,
	?activity_run_id: String?,
  ?result_hint: Object?
) -> ActivityHandle

def list_activities(
	String query,
	?rpc_options: RPCOptions?
) -> Enumerator[ActivityExecution, ActivityExecution]

def count_activities(
	String query,
	?rpc_options: RPCOptions?
) -> ActivityExecutionCount
```

## 3. Changes to other types

A. `Temporalio.Activity.Info`

```ruby
# New items

attr_reader activity_run_id: String? # nil if in workflow
attr_reader in_workflow?: bool
attr_reader namespace: String

# Changed items

attr_reader workflow_id: String? # nil if standalone
attr_reader workflow_run_id: String? # nil if standalone
attr_reader workflow_type: String? # nil if standalone

# Deprecated, nil if standalone
attr_reader workflow_namespace: String?
```

B. `Temporalio.Client.ActivityIDReference` - new definition.

```ruby
class ActivityIDReference
  attr_reader activity_id: String
  attr_reader activity_run_id: String?
  attr_reader workflow_id: String?
  attr_reader workflow_run_id: String?

  # either activity_run_id or workflow_run_id
  attr_reader run_id: String?

  def initialize: (
	  workflow_id: String,
	  run_id: String?,
	  activity_id: String
  ) -> void
  | (
    activity_id: String,
    activity_run_id: String?
  ) -> void
end
```

C. `Temporalio.Client.Interceptor`

```ruby
# New methods in OutboundInterceptor

def start_activity: (StartActivityInput input) -> ActivityHandle

def describe_activity: (
	DescribeActivityInput input
) -> ActivityExecution::Description

def cancel_activity: (CancelActivityInput input) -> void

def terminate_activity: (TerminateActivityInput input) -> void

def count_activities: (
	CountActivitiesInput input
) -> ActivityExecutionCount

# New types in Interceptor

class StartActivityInput
  attr_reader activity: String
  attr_reader args: Array[Object?]
  attr_reader activity_id: String
  attr_reader task_queue: String
  attr_reader schedule_to_close_timeout: duration?
  attr_reader schedule_to_start_timeout: duration?
  attr_reader start_to_close_timeout: duration?
  attr_reader heartbeat_timeout: duration?
  attr_reader id_reuse_policy: ActivityIDReusePolicy
  attr_reader id_conflict_policy: ActivityIDConflictPolicy
  attr_reader retry_policy: RetryPolicy?
  attr_reader search_attributes: SearchAttributes?
  attr_reader summary: String?
  attr_reader arg_hints: Array[Object]?
  attr_reader result_hint: Object?
  attr_reader rpc_options: RPCOptions?

  def initialize: (...) -> void
end

class DescribeActivityInput
  attr_reader activity_id: String
  attr_reader activity_run_id: String?
  attr_reader rpc_options: RPCOptions?

  def initialize: (...) -> void
end

class CancelActivityInput
  attr_reader activity_id: String
  attr_reader activity_run_id: String?
  attr_reader reason: String?
  attr_reader rpc_options: RPCOptions?

  def initialize: (...) -> void
end

class TerminateActivityInput
  attr_reader activity_id: String
  attr_reader activity_run_id: String?
  attr_reader reason: String?
  attr_reader rpc_options: RPCOptions?

  def initialize: (...) -> void
end

class CountActivitiesInput
  attr_reader query: String
  attr_reader rpc_options: RPCOptions?

  def initialize: (...) -> void
end
```

# Python

## 1. New types

`temporalio.common`

```python
# Maps to temporalio.api.enums.v1.ActivityIdReusePolicy
class ActivityIDReusePolicy(IntEnum):
    ...

# Maps to temporalio.api.enums.v1.ActivityIdConflictPolicy
class ActivityIDConflictPolicy(IntEnum):
    ...

# Maps to temporalio.api.enums.v1.ActivityExecutionStatus
class ActivityExecutionStatus(IntEnum):
    ...
```

`temporalio.client`

```python
class ActivityHandle(Generic[ReturnType]):
    @property
    def activity_id(self) -> str:
		    ...

    @property
    def activity_run_id(self) -> Option[str]:
		    ...

    async def result(
        self,
        *,
        rpc_metadata: Mapping[str, Union[str, bytes]] = {},
        rpc_timeout: Optional[timedelta] = None,
    ) -> ReturnType:
		    ...

    async def describe(
        self,
        *,
        rpc_metadata: Mapping[str, Union[str, bytes]] = {},
        rpc_timeout: Optional[timedelta] = None,
    ) -> ActivityExecutionDescription:
		    ...

    async def cancel(
        self,
        *,
        reason: Optional[str] = None,
        rpc_metadata: Mapping[str, Union[str, bytes]] = {},
        rpc_timeout: Optional[timedelta] = None,
    ) -> None:
		    ...

    async def terminate(
        self,
        *,
        reason: Optional[str] = None,
        rpc_metadata: Mapping[str, Union[str, bytes]] = {},
        rpc_timeout: Optional[timedelta] = None,
    ) -> None:
		    ...

@dataclass(frozen=True)
class ActivityExecution:
    ****activity_id: str
    ****activity_type: str
    ****activity_run_id: Optional[str]
    close_time: Optional[datetime]
    execution_duration: Optional[timedelta]
    namespace: str # not present in proto, copied from calling client
    raw_info: Union[
	    temporalio.api.activity.v1.ActivityExecutionListInfo,
	    temporalio.api.activity.v1.ActivityExecutionInfo
    ]
    scheduled_time: datetime
    search_attributes: temporalio.common.SearchAttributes
    state_transition_count: Optional[int] # not always present on List operation, see proto docs for details
    status: temporalio.common.ActivityExecutionStatus
    task_queue: str
****
@dataclass(frozen=True)
class ActivityExecutionDescription(ActivityExecution):
		attempt: int
    canceled_reason: Optional[str]
    current_retry_interval: Optional[timedelta]
    eager_execution_requested: bool
    expiration_time: datetime
    heartbeat_details: Sequence[Any]
    last_attempt_complete_time: Optional[datetime]
    last_failure: Optional[Exception]
    last_heartbeat_time: Optional[datetime]
    last_started_time: Optional[datetime]
    last_worker_identity: str
    retry_policy: Optional[temporalio.common.RetryPolicy]
    next_attempt_schedule_time: Optional[datetime]
    paused: bool
    run_state: Optional[temporalio.common.PendingActivityState]


class ActivityExecutionAsyncIterator:
    def __init__(
        self,
        client: Client,
        input: ListActivitiesInput,
    ) -> None:
		    ...

    @property
    def current_page_index(self) -> int:
		    ...

    @property
    def current_page(
        self,
    ) -> Optional[Sequence[ActivityExecution]]:
		    ...

    @property
    def next_page_token(self) -> Optional[bytes]:
		    ...

    async def fetch_next_page(
		    self, *, page_size: Optional[int] = None
    ) -> None:
		    ...

    def __aiter__(self) -> ActivityExecutionAsyncIterator:
		    ...

    async def __anext__(self) -> ActivityExecution:
		    ...

@dataclass
class ActivityExecutionCount:
    count: int
    groups: Sequence[ActivityExecutionCountAggregationGroup]

@dataclass
class ActivityExecutionCountAggregationGroup:
    count: int
    group_values: Sequence[temporalio.common.SearchAttributeValue]
```

## 2. New methods in `client.Client`

A. Start activity methods:

1. Same set of methods as for workflow activities:
`start_activity`, `start_activity_class`, `start_activity_method`, `execute_activity`, `execute_activity_class`, `execute_activity_method`
2. All `start_activity` methods are async and return `ActivityHandle[Any]`.
3. All `execute_activity` methods are async, wait for activity completion and return the result as `Any`.
4. All listed methods have overloads for specific activity types (`str`, various `Callable` types), same as workflow activities. For `Callable` overloads, a specific return type is used instead of `Any`.
5. All listed methods have the same set of arguments.

```python
# Bolded arguments are differences from the prototype.
async def start_activity(
    self,
    **activity: Any,**
    *,
    args: Sequence[Any] = [],
    # Note: workflow's start_activity() has activity_id argument instead of id.
    # The mismatch is intentional - because activity_id should never be set
    # except in rare circumstances, but this id should always have meaningful
    # value, giving them different names avoids potential copy-paste errors.
    id: str,
    task_queue: str,
    result_type: Optional[type] = None,
    schedule_to_close_timeout: Optional[timedelta] = None,
    schedule_to_start_timeout: Optional[timedelta] = None,
    start_to_close_timeout: Optional[timedelta] = None,
    heartbeat_timeout: Optional[timedelta] = None,
    id_reuse_policy: temporalio.common.ActivityIDReusePolicy = temporalio.common.ActivityIDReusePolicy.ALLOW_DUPLICATE,
    id_conflict_policy: temporalio.common.ActivityIDConflictPolicy = temporalio.common.ActivityIDConflictPolicy.FAIL,
    retry_policy: Optional[temporalio.common.RetryPolicy] = None,
    ****search_attributes: Optional[temporalio.common.TypedSearchAttributes] = None,
    **summary: Optional[str] = None,**
    # no static_details (matches workflow activity API)
    priority: temporalio.common.Priority = temporalio.common.Priority.default,
    rpc_metadata: Mapping[str, Union[str, bytes]] = {},
    rpc_timeout: Optional[timedelta] = None,
) -> temporalio.client.ActivityHandle[Any]:
		...
```

B. Other methods:

```python
def list_activities(
    self,
    query: Optional[str] = None,
    *,
    limit: Optional[int] = None,
    page_size: int = 1000,
    next_page_token: Optional[bytes] = None,
    rpc_metadata: Mapping[str, Union[str, bytes]] = {},
    rpc_timeout: Optional[timedelta] = None,
) -> ActivityExecutionAsyncIterator:
		...

async def count_activities(
    self,
    query: Optional[str] = None,
    *,
    rpc_metadata: Mapping[str, Union[str, bytes]] = {},
    rpc_timeout: Optional[timedelta] = None,
) -> ActivityExecutionCount:
		...

@overload
def get_activity_handle(
    self,
    activity_id: str,
    *
    activity_run_id: Optional[str] = None
) -> ActivityHandle[Any]:
		...

@overload
def get_activity_handle(
    self,
    activity_id: str,
    *
    result_type: type[ReturnType],
    activity_run_id: Optional[str] = None,
) -> ActivityHandle[ReturnType]:
		...

def get_activity_handle(
    self,
    activity_id: str,
    *
    result_type: Optional[type] = None,
    activity_run_id: Optional[str] = None
) -> ActivityHandle[ReturnType]:
		...
```

## 3. New methods in `client.OutboundInterceptor`

```python
async def start_activity(
		self, input: StartActivityInput
) -> ActivityHandle[Any]:
		...

async def get_activity_result(
    self, input: GetActivityResultInput[ReturnType]
) -> ReturnType:
    ...

async def describe_activity(
    self, input: DescribeActivityInput
) -> ActivityExecutionDescription:
    ...

async def cancel_activity(
		self, input: CancelActivityInput
) -> None:
		...

async def terminate_activity(
		self, input: TerminateActivityInput
) -> None:
		...

def list_activities(
    self, input: ListActivitiesInput
) -> ActivityExecutionAsyncIterator:
		...

async def count_activities(
    self, input: CountActivitiesInput
) -> ExecutionCount:
		...

@dataclass
class StartActivityInput:
    activity_type: str
    args: Sequence[Any]
    id: str
    task_queue: str
    result_type: Optional[type]
    schedule_to_close_timeout: Optional[timedelta]
    schedule_to_start_timeout: Optional[timedelta]
    start_to_close_timeout: Optional[timedelta]
    heartbeat_timeout: Optional[timedelta]
    id_reuse_policy: temporalio.common.ActivityIDReusePolicy
    id_conflict_policy: temporalio.common.ActivityIDConflictPolicy
    retry_policy: Optional[temporalio.common.RetryPolicy]
    search_attributes: Optional[temporalio.common.TypedSearchAttributes]
    summary: Optional[str]
    priority: temporalio.common.Priority
    headers: Mapping[str, temporalio.api.common.v1.Payload]
    rpc_metadata: Mapping[str, Union[str, bytes]]
    rpc_timeout: Optional[timedelta]

@dataclass
class DescribeActivityInput:
		activity_id: str
		activity_run_id: Optional[str]
    rpc_metadata: Mapping[str, Union[str, bytes]]
    rpc_timeout: Optional[timedelta]

@dataclass
class GetActivityResultInput[ReturnType]:
		activity_id: str
		activity_run_id: Optional[str]
		result_type: type[ReturnType]
    rpc_metadata: Mapping[str, Union[str, bytes]]
    rpc_timeout: Optional[timedelta]

@dataclass
class CancelActivityInput:
		activity_id: str
		activity_run_id: Optional[str]
    reason: Optional[str]
    wait_for_cancel_completed: bool
    rpc_metadata: Mapping[str, Union[str, bytes]]
    rpc_timeout: Optional[timedelta]

@dataclass
class TerminateActivityInput:
		activity_id: str
		activity_run_id: Optional[str]
    reason: Optional[str]
    rpc_metadata: Mapping[str, Union[str, bytes]]
    rpc_timeout: Optional[timedelta]

@dataclass
class ListActivitiesInput:
    query: Optional[str]
    limit: Optional[int]
    page_size: int
    next_page_token: Optional[bytes]
    rpc_metadata: Mapping[str, Union[str, bytes]]
    rpc_timeout: Optional[timedelta]

@dataclass
class CountActivitiesInput:
    query: Optional[str]
    rpc_metadata: Mapping[str, Union[str, bytes]]
    rpc_timeout: Optional[timedelta]
```

## 4. Other changes to existing types

A. `activity.Info`

```python
# new fields
namespace: str
activity_run_id: Optional[str] # None if in workflow

@property
def in_workflow(self) -> bool:
    return workflow_id is not None

# changed signature
workflow_id: Optional[str] # None if standalone
workflow_namespace: Optional[str] # DEPRECATED, None if standalone
workflow_run_id: Optional[str] # None if standalone
workflow_type: Optional[str] # None if standalone
```

B. `client.AsyncActivityIDReference`

```python
# changed signature
workflow_id: Optional[str]
```

## 5. Serialization context (TODO)

# TypeScript

## 1. New module `client.activity-client`

```tsx
/* Example use:

interface MyActivity {
  doWork(x: number): Promise<string>;
}

const connection = await Connection.connect({ address: 'localhost:7233' });
const client = new Client({ connection });
const opts = {
	taskQueue: 'task-queue',
	startToCloseTimeout: '1 minute'
};

// Typed activity start
const handle = await client.activity.typed<MyActivity>().start(
	'doWork', { id: 'activity1', ...opts }, 1
);
const result = await handle.result();

// Typed activity execution
const result = await client.activity.typed<MyActivity>().execute(
	'doWork', { id: 'activity2', ...opts }, 1
);

// Untyped activity execution
const result = await client.activity.execute(
	'doWork', { id: 'activity3', ...opts }, 'abc'
);

*/

import { AsyncCompletionClient, ActivityNotFoundError } from './async-completion-client'

export ActivityNotFoundError;
export type ActivityClientOptions = BaseClientOptions;
export type LoadedActivityClientOptions = LoadedWithDefaults<ActivityClientOptions>;

export class ActivityClient
	extends AsyncCompletionClient
	implements TypedActivityClient<UntypedActivities>
{
  public constructor(options?: ActivityClientOptions);

  public typed<T>(): TypedActivityClient<T> { return this; }

  public async start(
	  activity: string, options: ActivityOptions, ...args: any[]
  ): Promise<ActivityHandle>;

  public async execute(
	  activity: string, options: ActivityOptions, ...args: any[]
  ): Promise<any>;

  public getHandle(
	  activityId: string, activityRunId?: string
  ): ActivityHandle;

  public list(string query): AsyncIterable<ActivityExecutionInfo>;

  public async count(string query): Promise<CountActivitiesResult>;
}

// An instance of ActivityHandle is bound to the client it's created with.
export interface ActivityHandle<R> {
  readonly activityId: string;
  readonly activityRunId: string;
  result(): Promise<R>;
  describe(): Promise<ActivityDescription>;
  cancel(string reason): Promise<void>;
  terminate(string reason): Promise<void>;
}

export interface ActivityOptions {
	id: string;
  taskQueue: string;
  heartbeatTimeout?: Duration;
  retry?: RetryPolicy;
  startToCloseTimeout?: Duration;
  scheduleToStartTimeout?: Duration;
  scheduleToCloseTimeout?: Duration;
  summary?: string;
  priority?: Priority;
  idReusePolicy?: ActivityIdReusePolicy;
  idConflictPolicy?: ActivityIdConflictPolicy;
  typedSearchAttributes?: SearchAttributePair[] | TypedSearchAttributes;
}

export type ActivityKey<T> = {
  [K in keyof T & string]:
	  T[K] extends ActivityFunction<any, any> ? K : never;
}[keyof T & string];

export type ActivityArgs<T, K extends ActivityKey<T>> =
	T[K] extends ActivityFunction<infer P, any> ? P : never;

export type ActivityResult<T, K extends ActivityKey<T>> =
	T[K] extends ActivityFunction<any, infer R> ? R : never;

interface TypedActivityClient<T> {
  start<K extends ActivityKey<T>>(
	  activity: K,
	  options: ActivityOptions,
	  ...args: ActivityArgs<T, K>
  ): Promise<ActivityHandle<ActivityResult<T, K>>>;

  execute<K extends ActivityKey<T>>(
	  activity: K,
	  options: ActivityOptions,
	  ...args: ActivityArgs<T, K>
  ): Promise<ActivityResult<T, K>>;

```

## 2. New types in `client.types`

```tsx
export interface CountActivitiesResult {
  readonly count: number;
  readonly groups: {
    readonly count: number;
    readonly groupValues: TypedSearchAttributeValue<SearchAttributeType>[];
  }[];
}

export type RawActivityExecutionInfo = proto.temporal.api.activity.v1.IActivityExecutionInfo;
export type RawActivityExecutionListInfo = proto.temporal.api.activity.v1.IActivityExecutionListInfo;

export interface ActivityExecutionInfo {
	rawListInfo?: RawActivityExecutionListInfo;
	activityId: string;
	activityRunId: string;
	activityType: string;
	scheduledTime: Date;
	closeTime: Date;
	searchAttributes: TypedSearchAttributes;
	taskQueue: string;
	stateTransitionCount?: number;
	stateSizeBytes: number;
	executionDuration: Date;
}

export interface ActivityExecutionDescription extends ActivityExecutionInfo {
	rawInfo: RawActivityExecutionInfo;
	lastHeartbeatTime: Date;
	lastStartedTime: Date;
	attempt: number;
	retryPolicy: RetryPolicy;
	expirationTime: Date;
	lastWorkerIdentity: string;
	currentRetryInterval: Date;
	lastAttemptCompleteTime: Date;
	nextAttemptScheduleTime: Date;
	lastDeploymentVersion: WorkerDeploymentVersion;
	priority: Priority;
  eagerExecutionRequested: bool;
  canceledReason: string;
};

// Maps to temporal.api.enums.v1.ActivityIdReusePolicy
export const ActivityIdReusePolicy { ... }
export type ActivityIdReusePolicy =
	(typeof ActivityIdReusePolicy)[keyof typeof ActivityIdReusePolicy];
export const [encodeActivityIdReusePolicy, decodeActivityIdReusePolicy] =
	makeProtoEnumConverters<...>(...);

// Maps to temporal.api.enums.v1.ActivityIdConflictPolicy
export const ActivityIdConflictPolicy { ... }
export type ActivityIdConflictPolicy =
	(typeof ActivityIdConflictPolicy)[keyof typeof ActivityIdConflictPolicy];
export const [encodeActivityIdConflictPolicy, decodeActivityIdConflictPolicy] =
	makeProtoEnumConverters<...>(...);

// Maps to temporal.api.enums.v1.ActivityExecutionStatus
export const ActivityExecutionStatus { ... }
export type ActivityExecutionStatus =
	(typeof ActivityExecutionStatus)[keyof typeof ActivityExecutionStatus];
export const [encodeActivityExecutionStatus, decodeActivityExecutionStatus] =
	makeProtoEnumConverters<...>(...);

```

## 3. Changes to `client.interceptors`

A. `ClientInterceptors` has new field `activity?: ActivityClientInterceptor[];`.

B. New types:

```tsx
export interface ActivityClientInterceptor {
  start?: (
	  input: ActivityStartInput, next: Next<this, 'start'>
	) => Promise<ActivityHandle>;

  getResult?: (
	  input: ActivityGetResultInput, next: Next<this, 'getResult'>
	) => Promise<any>;

  describe?: (
	  input: ActivityDescribeInput, next: Next<this, 'describe'>
	) => Promise<ActivityExecutionDescription>;

  cancel?: (
	  input: ActivityCancelInput, next: Next<this, 'cancel'>
	) => Promise<void>;

  terminate?: (
	  input: ActivityTerminateInput, next: Next<this, 'terminate'>
	) => Promise<void>;

  list?: (
	  input: ActivityListInput, next: Next<this, 'list'>
	) => AsyncIterable<ActivityExecutionInfo>;

  count?: (
	  input: ActivityCountInput, next: Next<this, 'count'>
	) => Promise<CountActivitiesResult>;
}

export interface ActivityStartInput {
	readonly activityType: string;
	readonly args: any[];
	readonly options: ActivityOptions;
	readonly headers: Headers;
}

export interface ActivityGetResultInput {
	readonly activityId: string;
	readonly activityRunId: string;
	readonly headers: Headers;
}

export interface ActivityDescribeInput {
	readonly activityId: string;
	readonly activityRunId: string;
	readonly headers: Headers;
}

export interface ActivityCancelInput {
	readonly activityId: string;
	readonly activityRunId: string;
	readonly reason: string;
	readonly headers: Headers;
}

export interface ActivityTerminateInput {
	readonly activityId: string;
	readonly activityRunId: string;
	readonly reason: string;
	readonly headers: Headers;
}

export interface ActivityListInput {
	readonly query: string;
	readonly headers: Headers;
}

export interface ActivityCountInput {
	readonly query: string;
	readonly headers: Headers;
}
```

## 4. Other changes to existing types

A. `activity.Info`

```tsx
// new fields

readonly namespace: string;
readonly activityRunId?: string; // undefined if in workflow
readonly inWorkflow: boolean; // calculated property: false if workflowExecution is undefined

// changed fields

readonly activityNamespace: string // deprecated, same as namespace
readonly workflowNamespace?: string // deprecated, undefined if standalone
readonly workflowExecution?: interface { ... } // undefined if standalone
readonly workflowType?: string // undefined if standalone
```

B. `client.async-completion-client.FullActivityId`

```tsx
export interface FullActivityId {
  workflowId?: string; // undefined if standalone
  runId?: string; // either workflow run ID or activity run ID
  activityId: string;
}
```

C. `common.activity-options.ActivityOptions`

- Documentation change: options for starting an activity in workflow.
    - Open questions:
        - Should the package be moved to `workflow.activity-options` and the old one deprecated?
        - Should `client.activity-client.ActivityOptions` have its own package, or moved to `client.types`?