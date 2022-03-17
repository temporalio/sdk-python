package main

import (
	"encoding/json"
	"fmt"
	"log"
	"os"
	"time"

	"go.temporal.io/sdk/client"
	"go.temporal.io/sdk/temporal"
	"go.temporal.io/sdk/worker"
	"go.temporal.io/sdk/workflow"
)

func main() {
	if len(os.Args) != 4 {
		log.Fatalf("expected endpoint, namespace, and task queue arg, found %v args", len(os.Args)-1)
	}
	if err := run(os.Args[1], os.Args[2], os.Args[3]); err != nil {
		log.Fatal(err)
	}
}

func run(endpoint, namespace, taskQueue string) error {
	log.Printf("Creating client to %v", endpoint)
	cl, err := client.NewClient(client.Options{HostPort: endpoint, Namespace: namespace})
	if err != nil {
		return fmt.Errorf("failed to create client: %w", err)
	}
	defer cl.Close()

	log.Printf("Creating worker")
	w := worker.New(cl, taskQueue, worker.Options{})
	w.RegisterWorkflowWithOptions(KitchenSinkWorkflow, workflow.RegisterOptions{Name: "kitchen_sink"})
	defer log.Printf("Stopping worker")
	return w.Run(worker.InterruptCh())
}

type KitchenSinkWorkflowParams struct {
	Actions      []*KitchenSinkAction `json:"actions"`
	ActionSignal string               `json:"action_signal"`
}

type KitchenSinkAction struct {
	Result          *ResultAction          `json:"result"`
	Error           *ErrorAction           `json:"error"`
	ContinueAsNew   *ContinueAsNewAction   `json:"continue_as_new"`
	Sleep           *SleepAction           `json:"sleep"`
	QueryHandler    *QueryHandlerAction    `json:"query_handler"`
	Signal          *SignalAction          `json:"signal"`
	ExecuteActivity *ExecuteActivityAction `json:"execute_activity"`
}

type ResultAction struct {
	Value interface{} `json:"value"`
	RunID bool        `json:"run_id"`
}

type ErrorAction struct {
	Message string      `json:"message"`
	Details interface{} `json:"details"`
	Attempt bool        `json:"attempt"`
}

type ContinueAsNewAction struct {
	WhileAboveZero int `json:"while_above_zero"`
}

type SleepAction struct {
	Millis int64 `json:"millis"`
}

type QueryHandlerAction struct {
	Name string `json:"name"`
}

type SignalAction struct {
	Name string `json:"name"`
}

type ExecuteActivityAction struct {
	Name                     string        `json:"name"`
	TaskQueue                string        `json:"task_queue"`
	Args                     []interface{} `json:"args"`
	Count                    int           `json:"count"` // 0 same as 1
	IndexAsArg               bool          `json:"index_as_arg"`
	ScheduleToCloseTimeoutMS int64         `json:"schedule_to_close_timeout_ms"`
	StartToCloseTimeoutMS    int64         `json:"start_to_close_timeout_ms"`
	ScheduleToStartTimeoutMS int64         `json:"schedule_to_start_timeout_ms"`
	CancelAfterMS            int64         `json:"cancel_after_ms"`
	WaitForCancellation      bool          `json:"wait_for_cancellation"`
	HeartbeatTimeoutMS       int64         `json:"heartbeat_timeout_ms"`
	RetryMaxAttempts         int           `json:"retry_max_attempts"` // 0 same as 1
	NonRetryableErrorTypes   []string      `json:"non_retryable_error_types"`
}

func KitchenSinkWorkflow(ctx workflow.Context, params *KitchenSinkWorkflowParams) (interface{}, error) {
	b, _ := json.Marshal(params)
	workflow.GetLogger(ctx).Info("Started kitchen sink workflow", "params", string(b))

	// Handle all initial actions
	for _, action := range params.Actions {
		if shouldReturn, ret, err := handleAction(ctx, params, action); shouldReturn {
			return ret, err
		}
	}

	// Handle signal actions
	if params.ActionSignal != "" {
		actionCh := workflow.GetSignalChannel(ctx, params.ActionSignal)
		for {
			var action KitchenSinkAction
			actionCh.Receive(ctx, &action)
			if shouldReturn, ret, err := handleAction(ctx, params, &action); shouldReturn {
				return ret, err
			}
		}
	}

	return nil, nil
}

func handleAction(
	ctx workflow.Context,
	params *KitchenSinkWorkflowParams,
	action *KitchenSinkAction,
) (bool, interface{}, error) {
	info := workflow.GetInfo(ctx)
	switch {
	case action.Result != nil:
		if action.Result.RunID {
			return true, info.WorkflowExecution.RunID, nil
		}
		return true, action.Result.Value, nil

	case action.Error != nil:
		if action.Error.Attempt {
			return true, nil, fmt.Errorf("attempt %v", info.Attempt)
		}
		var details []interface{}
		if action.Error.Details != nil {
			details = append(details, action.Error.Details)
		}
		return true, nil, temporal.NewApplicationError(action.Error.Message, "", details...)

	case action.ContinueAsNew != nil:
		if action.ContinueAsNew.WhileAboveZero > 0 {
			action.ContinueAsNew.WhileAboveZero--
			return true, nil, workflow.NewContinueAsNewError(ctx, KitchenSinkWorkflow, params)
		}

	case action.Sleep != nil:
		if err := workflow.Sleep(ctx, time.Duration(action.Sleep.Millis)*time.Millisecond); err != nil {
			return true, nil, err
		}

	case action.QueryHandler != nil:
		err := workflow.SetQueryHandler(ctx, action.QueryHandler.Name, func(arg string) (string, error) { return arg, nil })
		if err != nil {
			return true, nil, err
		}

	case action.Signal != nil:
		workflow.GetSignalChannel(ctx, action.Signal.Name).Receive(ctx, nil)

	case action.ExecuteActivity != nil:
		opts := workflow.ActivityOptions{
			TaskQueue:              action.ExecuteActivity.TaskQueue,
			ScheduleToCloseTimeout: time.Duration(action.ExecuteActivity.ScheduleToCloseTimeoutMS) * time.Millisecond,
			StartToCloseTimeout:    time.Duration(action.ExecuteActivity.StartToCloseTimeoutMS) * time.Millisecond,
			ScheduleToStartTimeout: time.Duration(action.ExecuteActivity.ScheduleToStartTimeoutMS) * time.Millisecond,
			WaitForCancellation:    action.ExecuteActivity.WaitForCancellation,
			HeartbeatTimeout:       time.Duration(action.ExecuteActivity.HeartbeatTimeoutMS) * time.Millisecond,
			RetryPolicy: &temporal.RetryPolicy{
				InitialInterval:        1 * time.Millisecond,
				BackoffCoefficient:     1.01,
				MaximumInterval:        2 * time.Millisecond,
				MaximumAttempts:        1,
				NonRetryableErrorTypes: action.ExecuteActivity.NonRetryableErrorTypes,
			},
		}
		if opts.StartToCloseTimeout == 0 && opts.ScheduleToCloseTimeout == 0 {
			opts.ScheduleToCloseTimeout = 3 * time.Minute
		}
		if action.ExecuteActivity.RetryMaxAttempts > 1 {
			opts.RetryPolicy.MaximumAttempts = int32(action.ExecuteActivity.RetryMaxAttempts)
		}
		var lastErr error
		var lastResponse string
		count := action.ExecuteActivity.Count
		if count == 0 {
			count = 1
		}
		sel := workflow.NewSelector(ctx)
		sel.AddReceive(ctx.Done(), func(workflow.ReceiveChannel, bool) { lastErr = fmt.Errorf("context closed") })
		for i := 0; i < count; i++ {
			actCtx := workflow.WithActivityOptions(ctx, opts)
			if action.ExecuteActivity.CancelAfterMS > 0 {
				var cancel workflow.CancelFunc
				actCtx, cancel = workflow.WithCancel(actCtx)
				workflow.Go(actCtx, func(actCtx workflow.Context) {
					workflow.Sleep(actCtx, time.Duration(action.ExecuteActivity.CancelAfterMS)*time.Millisecond)
					cancel()
				})
			}
			args := action.ExecuteActivity.Args
			if action.ExecuteActivity.IndexAsArg {
				args = []interface{}{i}
			}
			sel.AddFuture(workflow.ExecuteActivity(actCtx, action.ExecuteActivity.Name, args...),
				func(fut workflow.Future) { lastErr = fut.Get(actCtx, &lastResponse) })
		}
		for i := 0; i < count && lastErr == nil; i++ {
			sel.Select(ctx)
		}
		return true, lastResponse, lastErr

	default:
		return true, nil, fmt.Errorf("unrecognized action")
	}
	return false, nil, nil
}
