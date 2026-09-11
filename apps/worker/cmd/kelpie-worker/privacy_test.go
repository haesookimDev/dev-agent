package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"github.com/haesookimdev/kelpie/apps/worker/internal/daemon"
)

const runtimePrivate = "synthetic-runtime-private-value"

// A real worker binary talks over TCP to an explicitly synthetic control plane.
// The failing qemu-img fixture never creates a disk or VM. This is diagnostic
// acceptance, not libvirt/KVM lifecycle acceptance.
func TestWorkerProcessPrivateDiagnostics(t *testing.T) {
	binary := filepath.Join(t.TempDir(), "worker")
	ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
	defer cancel()
	if err := exec.CommandContext(ctx, "go", "build", "-o", binary, ".").Run(); err != nil {
		t.Fatal("could not build the worker acceptance binary")
	}
	for _, scenario := range []struct{ name, expected string }{
		{"registration rejected", "control plane returned HTTP 403 Forbidden"},
		{"missing image", "VM base image unavailable"},
		{"command output", "VM command failed (exit 7)"},
	} {
		t.Run(scenario.name, func(t *testing.T) {
			root := t.TempDir()
			image := filepath.Join(root, runtimePrivate)
			if scenario.name == "command output" {
				if err := os.WriteFile(image, []byte("not a VM image"), 0600); err != nil {
					t.Fatal(err)
				}
				// Read only this process's synthetic fixture credential, never host env.
				script := "#!/bin/sh\nprintf '%s' \"$KELPIE_WORKER_TOKEN\"\nprintf '%s' \"$KELPIE_WORKER_TOKEN\" >&2\nexit 7\n"
				if err := os.WriteFile(filepath.Join(root, "qemu-img"), []byte(script), 0700); err != nil {
					t.Fatal(err)
				}
				// This fixture fails before VM creation. The lifecycle path must
				// still obtain a successful empty inventory, not infer absence from
				// a missing virsh command. No destructive command is accepted here.
				inventory := "#!/bin/sh\ncase \"$*\" in\n'--connect qemu:///system list --all --uuid'|'--connect qemu:///system list --all --uuid --persistent') exit 0;;\n*) exit 9;;\nesac\n"
				if err := os.WriteFile(filepath.Join(root, "virsh"), []byte(inventory), 0700); err != nil {
					t.Fatal(err)
				}
			}
			workerToken, leaseToken := strings.Repeat(runtimePrivate, 2), "synthetic-runtime-lease"
			work := daemon.WorkItem{ID: "33333333-3333-4333-8333-333333333333",
				CorrelationID: "44444444-4444-4444-8444-444444444444", Title: runtimePrivate,
				Status: "provisioning", Version: 2}
			var mu sync.Mutex
			var events []daemon.AgentEvent
			claimed, releases := false, 0
			released := make(chan struct{}, 1)
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				mu.Lock()
				defer mu.Unlock()
				if strings.HasPrefix(r.URL.Path, "/api/workers/") {
					if r.Header.Get("Authorization") != "Bearer "+workerToken || r.Header.Get("X-Kelpie-Lease") != "" {
						t.Error("worker request used the wrong credential")
					}
				} else if r.Header.Get("X-Kelpie-Lease") != leaseToken || r.Header.Get("Authorization") != "" ||
					r.Header.Get("X-Kelpie-Correlation-ID") != work.CorrelationID {
					t.Error("run request lost credential isolation or correlation")
				}
				switch {
				case strings.HasSuffix(r.URL.Path, "/register"):
					if scenario.name == "registration rejected" {
						w.WriteHeader(http.StatusForbidden)
						_, _ = io.WriteString(w, workerToken)
					} else {
						_ = json.NewEncoder(w).Encode(daemon.Worker{ID: "test-worker"})
					}
				case strings.HasSuffix(r.URL.Path, "/claim"):
					if claimed {
						_, _ = io.WriteString(w, "null")
					} else {
						claimed = true
						_ = json.NewEncoder(w).Encode(daemon.Claim{WorkItem: work, LeaseToken: leaseToken})
					}
				case strings.HasSuffix(r.URL.Path, "/events"):
					var event daemon.AgentEvent
					if json.NewDecoder(r.Body).Decode(&event) != nil {
						t.Error("invalid runtime event")
					}
					events = append(events, event)
				case r.Method == http.MethodGet:
					_ = json.NewEncoder(w).Encode(work)
				case strings.HasSuffix(r.URL.Path, "/transition"):
					var body struct {
						Status  string `json:"status"`
						Version int    `json:"expected_version"`
					}
					if json.NewDecoder(r.Body).Decode(&body) != nil || body.Status != "failed" || body.Version != 2 {
						t.Error("failure transition did not preserve the version gate")
					}
					work.Status, work.Version = "failed", 3
					_ = json.NewEncoder(w).Encode(work)
				case strings.HasSuffix(r.URL.Path, "/release"):
					if work.Status != "failed" {
						t.Error("release happened before terminal acknowledgement")
					}
					releases++
					select {
					case released <- struct{}{}:
					default:
					}
				default:
					t.Error("unexpected runtime request")
					w.WriteHeader(http.StatusNotFound)
				}
			}))
			defer server.Close()
			process := exec.Command(binary)
			process.Env = []string{"PATH=" + root, "KELPIE_CONTROL_URL=" + server.URL,
				"KELPIE_WORKER_TOKEN=" + workerToken, "KELPIE_WORKER_NAME=private-diagnostic-test",
				"KELPIE_EXECUTOR=libvirt", "KELPIE_BASE_IMAGE=" + image,
				"KELPIE_WORK_ROOT=" + filepath.Join(root, "runs"), "KELPIE_POLL_SECONDS=1"}
			var logs bytes.Buffer
			process.Stdout, process.Stderr = &logs, &logs
			if process.Start() != nil {
				t.Fatal("could not start owned worker")
			}
			done := make(chan error, 1)
			go func() { done <- process.Wait() }()
			finished := false
			defer func() {
				if !finished {
					_ = process.Process.Kill()
					<-done
				}
			}()
			if scenario.name != "registration rejected" {
				select {
				case <-released:
					_ = process.Process.Signal(syscall.SIGTERM)
				case <-time.After(10 * time.Second):
					t.Fatal("owned worker did not acknowledge terminal release")
				}
			}
			select {
			case err := <-done:
				finished = true
				var exit *exec.ExitError
				if scenario.name == "registration rejected" {
					if !errors.As(err, &exit) || exit.ExitCode() != 1 {
						t.Fatal("rejected registration did not fail closed")
					}
				} else if err != nil {
					t.Fatal("owned worker did not stop cleanly on SIGTERM")
				}
			case <-time.After(5 * time.Second):
				t.Fatal("owned worker shutdown timed out")
			}
			if strings.Contains(logs.String(), runtimePrivate) || strings.Contains(logs.String(), leaseToken) ||
				!strings.Contains(logs.String(), scenario.expected) {
				t.Fatal("process log retained private text or lost failure classification")
			}
			mu.Lock()
			defer mu.Unlock()
			if scenario.name == "registration rejected" {
				if claimed || releases != 0 || len(events) != 0 {
					t.Fatal("rejected worker performed run operations")
				}
			} else if len(events) != 1 || events[0].EventType != "worker.failed" || events[0].Message != scenario.expected || releases != 1 {
				t.Fatal("runtime failure event or exactly-once release did not match")
			}
		})
	}
}
