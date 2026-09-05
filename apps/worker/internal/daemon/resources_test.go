package daemon

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

var testResources = Resources{CPU: 2, MemoryMB: 4096, DiskGB: 30}

func resourceDaemon(server *httptest.Server) *Daemon {
	return &Daemon{
		config:  Config{RunResources: testResources},
		client:  NewClient(server.URL, strings.Repeat("test-only", 4)),
		tracker: NewTracker(testResources),
		logger:  slog.New(slog.NewTextHandler(io.Discard, nil)),
	}
}

func resourceClaim() Claim {
	return Claim{WorkItem: WorkItem{ID: "test-work"}, LeaseToken: "test-only-lease"}
}

func assertResources(t *testing.T, daemon *Daemon, available Resources, active int) {
	t.Helper()
	got, runs := daemon.tracker.Available()
	if got != available || runs != active {
		t.Fatalf("resources = %+v, active = %d; want %+v, %d", got, runs, available, active)
	}
}

func TestResourceWritesSerializeBothDirections(t *testing.T) {
	for _, order := range [][2]string{
		{"release", "heartbeat"}, {"heartbeat", "release"},
		{"claim", "heartbeat"}, {"heartbeat", "claim"},
	} {
		t.Run(strings.Join(order[:], "-then-"), func(t *testing.T) {
			requests := make(chan string, 2)
			unblock := make(chan struct{})
			var unblockOnce sync.Once
			finish := func() { unblockOnce.Do(func() { close(unblock) }) }
			ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
			defer cancel()
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				operation := r.URL.Path[strings.LastIndex(r.URL.Path, "/")+1:]
				requests <- operation
				if operation == order[0] {
					select {
					case <-unblock:
					case <-r.Context().Done():
						return
					}
				}
				if operation == "heartbeat" {
					var body Worker
					if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
						t.Error(err)
					}
					active := 1
					if order[0] == "release" || order[1] == "claim" {
						active = 0
					}
					available := testResources
					if active == 1 {
						available = Resources{}
					}
					if body.ActiveRuns != active || body.CPUAvailable != available.CPU ||
						body.MemoryMBAvailable != available.MemoryMB || body.DiskGBAvailable != available.DiskGB {
						t.Errorf("heartbeat published stale accounting: %+v; want active=%d resources=%+v", body, active, available)
					}
				}
				if operation == "claim" {
					_ = json.NewEncoder(w).Encode(resourceClaim())
				} else {
					w.WriteHeader(http.StatusNoContent)
				}
			}))
			defer server.Close()
			defer finish()
			daemon := resourceDaemon(server)
			releasing := order[0] == "release" || order[1] == "release"
			if releasing && !daemon.tracker.Reserve(testResources) {
				t.Fatal("initial reservation failed")
			}
			client := &reservedRunClient{Client: daemon.client, daemon: daemon, claim: resourceClaim()}
			invoke := func(operation string) error {
				switch operation {
				case "release":
					return client.Release(ctx, "test-work", "test-only-lease")
				case "heartbeat":
					return daemon.heartbeat(ctx, "test-worker")
				default:
					_, err := daemon.claim(ctx, "test-worker")
					return err
				}
			}
			firstDone, secondDone := make(chan error, 1), make(chan error, 1)
			go func() { firstDone <- invoke(order[0]) }()
			select {
			case operation := <-requests:
				if operation != order[0] {
					t.Fatalf("unexpected first request: %s", operation)
				}
			case <-ctx.Done():
				t.Fatal("first request did not start")
			}
			go func() { secondDone <- invoke(order[1]) }()
			select {
			case operation := <-requests:
				t.Fatalf("%s overtook in-flight %s", operation, order[0])
			case <-time.After(50 * time.Millisecond):
			}
			finish()
			for _, done := range []chan error{firstDone, secondDone} {
				if err := <-done; err != nil {
					t.Fatal(err)
				}
			}
			if releasing {
				assertResources(t, daemon, testResources, 0)
			} else {
				assertResources(t, daemon, Resources{}, 1)
			}
		})
	}
}

func TestReleaseRetainsReservationOnFailureAndReleasesExactlyOnce(t *testing.T) {
	calls := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if calls == 1 {
			w.WriteHeader(http.StatusServiceUnavailable)
			return
		}
		w.WriteHeader(http.StatusNoContent)
	}))
	defer server.Close()
	daemon := resourceDaemon(server)
	daemon.tracker.Reserve(testResources)
	client := &reservedRunClient{Client: daemon.client, daemon: daemon, claim: resourceClaim()}
	ctx := context.Background()
	if err := client.Release(ctx, "other-work", "test-only-lease"); err == nil || calls != 0 {
		t.Fatal("mismatched work must not release resources or call the API")
	}
	if err := client.Release(ctx, "test-work", "other-lease"); err == nil || calls != 0 {
		t.Fatal("mismatched lease must not release resources or call the API")
	}
	if err := client.Release(ctx, "test-work", "test-only-lease"); err == nil {
		t.Fatal("failed release must return an error")
	}
	assertResources(t, daemon, Resources{}, 1)
	if err := client.Release(ctx, "test-work", "test-only-lease"); err != nil {
		t.Fatal(err)
	}
	assertResources(t, daemon, testResources, 0)
	// Another run may already own the slot when an old release is repeated.
	daemon.tracker.Reserve(testResources)
	if err := client.Release(ctx, "test-work", "test-only-lease"); err != nil || calls != 2 {
		t.Fatalf("duplicate release repeated the request: calls=%d, error=%v", calls, err)
	}
	assertResources(t, daemon, Resources{}, 1)
}

func TestUnclaimedResourcesAreReturned(t *testing.T) {
	for _, response := range []string{"null", "error"} {
		t.Run(response, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if response == "error" {
					w.WriteHeader(http.StatusUnauthorized)
				} else {
					_, _ = io.WriteString(w, response)
				}
			}))
			defer server.Close()
			daemon := resourceDaemon(server)
			claim, err := daemon.claim(context.Background(), "test-worker")
			if claim != nil || (err != nil) != (response == "error") {
				t.Fatalf("unexpected claim result: %+v, %v", claim, err)
			}
			assertResources(t, daemon, testResources, 0)
		})
	}
}

type executorFunc func(context.Context, RunClient, Claim) error

func (f executorFunc) Execute(ctx context.Context, client RunClient, claim Claim) error {
	return f(ctx, client, claim)
}

func TestExecutionReturnsResourcesOnlyAfterAcknowledgedTerminalRelease(t *testing.T) {
	for _, scenario := range []struct {
		name, status, reject string
		success, retained    bool
	}{
		{name: "normal completion", success: true},
		{name: "failure transitions and releases", status: "implementing"},
		{name: "already terminal failure releases", status: "completed"},
		{name: "read failure retains", reject: "read", retained: true},
		{name: "transition failure retains", status: "implementing", reject: "transition", retained: true},
		{name: "release failure retains", status: "completed", reject: "release", retained: true},
	} {
		t.Run(scenario.name, func(t *testing.T) {
			releases := 0
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				operation := r.URL.Path[strings.LastIndex(r.URL.Path, "/")+1:]
				if r.Method == http.MethodGet {
					operation = "read"
				}
				if operation == scenario.reject {
					w.WriteHeader(http.StatusServiceUnavailable)
					return
				}
				switch operation {
				case "read", "transition":
					_ = json.NewEncoder(w).Encode(WorkItem{ID: "test-work", Status: scenario.status, Version: 3})
				case "release":
					releases++
					w.WriteHeader(http.StatusNoContent)
				default:
					w.WriteHeader(http.StatusNoContent)
				}
			}))
			defer server.Close()
			daemon := resourceDaemon(server)
			daemon.tracker.Reserve(testResources)
			daemon.executor = executorFunc(func(ctx context.Context, client RunClient, claim Claim) error {
				if scenario.success {
					return client.Release(ctx, claim.WorkItem.ID, claim.LeaseToken)
				}
				return errors.New("synthetic executor failure")
			})
			daemon.execute(context.Background(), resourceClaim())
			if scenario.retained {
				assertResources(t, daemon, Resources{}, 1)
				if releases != 0 {
					t.Fatal("uncertain execution released resources")
				}
			} else {
				assertResources(t, daemon, testResources, 0)
				if releases != 1 {
					t.Fatalf("successful release count = %d, want 1", releases)
				}
			}
		})
	}
}
