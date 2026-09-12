package daemon

import (
	"context"
	"encoding/json"
	"errors"
	"math"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

type failureStateLifecycle struct {
	cleaned  *bool
	released *bool
}

func (l failureStateLifecycle) Cleanup(context.Context) error {
	*l.cleaned = true
	return nil
}

func (l failureStateLifecycle) Released() error {
	*l.released = true
	return nil
}

func TestDaemonFailureSettlementAllowsOnlyExecutorOwnedStates(t *testing.T) {
	tests := []struct {
		name            string
		current         WorkItem
		wantTransitions int
		wantReleases    int
	}{
		{name: "provisioning", current: WorkItem{ID: "test-work", Status: "provisioning", Version: 2}, wantTransitions: 1, wantReleases: 1},
		{name: "analyzing", current: WorkItem{ID: "test-work", Status: "analyzing", Version: 3}, wantTransitions: 1, wantReleases: 1},
		{name: "implementing", current: WorkItem{ID: "test-work", Status: "implementing", Version: 4}, wantTransitions: 1, wantReleases: 1},
		{name: "verifying", current: WorkItem{ID: "test-work", Status: "verifying", Version: 5}, wantTransitions: 1, wantReleases: 1},
		{name: "completed", current: WorkItem{ID: "test-work", Status: "completed", Version: 6}, wantReleases: 1},
		{name: "failed", current: WorkItem{ID: "test-work", Status: "failed", Version: 6}, wantReleases: 1},
		{name: "cancelled", current: WorkItem{ID: "test-work", Status: "cancelled", Version: 6}, wantReleases: 1},
		{name: "committing", current: WorkItem{ID: "test-work", Status: "committing", Version: 6}},
		{name: "pr created", current: WorkItem{ID: "test-work", Status: "pr_created", Version: 6}},
		{name: "awaiting approval", current: WorkItem{ID: "test-work", Status: "awaiting_approval", Version: 6}},
		{name: "awaiting input", current: WorkItem{ID: "test-work", Status: "awaiting_input", Version: 6}},
		{name: "awaiting feedback", current: WorkItem{ID: "test-work", Status: "awaiting_feedback", Version: 6}},
		{name: "budget exhausted", current: WorkItem{ID: "test-work", Status: "budget_exhausted", Version: 6}},
		{name: "unknown", current: WorkItem{ID: "test-work", Status: "future_state", Version: 6}},
		{name: "wrong id", current: WorkItem{ID: "other-work", Status: "implementing", Version: 4}},
		{name: "zero version", current: WorkItem{ID: "test-work", Status: "implementing", Version: 0}},
		{name: "negative version", current: WorkItem{ID: "test-work", Status: "implementing", Version: -1}},
		{name: "stale version", current: WorkItem{ID: "test-work", Status: "implementing", Version: 1}},
		{name: "overflow version", current: WorkItem{ID: "test-work", Status: "implementing", Version: math.MaxInt}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			cleaned, released := false, false
			transitions, releases := 0, 0
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if !cleaned {
					t.Error("control-plane failure settlement started before physical cleanup")
				}
				assertFailureStateRequest(t, r)
				switch {
				case r.Method == http.MethodGet:
					_ = json.NewEncoder(w).Encode(tt.current)
				case strings.HasSuffix(r.URL.Path, "/transition"):
					transitions++
					var body struct {
						Status          string `json:"status"`
						ExpectedVersion int    `json:"expected_version"`
					}
					if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
						t.Error(err)
					}
					if body.Status != "failed" || body.ExpectedVersion != tt.current.Version {
						t.Errorf("transition = %+v; want failed at version %d", body, tt.current.Version)
					}
					_ = json.NewEncoder(w).Encode(WorkItem{ID: "test-work", Status: "failed", Version: tt.current.Version + 1})
				case strings.HasSuffix(r.URL.Path, "/release"):
					releases++
					w.WriteHeader(http.StatusNoContent)
				default:
					w.WriteHeader(http.StatusNoContent)
				}
			}))
			defer server.Close()

			daemon := resourceDaemon(server)
			if !daemon.tracker.Reserve(testResources) {
				t.Fatal("initial reservation failed")
			}
			daemon.executor = executorFunc(func(_ context.Context, client RunClient, _ Claim) error {
				if err := client.BindResources(failureStateLifecycle{cleaned: &cleaned, released: &released}); err != nil {
					return err
				}
				return errors.New("synthetic executor failure")
			})
			claim := resourceClaim()
			claim.WorkItem.Version = 2
			daemon.execute(context.Background(), claim)

			if !cleaned {
				t.Fatal("physical resources were not cleaned")
			}
			if transitions != tt.wantTransitions || releases != tt.wantReleases {
				t.Fatalf("transitions = %d, releases = %d; want %d, %d", transitions, releases, tt.wantTransitions, tt.wantReleases)
			}
			if tt.wantReleases == 1 {
				if !released {
					t.Fatal("released lifecycle was not acknowledged")
				}
				assertResources(t, daemon, testResources, 0)
			} else {
				if released {
					t.Fatal("protected state released its lease")
				}
				assertResources(t, daemon, Resources{}, 1)
			}
		})
	}
}

func TestDaemonFailureSettlementRereadsOnceAfterConflict(t *testing.T) {
	tests := []struct {
		name            string
		afterConflict   WorkItem
		wantTransitions int
		wantReleases    int
	}{
		{name: "cancel wins", afterConflict: WorkItem{ID: "test-work", Status: "cancelled", Version: 5}, wantTransitions: 1, wantReleases: 1},
		{name: "approval wins", afterConflict: WorkItem{ID: "test-work", Status: "awaiting_approval", Version: 5}, wantTransitions: 1},
		{name: "delivery wins", afterConflict: WorkItem{ID: "test-work", Status: "pr_created", Version: 5}, wantTransitions: 1},
		{name: "executor remains active", afterConflict: WorkItem{ID: "test-work", Status: "verifying", Version: 5}, wantTransitions: 2, wantReleases: 1},
		{name: "malformed reread", afterConflict: WorkItem{ID: "other-work", Status: "verifying", Version: 5}, wantTransitions: 1},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			reads, transitions, releases := 0, 0, 0
			cleaned, released := false, false
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				assertFailureStateRequest(t, r)
				switch {
				case r.Method == http.MethodGet:
					reads++
					current := WorkItem{ID: "test-work", Status: "implementing", Version: 4}
					if reads == 2 {
						current = tt.afterConflict
					}
					_ = json.NewEncoder(w).Encode(current)
				case strings.HasSuffix(r.URL.Path, "/transition"):
					transitions++
					if transitions == 1 {
						w.WriteHeader(http.StatusConflict)
						return
					}
					_ = json.NewEncoder(w).Encode(WorkItem{ID: "test-work", Status: "failed", Version: 6})
				case strings.HasSuffix(r.URL.Path, "/release"):
					releases++
					w.WriteHeader(http.StatusNoContent)
				default:
					w.WriteHeader(http.StatusNoContent)
				}
			}))
			defer server.Close()

			daemon := resourceDaemon(server)
			if !daemon.tracker.Reserve(testResources) {
				t.Fatal("initial reservation failed")
			}
			daemon.executor = executorFunc(func(_ context.Context, client RunClient, _ Claim) error {
				if err := client.BindResources(failureStateLifecycle{cleaned: &cleaned, released: &released}); err != nil {
					return err
				}
				return errors.New("synthetic executor failure")
			})
			claim := resourceClaim()
			claim.WorkItem.Version = 2
			daemon.execute(context.Background(), claim)

			if reads != 2 || transitions != tt.wantTransitions || releases != tt.wantReleases {
				t.Fatalf("reads = %d, transitions = %d, releases = %d; want 2, %d, %d", reads, transitions, releases, tt.wantTransitions, tt.wantReleases)
			}
			if !cleaned {
				t.Fatal("physical resources were not cleaned")
			}
			if (tt.wantReleases == 1) != released {
				t.Fatalf("released = %t; want %t", released, tt.wantReleases == 1)
			}
		})
	}
}

func assertFailureStateRequest(t *testing.T, r *http.Request) {
	t.Helper()
	validPath := r.URL.Path == "/api/runs/test-work" ||
		r.URL.Path == "/api/runs/test-work/events" ||
		r.URL.Path == "/api/runs/test-work/transition" ||
		r.URL.Path == "/api/runs/test-work/release"
	if !validPath {
		t.Errorf("privileged operation targeted %q", r.URL.Path)
	}
	if r.Header.Get("X-Kelpie-Lease") != "test-only-lease" {
		t.Error("privileged operation omitted its assigned lease")
	}
}

func TestDaemonFailureRequiresExactTransitionAcknowledgement(t *testing.T) {
	for _, response := range []string{
		``,
		`{}`,
		`{"id":"other-work","status":"failed","version":5}`,
		`{"id":"test-work","status":"awaiting_approval","version":5}`,
		`{"id":"test-work","status":"failed","version":4}`,
		`{"id":"test-work","status":"failed","version":6}`,
	} {
		t.Run(response, func(t *testing.T) {
			releases := 0
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				switch {
				case r.Method == http.MethodGet:
					_ = json.NewEncoder(w).Encode(WorkItem{ID: "test-work", Status: "implementing", Version: 4})
				case strings.HasSuffix(r.URL.Path, "/transition"):
					_, _ = w.Write([]byte(response))
				case strings.HasSuffix(r.URL.Path, "/release"):
					releases++
					w.WriteHeader(http.StatusNoContent)
				default:
					w.WriteHeader(http.StatusNoContent)
				}
			}))
			defer server.Close()
			d := resourceDaemon(server)
			d.tracker.Reserve(testResources)
			d.executor = executorFunc(func(context.Context, RunClient, Claim) error {
				return errors.New("synthetic executor failure")
			})
			d.execute(context.Background(), resourceClaim())
			if releases != 0 {
				t.Fatal("unconfirmed transition acknowledged as terminal work")
			}
			assertResources(t, d, Resources{}, 1)
		})
	}
}
