package main

import (
	"fmt"
	"log"
	"os"
	"os/signal"
	"path/filepath"
	"runtime"
	"strconv"
	"syscall"

	"github.com/DataDog/temporalite"
	"go.temporal.io/server/common/config"
	"go.temporal.io/server/common/dynamicconfig"
	serverlog "go.temporal.io/server/common/log"
	"go.temporal.io/server/common/rpc/encryption"
	"go.temporal.io/server/temporal"
)

func main() {
	if len(os.Args) != 3 {
		log.Fatalf("expected port and namespace arg, found %v args", len(os.Args)-1)
	}
	if err := run(os.Args[1], os.Args[2]); err != nil {
		log.Fatal(err)
	}
}

// TODO(cretz): No longer works in newer Temporalite versions, waiting on
// https://github.com/DataDog/temporalite/pull/75
const supportsTLS = false

func run(portStr, namespace string) error {
	port, err := strconv.Atoi(portStr)
	if err != nil {
		return fmt.Errorf("invalid port: %w", err)
	}

	log.Printf("Starting separate servers on ports %v and %v (TLS) for namespace %v", port, port+1, namespace)

	// Start non-TLS server
	server, err := temporalite.NewServer(
		temporalite.WithNamespaces(namespace),
		temporalite.WithPersistenceDisabled(),
		temporalite.WithFrontendPort(port),
		// TODO(cretz): Allow verbose output?
		temporalite.WithLogger(serverlog.NewNoopLogger()),
		// This is needed so that tests can run without search attribute cache
		temporalite.WithUpstreamOptions(temporal.WithDynamicConfigClient(dynamicconfig.NewMutableEphemeralClient(
			dynamicconfig.Set(dynamicconfig.ForceSearchAttributesCacheRefreshOnRead, true)))),
	)
	if err != nil {
		return err
	}
	if err := server.Start(); err != nil {
		return err
	}
	defer server.Stop()

	if supportsTLS {
		// Start TLS server
		_, thisFile, _, _ := runtime.Caller(0)
		certsDir := filepath.Join(thisFile, "..", "certs")
		var rootTLS config.RootTLS
		rootTLS.Frontend.Server.CertFile = filepath.Join(certsDir, "server-cert.pem")
		rootTLS.Frontend.Server.KeyFile = filepath.Join(certsDir, "server-key.pem")
		rootTLS.Frontend.Server.ClientCAFiles = []string{filepath.Join(certsDir, "client-ca-cert.pem")}
		rootTLS.Frontend.Server.RequireClientAuth = true
		tlsConfigProvider, err := encryption.NewTLSConfigProviderFromConfig(rootTLS, nil, serverlog.NewNoopLogger(), nil)
		if err != nil {
			return err
		}
		tlsServer, err := temporalite.NewServer(
			temporalite.WithNamespaces(namespace),
			temporalite.WithPersistenceDisabled(),
			temporalite.WithFrontendPort(port+1000),
			temporalite.WithLogger(serverlog.NewNoopLogger()),
			temporalite.WithUpstreamOptions(temporal.WithTLSConfigFactory(tlsConfigProvider)),
		)
		if err != nil {
			return err
		}
		if err := tlsServer.Start(); err != nil {
			return err
		}
		defer tlsServer.Stop()
	}

	defer log.Printf("Stopping servers")
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, os.Interrupt, syscall.SIGTERM, os.Interrupt)
	<-sigCh
	return nil
}
