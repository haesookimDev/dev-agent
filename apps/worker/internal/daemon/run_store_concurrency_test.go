package daemon

import (
	"fmt"
	"sync"
	"testing"
)

func TestRunStoreConcurrentCreationAndInventory(t *testing.T) {
	store := newTestRunStore(t)
	start := make(chan struct{})
	var tasks sync.WaitGroup
	for index := 0; index < 16; index++ {
		tasks.Add(1)
		go func(index int) {
			defer tasks.Done()
			<-start
			lease := fmt.Sprintf("dd2031f8-1725-49d1-8b06-%012x", index)
			if _, err := store.Create(storeTestWork, lease, testResources); err != nil {
				t.Error(err)
			}
			if _, err := store.List(); err != nil {
				t.Error("inventory observed an incomplete concurrent creation")
			}
		}(index)
	}
	close(start)
	tasks.Wait()
	runs, err := store.List()
	if err != nil || len(runs) != 16 {
		t.Fatal("concurrent creation lost a record")
	}
}

func TestRunStoreConcurrentPhaseAdvanceIsIdempotent(t *testing.T) {
	store := newTestRunStore(t)
	run := createTestRun(t, store)
	var tasks sync.WaitGroup
	for index := 0; index < 16; index++ {
		tasks.Add(1)
		go func() {
			defer tasks.Done()
			if _, err := store.Advance(run.Record.RunID, "cleanup-pending"); err != nil {
				t.Error("same phase raced its exclusive marker write")
			}
		}()
	}
	tasks.Wait()
	loaded, err := store.Load(run.Record.RunID)
	if err != nil || loaded.Phase != "cleanup-pending" {
		t.Fatal("concurrent phase advance lost its durable state")
	}
}
