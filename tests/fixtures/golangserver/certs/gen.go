package main

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"log"
	"math"
	"math/big"
	"net"
	"os"
	"path/filepath"
	"runtime"
	"time"
)

func main() {
	if err := run(); err != nil {
		log.Fatal(err)
	}
}

func run() error {
	_, thisFile, _, _ := runtime.Caller(0)
	thisDir := filepath.Dir(thisFile)
	// Gen CAs and certs
	if err := genCAAndCert(filepath.Join(thisDir, "server")); err != nil {
		return err
	} else if err = genCAAndCert(filepath.Join(thisDir, "client")); err != nil {
		return err
	}
	return nil
}

func genCAAndCert(filePrefix string) error {
	if ca, err := genCert(nil); err != nil {
		return err
	} else if err := os.WriteFile(filePrefix+"-ca-cert.pem", ca.certPEM, 0644); err != nil {
		return err
	} else if err := os.WriteFile(filePrefix+"-ca-key.pem", ca.keyPEM, 0600); err != nil {
		return err
	} else if cert, err := genCert(ca); err != nil {
		return err
	} else if err := os.WriteFile(filePrefix+"-cert.pem", cert.certPEM, 0644); err != nil {
		return err
	} else if err := os.WriteFile(filePrefix+"-key.pem", cert.keyPEM, 0600); err != nil {
		return err
	}
	return nil
}

type keyPair struct {
	cert    *x509.Certificate
	certPEM []byte
	key     *ecdsa.PrivateKey
	keyPEM  []byte
}

// Without parent this assumes it will be a CA and will self sign
func genCert(parent *keyPair) (*keyPair, error) {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, err
	}
	template := &x509.Certificate{
		Subject:               pkix.Name{Organization: []string{"My Org"}},
		IPAddresses:           []net.IP{net.IPv4(127, 0, 0, 1), net.IPv6loopback},
		DNSNames:              []string{"localhost", "myserver"},
		NotAfter:              time.Now().AddDate(10, 0, 0),
		NotBefore:             time.Now().AddDate(-10, 0, 0),
		KeyUsage:              x509.KeyUsageDigitalSignature,
		ExtKeyUsage:           []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth, x509.ExtKeyUsageServerAuth},
		BasicConstraintsValid: true,
	}
	template.SerialNumber, _ = rand.Int(rand.Reader, big.NewInt(math.MaxInt64))
	signCert, signKey := template, key
	if parent == nil {
		template.KeyUsage |= x509.KeyUsageCertSign
		template.IsCA = true
	} else {
		signCert, signKey = parent.cert, parent.key
	}
	der, err := x509.CreateCertificate(rand.Reader, template, signCert, key.Public(), signKey)
	if err != nil {
		return nil, err
	}
	cert, err := x509.ParseCertificate(der)
	if err != nil {
		return nil, err
	}
	certPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	keyPKCS, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		return nil, err
	}
	keyPEM := pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: keyPKCS})
	return &keyPair{cert, certPEM, key, keyPEM}, nil
}
