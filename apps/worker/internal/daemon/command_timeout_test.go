package daemon

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestVMCommandHasItsOwnDeadline(t *testing.T) {
	start := time.Now()
	err := runWithLimit(context.Background(), 40*time.Millisecond, "sh", "-c", "sleep 0.3")
	if !errors.Is(err, context.DeadlineExceeded) || time.Since(start) >= 2*time.Second {
		t.Fatal("VM command exceeded its own time limit or lost deadline classification")
	}
}

func TestVMCommandDoesNotExtendParentDeadline(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 40*time.Millisecond)
	defer cancel()
	if !errors.Is(runWithLimit(ctx, time.Second, "sh", "-c", "sleep 0.3"), context.DeadlineExceeded) {
		t.Fatal("per-command limit extended the earlier parent deadline")
	}
}

func TestVMCommandExpiredLimitNeverStarts(t *testing.T) {
	for _, limit := range []time.Duration{0, -time.Second} {
		marker := filepath.Join(t.TempDir(), "started")
		err := runWithLimit(context.Background(), limit, "sh", "-c", `printf started > "$1"`, "fixture", marker)
		if !errors.Is(err, context.DeadlineExceeded) {
			t.Fatal("expired command budget did not fail with a deadline")
		}
		if _, err := os.Stat(marker); !errors.Is(err, os.ErrNotExist) {
			t.Fatal("command ran after its budget expired")
		}
	}
}

func TestVMCommandCancellationStopsOrdinaryDescendants(t *testing.T) {
	directory := t.TempDir()
	ready, survived := filepath.Join(directory, "ready"), filepath.Join(directory, "survived")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() {
		done <- run(ctx, "sh", "-c", `sh -c 'printf ready > "$1"; sleep 1; printf survived > "$2"' child "$1" "$2" & wait`, "parent", ready, survived)
	}()
	deadline := time.Now().Add(3 * time.Second)
	for {
		if _, err := os.Stat(ready); err == nil {
			break
		}
		if time.Now().After(deadline) {
			cancel()
			<-done
			t.Fatal("owned child did not start")
		}
		time.Sleep(5 * time.Millisecond)
	}
	cancel()
	select {
	case err := <-done:
		if !errors.Is(err, context.Canceled) {
			t.Fatal("command lost cancellation classification")
		}
	case <-time.After(time.Second):
		t.Fatal("command did not stop after cancellation")
	}
	// The child deliberately emits a witness if it survives its parent. Its
	// bounded one-second body then exits even on a regression; no PID is reused
	// or unrelated process signalled by this test's cleanup.
	time.Sleep(1200 * time.Millisecond)
	if _, err := os.Stat(survived); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("command descendant survived cancellation and performed later work")
	}
}
