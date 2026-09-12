package daemon

import (
	"encoding/json"
	"fmt"
	"net/netip"
	"os"
	"path/filepath"
	"reflect"
	"sync"
	"testing"
)

func emptyNetworkInventory(t *testing.T) {
	t.Helper()
	dir := t.TempDir()
	body := `#!/bin/sh
case "$*" in
'--connect qemu:///system list --all --uuid'|'--connect qemu:///system list --all --uuid --persistent'|'--connect qemu:///system net-list --uuid --all'|'--connect qemu:///system net-list --uuid --all --persistent'|'--connect qemu:///system net-list --uuid --all --autostart') exit 0;;
'--connect qemu:///system nwfilter-list') printf ' UUID Name\n----------\n';;
'--connect qemu:///system nwfilter-binding-list') printf ' Port Dev Filter\n----------\n';;
*) exit 9;;
esac
`
	if err := os.WriteFile(filepath.Join(dir, "virsh"), []byte(body), 0700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir)
}

func TestRunNetworkStorePersistsConcurrentReservations(t *testing.T) {
	store := newTestRunStore(t)
	var tasks sync.WaitGroup
	for index := 0; index < 16; index++ {
		tasks.Add(1)
		go func(index int) {
			defer tasks.Done()
			id := fmt.Sprintf("dd2031f8-1725-49d1-8b06-%012x", index)
			if _, err := store.CreateNetworked(storeTestWork, id, testResources, "10.240.0.0/26", nil); err != nil {
				t.Error(err)
			}
		}(index)
	}
	tasks.Wait()
	runs, err := store.List()
	if err != nil || len(runs) != 16 {
		t.Fatal("lost concurrent network ownership")
	}
	subnets := map[string]bool{}
	for _, run := range runs {
		if run.Record.Schema != 3 || run.Record.Network == nil || !run.Record.Network.valid(run.Record.RunID) || subnets[run.Record.Network.CIDR] {
			t.Fatal("invalid or duplicated network ownership")
		}
		subnets[run.Record.Network.CIDR] = true
	}
	if _, err := store.CreateNetworked(storeTestWork, networkTestRun, testResources, "10.240.0.0/26", nil); err == nil {
		t.Fatal("overcommitted a full pool")
	}
	if _, err := os.Stat(filepath.Join(store.root.Name(), networkTestRun)); !os.IsNotExist(err) {
		t.Fatal("failed allocation left a partial ownership directory")
	}
	path := store.root.Name()
	store.Close()
	reopened, err := openRunStore(path)
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	loaded, err := reopened.List()
	if err != nil || !reflect.DeepEqual(loaded, runs) {
		t.Fatal("network identity changed after restart")
	}
}

func TestRunNetworkStoreRetainsReservationUntilReleased(t *testing.T) {
	store := newTestRunStore(t)
	run, err := store.CreateNetworked(storeTestWork, networkTestRun, testResources, "10.240.0.0/30", nil)
	if err != nil {
		t.Fatal(err)
	}
	other := "aa2031f8-1725-49d1-8b06-1422de3485b4"
	for _, phase := range []string{"prepared", "cleanup-pending", "cleaned"} {
		if phase != "prepared" {
			if _, err := store.Advance(run.Record.RunID, phase); err != nil {
				t.Fatal(err)
			}
		}
		if _, err := store.CreateNetworked(storeTestWork, other, testResources, "10.240.0.0/30", nil); err == nil {
			t.Fatalf("reused an unacknowledged %s reservation", phase)
		}
	}
	if _, err := store.Advance(run.Record.RunID, "released"); err != nil {
		t.Fatal(err)
	}
	if _, err := store.CreateNetworked(storeTestWork, other, testResources, "10.240.0.0/30", nil); err != nil {
		t.Fatal("released address not available to a new identity")
	}
	if _, err := store.CreateNetworked(storeTestWork, networkTestRun, testResources, "10.240.1.0/30", nil); err == nil {
		t.Fatal("reused an already recorded lease UUID")
	}
}

func TestRunNetworkStoreRejectsLegacyAndHostConflicts(t *testing.T) {
	store := newTestRunStore(t)
	legacy := createTestRun(t, store)
	if _, err := store.CreateNetworked(storeTestWork, networkTestRun, testResources, "10.240.0.0/30", nil); err == nil {
		t.Fatal("treated unresolved legacy network ownership as empty")
	}
	for _, phase := range []string{"cleanup-pending", "cleaned", "released"} {
		if _, err := store.Advance(legacy.Record.RunID, phase); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := store.CreateNetworked(storeTestWork, networkTestRun, testResources, "10.240.0.0/30", []netip.Prefix{netip.MustParsePrefix("10.240.0.0/24")}); err == nil {
		t.Fatal("ignored host address exclusion")
	}
	if _, err := store.CreateNetworked(storeTestWork, networkTestRun, testResources, "10.240.0.0/30", nil); err != nil {
		t.Fatal(err)
	}
}

func TestRunNetworkStoreRejectsIncompleteOrCrossSchemaIdentity(t *testing.T) {
	for _, scenario := range []string{"missing", "foreign", "legacy-with-network", "unsupported"} {
		t.Run(scenario, func(t *testing.T) {
			store := newTestRunStore(t)
			run, err := store.CreateNetworked(storeTestWork, networkTestRun, testResources, "10.240.0.0/30", nil)
			if err != nil {
				t.Fatal(err)
			}
			switch scenario {
			case "missing":
				run.Record.Network = nil
			case "foreign":
				run.Record.Network.UUID = storeTestWork
			case "legacy-with-network":
				run.Record.Schema = 2
			case "unsupported":
				run.Record.Schema = 4
			}
			data, _ := json.Marshal(run.Record)
			if err := os.WriteFile(filepath.Join(store.root.Name(), networkTestRun, "run.json"), append(data, '\n'), 0600); err != nil {
				t.Fatal(err)
			}
			if _, err := store.Load(networkTestRun); err == nil {
				t.Fatal("accepted incomplete or cross-schema network identity")
			}
		})
	}
}
