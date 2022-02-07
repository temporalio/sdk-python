package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"os"

	"go.temporal.io/sdk/client"
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
	return w.Run(worker.InterruptCh())
}

type KitchenSinkWorkflowParams struct {
	Result    interface{} `json:"result"`
	ErrorWith string      `json:"error_with"`
}

func KitchenSinkWorkflow(ctx workflow.Context, params *KitchenSinkWorkflowParams) (interface{}, error) {
	b, _ := json.Marshal(params)
	workflow.GetLogger(ctx).Info("Started kitchen sink workflow", "params", string(b))
	if params.ErrorWith != "" {
		return nil, errors.New(params.ErrorWith)
	}
	return params.Result, nil
}
