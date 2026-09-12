package daemon

import (
	"context"
	"errors"
	"math"
	"strings"
	"testing"
	"time"
)

// Unimplemented mutation methods deliberately panic if the readiness observer
// attempts to transition, emit events, release, or provision resources itself.
type bootstrapClient struct {
	RunClient
	read func(context.Context, string, string) (WorkItem, error)
}

func (c bootstrapClient) ReadRun(ctx context.Context, id, lease string) (WorkItem, error) {
	return c.read(ctx, id, lease)
}

func bootstrapFixture(t *testing.T) (*cleanupFixture, Claim) {
	t.Helper()
	f := newCleanupFixture(t)
	if _, err := f.cleanup.store.Advance(f.run.Record.RunID, "running"); err != nil {
		t.Fatal(err)
	}
	claim := resourceClaim()
	claim.LeaseID = f.run.Record.RunID
	claim.WorkItem = WorkItem{ID: f.run.Record.WorkID, Status: "provisioning", Version: 2}
	return f, claim
}

func TestGuestBootstrapObservesAuthenticatedStateWithoutWorkerTransition(t *testing.T) {
	for _, status := range []string{"analyzing", "implementing", "verifying", "awaiting_approval", "awaiting_input", "awaiting_feedback", "budget_exhausted", "committing", "pr_created", "failed", "cancelled", "completed"} {
		t.Run(status, func(t *testing.T) {
			f, claim := bootstrapFixture(t)
			reads := 0
			client := bootstrapClient{read: func(ctx context.Context, id, lease string) (WorkItem, error) {
				if id != claim.WorkItem.ID || lease != claim.LeaseToken {
					t.Fatal("read escaped the claimed lease")
				}
				deadline, ok := ctx.Deadline()
				if !ok || time.Until(deadline) > time.Second {
					t.Fatal("read escaped the single startup deadline")
				}
				reads++
				if reads == 1 {
					return claim.WorkItem, nil
				}
				version := 4
				if status == "analyzing" {
					version = 3
				}
				return WorkItem{ID: id, Status: status, Version: version}, nil
			}}
			work, err := waitGuestBootstrap(context.Background(), client, claim, f.cleanup, time.Second, time.Millisecond)
			if err != nil || work.Status != status || reads != 2 || !f.exists || !f.running {
				t.Fatal("readiness observer failed or mutated physical resources", err)
			}
			for _, call := range f.calls {
				if call != "dumpxml" && call != "domstate" {
					t.Fatal("readiness observer issued a resource mutation")
				}
			}
		})
	}
}

func TestGuestBootstrapRejectsMismatchedStateAndLostVM(t *testing.T) {
	for _, scenario := range []string{"wrong-id", "zero-version", "stale-version", "advanced-provisioning", "stale-analyzing", "advanced-analyzing", "early-implementing", "unknown", "early-terminal", "read-error", "stopped", "missing-domain", "wrong-identity", "wrong-phase", "wrong-lease", "overflow-claim"} {
		t.Run(scenario, func(t *testing.T) {
			f, claim := bootstrapFixture(t)
			work := WorkItem{ID: claim.WorkItem.ID, Status: "analyzing", Version: 3}
			var readErr error
			switch scenario {
			case "wrong-id":
				work.ID = "another-work"
			case "zero-version":
				work.Version = 0
			case "stale-version":
				work.Version = 1
			case "advanced-provisioning":
				work.Status = "provisioning"
			case "stale-analyzing":
				work.Version = 2
			case "advanced-analyzing":
				work.Version = 4
			case "early-implementing":
				work.Status = "implementing"
			case "unknown":
				work.Status = "arbitrary"
			case "early-terminal":
				work.Status, work.Version = "failed", 2
			case "read-error":
				readErr = errors.New(privateFixture)
			case "stopped":
				f.running = false
			case "missing-domain":
				f.refuse = "dumpxml"
			case "wrong-identity":
				f.extraXML = "<uuid>another-domain</uuid>"
			case "wrong-phase":
				if _, err := f.cleanup.store.Advance(claim.LeaseID, "cleanup-pending"); err != nil {
					t.Fatal(err)
				}
			case "wrong-lease":
				claim.LeaseID = storeTestWork
			case "overflow-claim":
				claim.WorkItem.Version = math.MaxInt
			}
			client := bootstrapClient{read: func(context.Context, string, string) (WorkItem, error) { return work, readErr }}
			result, err := waitGuestBootstrap(context.Background(), client, claim, f.cleanup, time.Second, time.Millisecond)
			if err == nil || result.ID != "" || safeDiagnostic(err) != "VM Runner control bootstrap was not confirmed" || strings.Contains(err.Error(), privateFixture) {
				t.Fatal("unconfirmed bootstrap accepted or private error disclosed")
			}
		})
	}
}

func TestGuestBootstrapHasOneBoundedDeadlineIncludingBlockedRead(t *testing.T) {
	if guestBootstrapTimeout != 180*time.Second {
		t.Fatal("production startup deadline changed")
	}
	for _, mode := range []string{"provisioning", "blocked-read", "cancelled"} {
		t.Run(mode, func(t *testing.T) {
			f, claim := bootstrapFixture(t)
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			client := bootstrapClient{read: func(ctx context.Context, _, _ string) (WorkItem, error) {
				if mode == "cancelled" {
					cancel()
				}
				if mode == "blocked-read" {
					<-ctx.Done()
					return WorkItem{}, ctx.Err()
				}
				return claim.WorkItem, nil
			}}
			start := time.Now()
			_, err := waitGuestBootstrap(ctx, client, claim, f.cleanup, 25*time.Millisecond, time.Millisecond)
			if err == nil || time.Since(start) > time.Second || !f.exists || !f.running {
				t.Fatal("startup wait escaped its bound or performed uncoordinated cleanup")
			}
		})
	}
}
