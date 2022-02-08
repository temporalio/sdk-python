package main

import (
	"fmt"
	"log"
	"os"
	"os/signal"
	"strconv"
	"syscall"

	"github.com/DataDog/temporalite"
	serverlog "go.temporal.io/server/common/log"
)

func main() {
	if len(os.Args) != 3 {
		log.Fatalf("expected port and namespace arg, found %v args", len(os.Args)-1)
	}
	if err := run(os.Args[1], os.Args[2]); err != nil {
		log.Fatal(err)
	}
}

func run(portStr, namespace string) error {
	port, err := strconv.Atoi(portStr)
	if err != nil {
		return fmt.Errorf("invalid port: %w", err)
	}

	server, err := temporalite.NewServer(
		temporalite.WithNamespaces(namespace),
		temporalite.WithPersistenceDisabled(),
		temporalite.WithFrontendPort(port),
		// TODO(cretz): Allow verbose output?
		temporalite.WithLogger(serverlog.NewNoopLogger()),
	)
	if err != nil {
		return err
	}
	log.Printf("Starting server on port %v for namespace %v", port, namespace)
	if err := server.Start(); err != nil {
		return err
	}
	defer server.Stop()
	defer log.Printf("Stopping server")
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, os.Interrupt, syscall.SIGTERM)
	<-sigCh
	return nil
}
