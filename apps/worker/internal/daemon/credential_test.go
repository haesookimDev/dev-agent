package daemon

import (
	"context"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
)

func TestCredentialFileRejectsInvalidSourcesWithoutFallback(t *testing.T) {
	root := t.TempDir()
	good := strings.Repeat("a", 64)
	for name, value := range map[string]string{
		"empty": "", "short": "short", "large": strings.Repeat("x", maxCredentialBytes+1),
		"newline": good + "\ninjected", "nul": good + "\x00", "space": good + " ",
	} {
		path := filepath.Join(root, name)
		if err := os.WriteFile(path, []byte(value), 0600); err != nil {
			t.Fatal(err)
		}
		if _, err := readCredential(path, good); !errors.Is(err, errCredentialUnavailable) {
			t.Fatalf("source %s was not rejected safely", name)
		}
	}
	fifo := filepath.Join(root, "fifo")
	if err := syscall.Mkfifo(fifo, 0600); err != nil {
		t.Fatal(err)
	}
	for _, path := range []string{root, fifo, filepath.Join(root, "missing")} {
		if _, err := readCredential(path, good); !errors.Is(err, errCredentialUnavailable) {
			t.Fatal("non-regular or missing source was not rejected safely")
		}
	}
}

func TestWorkerReloadsFileForRegisterHeartbeatClaimAndPreservesRunLease(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, "token")
	first, second := strings.Repeat("a", 64), strings.Repeat("b", 64)
	if err := os.WriteFile(path, []byte(first+"\n"), 0600); err != nil {
		t.Fatal(err)
	}
	expected := first
	requests := 0
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		requests++
		if strings.HasPrefix(request.URL.Path, "/api/runs/") {
			if request.Header.Get("Authorization") != "" || request.Header.Get("X-Kelpie-Lease") != "run-lease" {
				t.Error("run request mixed worker and lease credentials")
			}
		} else if request.Header.Get("Authorization") != "Bearer "+expected {
			t.Error("worker request did not use current file credential")
		}
		writer.WriteHeader(http.StatusNoContent)
	}))
	defer server.Close()
	t.Setenv("KELPIE_WORKER_TOKEN", strings.Repeat("z", 64))
	t.Setenv("KELPIE_WORKER_TOKEN_FILE", path)
	t.Setenv("KELPIE_CONTROL_URL", server.URL)
	t.Setenv("KELPIE_EXECUTOR", "mock")
	config, err := ConfigFromEnv()
	if err != nil || config.WorkerToken != "" {
		t.Fatal("file configuration retained fallback or failed validation")
	}
	client := New(config, slog.New(slog.NewTextHandler(io.Discard, nil))).client
	ctx := context.Background()
	if _, err := client.Register(ctx, config); err != nil {
		t.Fatal(err)
	}
	next := filepath.Join(root, "next")
	if err := os.WriteFile(next, []byte(second+"\r\n"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(next, path); err != nil {
		t.Fatal(err)
	}
	expected = second
	if err := client.Heartbeat(ctx, "worker-a", Resources{}, 0); err != nil {
		t.Fatal(err)
	}
	if _, err := client.Claim(ctx, "worker-a", Resources{}); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	if _, err := client.Claim(ctx, "worker-a", Resources{}); !errors.Is(err, errCredentialUnavailable) {
		t.Fatal("missing file did not fail closed")
	}
	if requests != 3 {
		t.Fatal("missing file sent a request with a cached or fallback token")
	}
	if _, err := client.ReadRun(ctx, "work-a", "run-lease"); err != nil {
		t.Fatal(err)
	}
	if _, err := ConfigFromEnv(); !errors.Is(err, errCredentialUnavailable) {
		t.Fatal("startup accepted missing configured file")
	}
}
