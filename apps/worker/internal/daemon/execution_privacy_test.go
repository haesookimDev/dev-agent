package daemon

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
)

func TestCommandFailureOmitsOutputArgumentsAndPath(t *testing.T) {
	err := run(context.Background(), "sh", "-c", `printf '%s' "$1"; printf '%s' "$1" >&2; exit 7`, "test", privateFixture)
	assertPrivateError(t, err, "VM command failed (exit 7)")
	err = run(context.Background(), filepath.Join(t.TempDir(), privateFixture))
	assertPrivateError(t, err, "VM command could not start")
	if err := run(context.Background(), "sh", "-c", `printf '%s' "$1"`, "test", privateFixture); err != nil {
		t.Fatal("successful external command no longer succeeds")
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if !errors.Is(run(ctx, "sh", "-c", "exit 0"), context.Canceled) {
		t.Fatal("command cancellation classification was lost")
	}
}

func TestExecutionLogsAndFailureEventExcludePrivateDiagnostics(t *testing.T) {
	for _, releaseFails := range []bool{false, true} {
		var events []AgentEvent
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			switch {
			case strings.HasSuffix(r.URL.Path, "/events"):
				var event AgentEvent
				if err := json.NewDecoder(r.Body).Decode(&event); err != nil {
					t.Error("invalid event body")
				}
				events = append(events, event)
			case r.Method == http.MethodGet:
				_ = json.NewEncoder(w).Encode(WorkItem{ID: "test-work", Status: "failed", Version: 3})
			case releaseFails:
				w.WriteHeader(http.StatusServiceUnavailable)
				_, _ = io.WriteString(w, privateFixture)
			}
		}))
		daemon := resourceDaemon(server)
		var logs bytes.Buffer
		daemon.logger = slog.New(slog.NewJSONHandler(&logs, nil))
		daemon.executor = executorFunc(func(context.Context, RunClient, Claim) error { return errors.New(privateFixture) })
		claim := resourceClaim()
		claim.WorkItem.Title = privateFixture
		daemon.tracker.Reserve(testResources)
		daemon.execute(context.Background(), claim)
		server.Close()
		if strings.Contains(logs.String(), privateFixture) || !strings.Contains(logs.String(), "work execution failed") {
			t.Fatal("execution log retained private content or omitted the failure")
		}
		if len(events) != 1 || events[0].EventType != "worker.failed" || events[0].Message != "worker execution failed" {
			t.Fatal("failure event retained arbitrary error text or changed identity")
		}
		if releaseFails {
			assertResources(t, daemon, Resources{}, 1)
			if !strings.Contains(logs.String(), "HTTP 503 Service Unavailable") {
				t.Fatal("release failure lost its safe HTTP classification")
			}
		} else {
			assertResources(t, daemon, testResources, 0)
		}
	}
}

func TestBaseImageFailureOmitsPrivatePath(t *testing.T) {
	executor := LibvirtExecutor{config: Config{BaseImage: filepath.Join(t.TempDir(), privateFixture)}}
	err := executor.Execute(context.Background(), nil, Claim{WorkItem: WorkItem{ID: "33333333-3333-4333-8333-333333333333"}})
	assertPrivateError(t, err, "VM base image unavailable")
}

func TestDiagnosticWrapperNeverRendersPrivateContext(t *testing.T) {
	for _, scenario := range []struct {
		err      error
		expected string
	}{
		{fmt.Errorf("%s: %w", privateFixture, diagnosticError{kind: controlStatus, code: 409}), "control plane returned HTTP 409 Conflict"},
		{fmt.Errorf("%s: %w", privateFixture, context.Canceled), "context canceled"},
		{fmt.Errorf("%s: %w", privateFixture, context.DeadlineExceeded), "context deadline exceeded"},
		{fmt.Errorf("%s: %w", privateFixture, errCredentialUnavailable), errCredentialUnavailable.Error()},
		{errors.New(privateFixture), "worker execution failed"},
	} {
		if safeDiagnostic(scenario.err) != scenario.expected {
			t.Fatal("wrapped diagnostic lost safe classification or retained private text")
		}
	}
}
