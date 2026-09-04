package daemon

import "testing"

func TestTrackerPreventsOvercommit(t *testing.T) {
	tracker := NewTracker(Resources{CPU: 4, MemoryMB: 8192, DiskGB: 60})
	run := Resources{CPU: 2, MemoryMB: 4096, DiskGB: 30}
	if !tracker.Reserve(run) || !tracker.Reserve(run) {
		t.Fatal("expected two reservations to fit")
	}
	if tracker.Reserve(run) {
		t.Fatal("third reservation must not overcommit")
	}
	tracker.Release(run)
	available, active := tracker.Available()
	if active != 1 || available.CPU != 2 || available.MemoryMB != 4096 || available.DiskGB != 30 {
		t.Fatalf("unexpected resources: %+v active=%d", available, active)
	}
}
