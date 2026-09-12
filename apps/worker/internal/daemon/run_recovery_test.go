package daemon

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func emptyDomainInventory(t *testing.T) {
	t.Helper()
	dir := t.TempDir()
	body := "#!/bin/sh\ncase \"$*\" in\n'--connect qemu:///system list --all --uuid'|'--connect qemu:///system list --all --uuid --persistent') exit 0;;\n*) exit 9;;\nesac\n"
	if err := os.WriteFile(filepath.Join(dir, "virsh"), []byte(body), 0700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir)
}

func TestRecoveryCleansLegacyFilesButDoesNotInventRemoteRelease(t *testing.T) {
	emptyDomainInventory(t)
	store := newTestRunStore(t)
	run := createTestRun(t, store)
	path := store.root.Name()
	run.Record.Schema = 1
	legacy, err := json.Marshal(run.Record)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(path, run.Record.RunID, "run.json"), append(legacy, '\n'), 0600); err != nil {
		t.Fatal(err)
	}
	artifact := filepath.Join(path, run.Record.RunID, "user-data")
	if err := os.WriteFile(artifact, []byte("synthetic assignment"), 0600); err != nil {
		t.Fatal(err)
	}
	store.Close()
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) { calls.Add(1) }))
	defer server.Close()
	d := resourceDaemon(server)
	d.config.Executor, d.config.WorkRoot = "libvirt", path
	if err := d.Run(context.Background()); !errors.Is(err, errRunReconciliation) {
		t.Fatal("uncertain remote release did not block startup")
	}
	if calls.Load() != 0 {
		t.Fatal("recovery published capacity or requested a new claim")
	}
	if _, err := os.Stat(artifact); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("owned pending artifact was not cleaned")
	}
	reopened, err := openRunStore(path)
	if err != nil {
		t.Fatal("failed startup leaked the ownership lock")
	}
	defer reopened.Close()
	recovered, err := reopened.Load(run.Record.RunID)
	if err != nil || recovered.Phase != "cleaned" {
		t.Fatal("recovery forged a release marker or lost cleanup evidence")
	}
}

func TestRecoveryPreservesUnknownResourcesWithoutContactingControlPlane(t *testing.T) {
	path := filepath.Join(t.TempDir(), "runs")
	if err := os.Mkdir(path, 0700); err != nil {
		t.Fatal(err)
	}
	legacy := filepath.Join(path, "legacy-run")
	if err := os.Mkdir(legacy, 0700); err != nil {
		t.Fatal(err)
	}
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) { calls.Add(1) }))
	defer server.Close()
	d := resourceDaemon(server)
	d.config.Executor, d.config.WorkRoot = "libvirt", path
	if err := d.Run(context.Background()); !errors.Is(err, errRunStore) {
		t.Fatal("legacy directory did not block unproven recovery")
	}
	if calls.Load() != 0 {
		t.Fatal("unknown resources were advertised as free capacity")
	}
	if _, err := os.Stat(legacy); err != nil {
		t.Fatal("unowned directory was changed")
	}
}

func TestRemoteActiveRunsBlockNewClaimsAfterLocalStateLoss(t *testing.T) {
	var registrations, unexpected atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/register") {
			registrations.Add(1)
			_ = json.NewEncoder(w).Encode(recoveryWorker(1))
			return
		}
		unexpected.Add(1)
		w.WriteHeader(http.StatusServiceUnavailable)
	}))
	defer server.Close()
	d := recoveryDaemon(server, filepath.Join(t.TempDir(), "runs"))
	if err := d.Run(context.Background()); !errors.Is(err, errRunReconciliation) {
		t.Fatalf("unaccounted remote run did not stop admission: %v", err)
	}
	if registrations.Load() != 1 || unexpected.Load() != 0 {
		t.Fatal("worker heartbeat overwrote a remote reservation")
	}
}

type testLifecycle struct {
	cleanup  func(context.Context) error
	released func() error
}

func (l testLifecycle) Cleanup(ctx context.Context) error { return l.cleanup(ctx) }
func (l testLifecycle) Released() error                   { return l.released() }

func TestDaemonWaitsForCancelledExecutionCleanup(t *testing.T) {
	var claimed atomic.Bool
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case strings.HasSuffix(r.URL.Path, "/register"):
			_ = json.NewEncoder(w).Encode(Worker{ID: "test-worker"})
		case strings.HasSuffix(r.URL.Path, "/claim"):
			if claimed.Swap(true) {
				_, _ = w.Write([]byte("null"))
			} else {
				_ = json.NewEncoder(w).Encode(resourceClaim())
			}
		case r.Method == http.MethodGet:
			_ = json.NewEncoder(w).Encode(WorkItem{ID: "test-work", Status: "failed", Version: 3})
		default:
			w.WriteHeader(http.StatusNoContent)
		}
	}))
	defer server.Close()
	d := resourceDaemon(server)
	d.config.PollInterval = time.Millisecond
	started, cleaning, unblock := make(chan struct{}), make(chan struct{}), make(chan struct{})
	var once, unblockOnce sync.Once
	finish := func() { unblockOnce.Do(func() { close(unblock) }) }
	defer finish()
	var cleanupCancelled, released atomic.Bool
	d.executor = executorFunc(func(ctx context.Context, client RunClient, _ Claim) error {
		if err := client.BindResources(testLifecycle{
			cleanup: func(cleanCtx context.Context) error {
				cleanupCancelled.Store(cleanCtx.Err() != nil)
				once.Do(func() { close(cleaning) })
				<-unblock
				return nil
			}, released: func() error { released.Store(true); return nil },
		}); err != nil {
			return err
		}
		close(started)
		<-ctx.Done()
		return ctx.Err()
	})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- d.Run(ctx) }()
	select {
	case <-started:
	case <-time.After(2 * time.Second):
		t.Fatal("execution did not start")
	}
	cancel()
	select {
	case <-cleaning:
	case <-time.After(2 * time.Second):
		t.Fatal("cancellation did not begin cleanup")
	}
	select {
	case <-done:
		t.Fatal("daemon exited before its owned cleanup")
	case <-time.After(30 * time.Millisecond):
	}
	finish()
	select {
	case err := <-done:
		if !errors.Is(err, context.Canceled) {
			t.Fatal("daemon lost cancellation result")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("daemon did not join completed cleanup")
	}
	if cleanupCancelled.Load() || !released.Load() {
		t.Fatal("cancelled caller aborted cleanup or its acknowledged release")
	}
}

func TestAcknowledgedReleaseJournalFailureRetainsCapacityWithoutDuplicateAPI(t *testing.T) {
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { calls.Add(1); w.WriteHeader(http.StatusNoContent) }))
	defer server.Close()
	d := resourceDaemon(server)
	d.tracker.Reserve(testResources)
	client := &reservedRunClient{Client: d.client, daemon: d, claim: resourceClaim()}
	journalCalls := 0
	if err := client.BindResources(testLifecycle{cleanup: func(context.Context) error { return nil }, released: func() error {
		journalCalls++
		if journalCalls == 1 {
			return errors.New("synthetic journal failure")
		}
		return nil
	}}); err != nil {
		t.Fatal(err)
	}
	if err := client.Release(context.Background(), "test-work", "test-only-lease"); err == nil {
		t.Fatal("missing journal acknowledgement freed local capacity")
	}
	assertResources(t, d, Resources{}, 1)
	if err := client.Release(context.Background(), "test-work", "test-only-lease"); err != nil {
		t.Fatal(err)
	}
	if calls.Load() != 1 || journalCalls != 2 {
		t.Fatal("acknowledged remote release was repeated")
	}
	assertResources(t, d, testResources, 0)
}
