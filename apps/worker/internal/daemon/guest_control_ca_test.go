package daemon

import (
	"bytes"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"encoding/base64"
	"encoding/pem"
	"math/big"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func guestTestCA(t *testing.T, valid bool, mutate ...func(*x509.Certificate)) []byte {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	cert := &x509.Certificate{SerialNumber: big.NewInt(1), NotBefore: time.Now().Add(-time.Hour),
		NotAfter: time.Now().Add(time.Hour), IsCA: valid, BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign}
	for _, change := range mutate {
		change(cert)
	}
	der, err := x509.CreateCertificate(rand.Reader, cert, cert, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
}

func TestGuestControlCARejectsExpiredFutureAndNonSigningRoots(t *testing.T) {
	for _, change := range []func(*x509.Certificate){
		func(c *x509.Certificate) { c.NotAfter = time.Now().Add(-time.Minute) },
		func(c *x509.Certificate) { c.NotBefore = time.Now().Add(time.Minute) },
		func(c *x509.Certificate) { c.KeyUsage = x509.KeyUsageDigitalSignature },
	} {
		if data, err := canonicalGuestControlCA(guestTestCA(t, true, change)); err == nil || data != nil {
			t.Fatal("invalid CA validity or signing constraints accepted")
		}
	}
}

func TestGuestControlCAConfigValidatesExplicitFile(t *testing.T) {
	t.Setenv("KELPIE_WORKER_TOKEN_FILE", "")
	t.Setenv("KELPIE_WORKER_TOKEN", "12345678901234567890123456789012")
	t.Setenv("KELPIE_EXECUTOR", "libvirt")
	t.Setenv("KELPIE_GUEST_CONTROL_URL", "https://control.example.test")
	t.Setenv("KELPIE_GUEST_CONTROL_IPV4", "192.0.2.7")
	t.Setenv("KELPIE_NETWORK_POOL", "10.240.0.0/16")
	path := filepath.Join(t.TempDir(), "control-ca.pem")
	t.Setenv("KELPIE_GUEST_CONTROL_CA_FILE", path)
	if _, err := ConfigFromEnv(); err == nil {
		t.Fatal("missing explicit CA file accepted")
	}
	if err := os.WriteFile(path, guestTestCA(t, true), 0600); err != nil {
		t.Fatal(err)
	}
	config, err := ConfigFromEnv()
	if err != nil || config.GuestControlCAFile != path {
		t.Fatal("explicit valid CA configuration rejected")
	}
}

func TestGuestControlCARejectsUnsafeFileAndNonCertificateData(t *testing.T) {
	ca := guestTestCA(t, true)
	path := filepath.Join(t.TempDir(), "control-ca.pem")
	if err := os.WriteFile(path, ca, 0600); err != nil {
		t.Fatal(err)
	}
	data, err := readGuestControlCA(path)
	if err != nil || !bytes.Equal(data, ca) {
		t.Fatal("explicit private CA rejected")
	}
	if data, err := readGuestControlCA(""); err != nil || data != nil {
		t.Fatal("optional CA should not inspect ambient files")
	}
	link := path + "-link"
	if err := os.Symlink(path, link); err != nil {
		t.Fatal(err)
	}
	if _, err := readGuestControlCA(link); err == nil {
		t.Fatal("CA symlink accepted")
	}
	if err := os.Chmod(path, 0644); err != nil {
		t.Fatal(err)
	}
	if _, err := readGuestControlCA(path); err == nil {
		t.Fatal("unprotected CA file accepted")
	}
	for _, bad := range [][]byte{nil, []byte("private-key-canary"), guestTestCA(t, false),
		append([]byte("untrusted preamble\n"), ca...), append(append([]byte{}, ca...), []byte("private-key-canary")...),
		append([]byte("-----BEGIN CERTIFICATE-----\ninvalid\n-----END CERTIFICATE-----\n"), ca...),
		bytes.Repeat(ca, 9), bytes.Repeat([]byte("x"), (64<<10)+1)} {
		if data, err := canonicalGuestControlCA(bad); err == nil || data != nil || strings.Contains(err.Error(), "canary") {
			t.Fatal("invalid CA data accepted or disclosed")
		}
	}
}

func TestGuestSeedScopesCustomTrustToControlClient(t *testing.T) {
	ca := guestTestCA(t, true)
	control, _ := parseGuestControl("https://control.example.test", "192.0.2.7")
	body, err := guestUserData(control, resourceClaim(), ca)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Contains(body, []byte("path: /run/kelpie/control-ca.pem")) ||
		!bytes.Contains(body, []byte(base64.StdEncoding.EncodeToString(ca))) ||
		bytes.Contains(body, []byte("SSL_CERT_FILE")) || bytes.Contains(body, []byte("update-ca-certificates")) {
		t.Fatal("guest CA missing or leaked into global/model trust")
	}
	parts := strings.Split(string(body), "    encoding: b64\n    content: ")
	if len(parts) != 3 {
		t.Fatal("expected environment and CA files")
	}
	environment, err := base64.StdEncoding.DecodeString(strings.SplitN(parts[1], "\n", 2)[0])
	if err != nil || !bytes.Contains(environment, []byte("KELPIE_CONTROL_CA_FILE=/run/kelpie/control-ca.pem\n")) {
		t.Fatal("CA not passed to Runner")
	}
	if body, err := guestUserData(control, resourceClaim(), []byte("private-key-canary")); err == nil || body != nil {
		t.Fatal("unsafe CA encoded into guest seed")
	}
}
