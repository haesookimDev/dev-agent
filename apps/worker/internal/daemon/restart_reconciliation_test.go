package daemon

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func recoveryWorker(active int) map[string]any {
	return map[string]any{
		"id": leaseClientWorkerID, "name": "recovery-worker", "state": "online",
		"cpu_total": 4, "cpu_available": 4 - 2*active,
		"memory_mb_total": 8192, "memory_mb_available": 8192 - 4096*active,
		"disk_gb_available": 60 - 30*active, "active_runs": active,
		"labels":       map[string]string{"virtualization": "libvirt"},
		"last_seen_at": "2026-09-11T00:00:00Z",
	}
}

func recoveryDaemon(server *httptest.Server, path string) *Daemon {
	d := resourceDaemon(server)
	d.config.Executor, d.config.WorkRoot, d.config.WorkerName = "libvirt", path, "recovery-worker"
	d.config.CPUTotal, d.config.MemoryMBTotal, d.config.DiskGBTotal = 4, 8192, 60
	d.config.PollInterval = time.Millisecond
	return d
}

func TestRestartReconcilesTerminalLeaseBeforeAdmission(t *testing.T) {
	for _, state := range []string{"active", "released", "network-active", "network-released"} {
		t.Run(state, func(t *testing.T) {
			emptyDomainInventory(t)
			store := newTestRunStore(t)
			leaseState := strings.TrimPrefix(state, "network-")
			var run ownedRun
			if leaseState != state {
				emptyNetworkInventory(t)
				var err error
				run, err = store.CreateNetworked(storeTestWork, networkTestRun, testResources, "10.240.0.0/30", nil)
				if err != nil {
					t.Fatal(err)
				}
			} else {
				run = createTestRun(t, store)
			}
			path := store.root.Name()
			artifact := filepath.Join(path, run.Record.RunID, "user-data")
			if err := os.WriteFile(artifact, []byte("synthetic assignment"), 0600); err != nil {
				t.Fatal(err)
			}
			store.Close()
			var registrations, inspections, reconciliations, claims atomic.Int32
			ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
			defer cancel()
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Header.Get("X-Kelpie-Lease") != "" || r.Header.Get("Authorization") == "" {
					t.Error("restart used a lost run token instead of its Worker credential")
				}
				switch {
				case strings.HasSuffix(r.URL.Path, "/register"):
					count := registrations.Add(1)
					if _, err := os.Stat(artifact); !errors.Is(err, os.ErrNotExist) {
						t.Error("registration preceded physical cleanup")
					}
					active := 0
					if count == 1 && leaseState == "active" {
						active = 1
					}
					if count > 1 && reconciliations.Load() != 1 {
						t.Error("capacity refresh preceded reconciliation")
					}
					_ = json.NewEncoder(w).Encode(recoveryWorker(active))
				case r.Method == http.MethodGet:
					inspections.Add(1)
					snapshot := leaseClientSnapshot()
					snapshot.LeaseID, snapshot.State = run.Record.RunID, leaseState
					_ = json.NewEncoder(w).Encode(snapshot)
				case strings.HasSuffix(r.URL.Path, "/reconcile"):
					reconciliations.Add(1)
					if inspections.Load() != 1 || registrations.Load() != 1 {
						t.Error("reconciliation did not follow exact lease inspection")
					}
					if _, err := os.Stat(filepath.Join(path, run.Record.RunID, "cleaned.json")); err != nil {
						t.Error("reconciliation preceded durable cleanup")
					}
					w.WriteHeader(http.StatusNoContent)
				case strings.HasSuffix(r.URL.Path, "/claim"):
					claims.Add(1)
					if registrations.Load() != 2 || reconciliations.Load() != 1 {
						t.Error("admission preceded refreshed remote capacity")
					}
					if _, err := os.Stat(filepath.Join(path, run.Record.RunID, "released.json")); err != nil {
						t.Error("admission preceded the durable release acknowledgement")
					}
					_, _ = w.Write([]byte("null"))
					cancel()
				default:
					t.Error("unexpected recovery request")
					w.WriteHeader(500)
				}
			}))
			defer server.Close()
			d := recoveryDaemon(server, path)
			if err := d.Run(ctx); !errors.Is(err, context.Canceled) {
				t.Fatalf("reconciled Worker did not resume admission: %v", err)
			}
			if claims.Load() == 0 || reconciliations.Load() != 1 || registrations.Load() != 2 {
				t.Fatal("recovery skipped its remote acknowledgement or capacity refresh")
			}
			reopened, err := openRunStore(path)
			if err != nil {
				t.Fatal("recovery leaked the exclusive lock")
			}
			defer reopened.Close()
			final, err := reopened.Load(run.Record.RunID)
			if err != nil || final.Phase != "released" || !reflect.DeepEqual(final.Record, run.Record) {
				t.Fatal("recovery did not retain identity with a durable release")
			}
		})
	}
}

func TestRestartRejectsUnprovenRecoveryWithoutAdmission(t *testing.T) {
	for _, scenario := range []string{
		"work-mismatch", "cpu-mismatch", "memory-mismatch", "disk-mismatch", "nonterminal",
		"inspect-denied", "inspect-missing", "reconcile-conflict", "reconcile-denied", "false-ack",
		"refresh-active", "refresh-identity", "refresh-empty", "refresh-capacity", "artifact-reappeared",
	} {
		t.Run(scenario, func(t *testing.T) {
			emptyDomainInventory(t)
			store := newTestRunStore(t)
			run := createTestRun(t, store)
			path := store.root.Name()
			store.Close()
			var registrations, reconciliations, unexpected atomic.Int32
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				switch {
				case strings.HasSuffix(r.URL.Path, "/register"):
					count := registrations.Add(1)
					worker := recoveryWorker(1)
					if count > 1 {
						worker = recoveryWorker(0)
						switch scenario {
						case "refresh-active":
							worker = recoveryWorker(1)
						case "refresh-identity":
							worker["id"] = "44444444-4444-4444-8444-444444444444"
						case "refresh-empty":
							return
						case "refresh-capacity":
							worker["cpu_available"] = 2
						}
					}
					_ = json.NewEncoder(w).Encode(worker)
				case r.Method == http.MethodGet:
					snapshot := leaseClientSnapshot()
					snapshot.LeaseID = run.Record.RunID
					switch scenario {
					case "work-mismatch":
						snapshot.WorkItemID = "44444444-4444-4444-8444-444444444444"
					case "cpu-mismatch":
						snapshot.CPU++
					case "memory-mismatch":
						snapshot.MemoryMB++
					case "disk-mismatch":
						snapshot.DiskGB++
					case "nonterminal":
						snapshot.WorkStatus = "awaiting_approval"
					case "inspect-denied":
						w.WriteHeader(http.StatusUnauthorized)
						return
					case "inspect-missing":
						w.WriteHeader(http.StatusNotFound)
						return
					case "artifact-reappeared":
						if err := os.WriteFile(filepath.Join(path, run.Record.RunID, "root.qcow2"), []byte("unproven new artifact"), 0600); err != nil {
							t.Error(err)
						}
					}
					_ = json.NewEncoder(w).Encode(snapshot)
				case strings.HasSuffix(r.URL.Path, "/reconcile"):
					reconciliations.Add(1)
					switch scenario {
					case "reconcile-conflict":
						w.WriteHeader(http.StatusConflict)
					case "reconcile-denied":
						w.WriteHeader(http.StatusForbidden)
					case "false-ack":
						w.WriteHeader(http.StatusOK)
					default:
						w.WriteHeader(http.StatusNoContent)
					}
				default:
					unexpected.Add(1)
					w.WriteHeader(http.StatusServiceUnavailable)
				}
			}))
			defer server.Close()
			ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
			defer cancel()
			if err := recoveryDaemon(server, path).Run(ctx); err == nil || errors.Is(err, context.DeadlineExceeded) {
				t.Fatalf("unproven recovery did not fail closed promptly: %v", err)
			}
			if unexpected.Load() != 0 {
				t.Fatal("unproven recovery published a heartbeat or accepted new work")
			}
			expectedPhase, expectedReconciliations, expectedRegistrations := "cleaned", int32(0), int32(1)
			if strings.HasPrefix(scenario, "reconcile-") || scenario == "false-ack" {
				expectedReconciliations = 1
			}
			if strings.HasPrefix(scenario, "refresh-") {
				expectedPhase, expectedReconciliations, expectedRegistrations = "released", 1, 2
			}
			if reconciliations.Load() != expectedReconciliations || registrations.Load() != expectedRegistrations {
				t.Fatal("failure crossed its allowed reconciliation boundary")
			}
			reopened, err := openRunStore(path)
			if err != nil {
				t.Fatal("failed recovery leaked its ownership lock")
			}
			defer reopened.Close()
			retained, err := reopened.Load(run.Record.RunID)
			if err != nil || retained.Phase != expectedPhase || retained.Record != run.Record {
				t.Fatal("failed recovery forged or lost its durable acknowledgement")
			}
			if scenario == "artifact-reappeared" {
				if _, err := os.Stat(filepath.Join(path, run.Record.RunID, "root.qcow2")); err != nil {
					t.Fatal("contradictory post-cleanup artifact was silently removed")
				}
			}
		})
	}
}

func TestRestartRetriesLostReconciliationAcknowledgement(t *testing.T) {
	emptyDomainInventory(t)
	store := newTestRunStore(t)
	run := createTestRun(t, store)
	path := store.root.Name()
	store.Close()
	var registrations, inspections, reconciliations, claims atomic.Int32
	var remoteReleased atomic.Bool
	var cancelAdmission atomic.Pointer[context.CancelFunc]
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case strings.HasSuffix(r.URL.Path, "/register"):
			registrations.Add(1)
			active := 1
			if remoteReleased.Load() {
				active = 0
			}
			_ = json.NewEncoder(w).Encode(recoveryWorker(active))
		case r.Method == http.MethodGet:
			inspections.Add(1)
			snapshot := leaseClientSnapshot()
			snapshot.LeaseID = run.Record.RunID
			if remoteReleased.Load() {
				snapshot.State = "released"
			}
			_ = json.NewEncoder(w).Encode(snapshot)
		case strings.HasSuffix(r.URL.Path, "/reconcile"):
			reconciliations.Add(1)
			if !remoteReleased.Swap(true) {
				// The real API's idempotence is covered by PostgreSQL tests. This
				// synthetic server models a committed return followed by a lost ACK.
				conn, _, err := w.(http.Hijacker).Hijack()
				if err != nil {
					t.Error(err)
					return
				}
				_ = conn.Close()
				return
			}
			w.WriteHeader(http.StatusNoContent)
		case strings.HasSuffix(r.URL.Path, "/claim"):
			claims.Add(1)
			if cancel := cancelAdmission.Load(); cancel != nil {
				(*cancel)()
			} else {
				t.Error("lost ACK admitted a new claim")
			}
			_, _ = w.Write([]byte("null"))
		default:
			t.Error("unexpected recovery request")
			w.WriteHeader(500)
		}
	}))
	defer server.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	if err := recoveryDaemon(server, path).Run(ctx); err == nil || errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("lost acknowledgement was not rejected: %v", err)
	}
	if claims.Load() != 0 || registrations.Load() != 1 || reconciliations.Load() != 1 {
		t.Fatal("lost acknowledgement published fresh capacity")
	}
	reopened, err := openRunStore(path)
	if err != nil {
		t.Fatal(err)
	}
	retained, err := reopened.Load(run.Record.RunID)
	reopened.Close()
	if err != nil || retained.Phase != "cleaned" {
		t.Fatal("lost acknowledgement forged a released marker")
	}
	for attempt := 0; attempt < 2; attempt++ {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		cancelAdmission.Store(&cancel)
		err := recoveryDaemon(server, path).Run(ctx)
		cancel()
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("retry did not restore admission: %v", err)
		}
	}
	if registrations.Load() != 4 || inspections.Load() != 2 || reconciliations.Load() != 2 {
		t.Fatal("retry failed to settle exactly once or repeated durable completed recovery")
	}
}
