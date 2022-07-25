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

const supportsTLS = true

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
		var tlsServerConfig config.Config
		tlsServerConfig.Global.TLS.Frontend.Server = config.ServerTLS{
			CertFile:      filepath.Join(certsDir, "server-cert.pem"),
			KeyFile:       filepath.Join(certsDir, "server-key.pem"),
			ClientCAFiles: []string{filepath.Join(certsDir, "client-ca-cert.pem")},
			// Cannot require client auth because the frontend client cannot connect
			// to itself
			// RequireClientAuth: true,
		}
		tlsServerConfig.Global.TLS.Frontend.Client = config.ClientTLS{
			RootCAFiles: []string{filepath.Join(certsDir, "server-ca-cert.pem")},
		}
		tlsServer, err := temporalite.NewServer(
			temporalite.WithNamespaces(namespace),
			temporalite.WithPersistenceDisabled(),
			temporalite.WithFrontendPort(port+1000),
			temporalite.WithLogger(serverlog.NewNoopLogger()),
			temporalite.WithBaseConfig(&tlsServerConfig),
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
