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
	Result                  interface{} `json:"result"`
	ErrorWith               string      `json:"error_with"`
	ErrorDetails            interface{} `json:"error_details"`
	ContinueAsNewCount      int         `json:"continue_as_new_count"`
	ResultAsStringSignalArg string      `json:"result_as_string_signal_arg"`
	ResultAsRunID           bool        `json:"result_as_run_id"`
	SleepMS                 int64       `json:"sleep_ms"`
	QueriesWithStringArg    []string    `json:"queries_with_string_arg"`
}

func KitchenSinkWorkflow(ctx workflow.Context, params *KitchenSinkWorkflowParams) (interface{}, error) {
	b, _ := json.Marshal(params)
	workflow.GetLogger(ctx).Info("Started kitchen sink workflow", "params", string(b))

	for _, name := range params.QueriesWithStringArg {
		workflow.SetQueryHandler(ctx, name, func(arg string) (string, error) { return arg, nil })
	}

	if params.SleepMS > 0 {
		if err := workflow.Sleep(ctx, time.Duration(params.SleepMS)*time.Millisecond); err != nil {
			return nil, err
		}
	}

	switch {
	case params.ContinueAsNewCount > 0:
		params.ContinueAsNewCount--
		return nil, workflow.NewContinueAsNewError(ctx, KitchenSinkWorkflow, params)
	case params.ErrorWith != "":
		var details []interface{}
		if params.ErrorDetails != nil {
			details = append(details, params.ErrorDetails)
		}
		return nil, temporal.NewApplicationError(params.ErrorWith, "", details...)
	case params.ResultAsStringSignalArg != "":
		var signalArg string
		workflow.GetSignalChannel(ctx, params.ResultAsStringSignalArg).Receive(ctx, &signalArg)
		return signalArg, nil
	case params.ResultAsRunID:
		return workflow.GetInfo(ctx).WorkflowExecution.RunID, nil
	}
	return params.Result, nil
}
