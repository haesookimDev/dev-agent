package daemon

import (
	"context"
	"math"
	"slices"
	"strings"
	"time"
)

const guestBootstrapTimeout = 180 * time.Second

// The guest, not virt-install or the Worker, performs the authenticated
// provisioning -> analyzing transition. One deadline covers OS boot and the
// first control connection. There is no second unbounded readiness wait.
func waitGuestBootstrap(ctx context.Context, client RunClient, claim Claim, cleanup *vmCleanup, limit, poll time.Duration) (WorkItem, error) {
	failure := diagnosticError{kind: vmControlBootstrap}
	if claim.WorkItem.Status != "provisioning" || claim.WorkItem.Version < 1 || claim.WorkItem.Version == math.MaxInt ||
		cleanup == nil || cleanup.runID != claim.LeaseID || limit <= 0 || poll <= 0 {
		return WorkItem{}, failure
	}
	ctx, cancel := context.WithTimeout(ctx, limit)
	defer cancel()
	for {
		readCtx, stopRead := context.WithTimeout(ctx, 5*time.Second)
		work, err := client.ReadRun(readCtx, claim.WorkItem.ID, claim.LeaseToken)
		stopRead()
		if err != nil || ctx.Err() != nil || work.ID != claim.WorkItem.ID || work.Version < claim.WorkItem.Version {
			return WorkItem{}, failure
		}
		if terminalRun(work.Status) && work.Version > claim.WorkItem.Version {
			// A fast clone failure/cancellation needs cleanup, not a false ready
			// event or a second failure transition from the Worker.
			return work, nil
		}
		ready := work.Status == "analyzing" && work.Version == claim.WorkItem.Version+1 ||
			slices.Contains([]string{"implementing", "verifying", "awaiting_approval", "awaiting_input", "awaiting_feedback", "budget_exhausted", "committing", "pr_created"}, work.Status) && work.Version > claim.WorkItem.Version+1
		if !ready && (work.Status != "provisioning" || work.Version != claim.WorkItem.Version) {
			return WorkItem{}, failure
		}
		run, err := cleanup.store.Load(claim.LeaseID)
		if err != nil || run.Phase != "running" || run.Record.WorkID != claim.WorkItem.ID {
			return WorkItem{}, failure
		}
		if _, err := cleanup.verifyDomain(ctx, run.Record); err != nil {
			return WorkItem{}, failure
		}
		state, err := cleanup.command(ctx, "domstate", claim.LeaseID)
		if err != nil || !slices.Contains([]string{"running", "idle"}, strings.TrimSpace(string(state))) {
			return WorkItem{}, failure
		}
		if ready {
			return work, nil
		}
		timer := time.NewTimer(poll)
		select {
		case <-ctx.Done():
			timer.Stop()
			return WorkItem{}, failure
		case <-timer.C:
		}
	}
}

func terminalRun(status string) bool {
	return status == "completed" || status == "failed" || status == "cancelled"
}
