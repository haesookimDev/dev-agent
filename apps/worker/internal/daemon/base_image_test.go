package daemon

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"testing"
)

func TestBaseCapacityValidatesMetadataAndReservedSize(t *testing.T) {
	for _, test := range []struct {
		name, body string
		disk       int
		valid      bool
	}{
		{"equal", `{"format":"qcow2","virtual-size":42949672960}`, 40, true},
		{"larger", `{"format":"qcow2","virtual-size":42949672960}`, 41, true},
		{"smaller", `{"format":"qcow2","virtual-size":42949672960}`, 39, false},
		{"zero reservation", `{"format":"qcow2","virtual-size":1}`, 0, false},
		{"missing size", `{"format":"qcow2"}`, 40, false},
		{"negative size", `{"format":"qcow2","virtual-size":-1}`, 40, false},
		{"raw", `{"format":"raw","virtual-size":1}`, 40, false},
		{"overflow", `{"format":"qcow2","virtual-size":9223372036854775808}`, 40, false},
		{"malformed", `private-metadata-canary`, 40, false},
	} {
		t.Run(test.name, func(t *testing.T) {
			directory := t.TempDir()
			t.Setenv("PATH", directory)
			script := fmt.Sprintf("#!/bin/sh\nprintf '%%s' '%s'\n", test.body)
			if err := os.WriteFile(filepath.Join(directory, "qemu-img"), []byte(script), 0700); err != nil {
				t.Fatal(err)
			}
			err := validateBaseCapacity(context.Background(), "/synthetic/base.qcow2", test.disk)
			if (err == nil) != test.valid {
				t.Fatal("unexpected capacity decision")
			}
			if err != nil && err.Error() != "VM base image unavailable" && err.Error() != "VM disk reservation is smaller than the base image" {
				t.Fatal("private metadata escaped the diagnostic boundary")
			}
		})
	}
}

func TestExecutorRejectsDiskSmallerThanBaseBeforeAllocation(t *testing.T) {
	emptyDomainInventory(t)
	store := newTestRunStore(t)
	commands := os.Getenv("PATH")
	if err := os.WriteFile(filepath.Join(commands, "qemu-img"), []byte(`#!/bin/sh
case "$1" in
check) exit 0;;
info) printf '%s' '{"format":"qcow2","virtual-size":42949672960}'; exit 0;;
*) exit 9;;
esac
`), 0700); err != nil {
		t.Fatal(err)
	}
	base := filepath.Join(commands, "base.qcow2")
	if err := os.WriteFile(base, []byte("synthetic metadata fixture"), 0600); err != nil {
		t.Fatal(err)
	}
	reader := inventoryFixture(t)
	executor := LibvirtExecutor{store: store, networkReader: &reader, config: Config{
		BaseImage: base, GuestControlURL: "https://control.example.test", GuestControlIPv4: "192.0.2.7",
		NetworkPool: "10.240.0.0/24", RunResources: Resources{CPU: 2, MemoryMB: 2048, DiskGB: 20},
	}}
	claim := resourceClaim()
	claim.WorkItem.ID = storeTestWork
	claim.WorkItem.Status, claim.WorkItem.Version = "provisioning", 2
	err := executor.Execute(context.Background(), &reservedRunClient{}, claim)
	runs, listErr := store.List()
	if err == nil || err.Error() != "VM disk reservation is smaller than the base image" || listErr != nil || len(runs) != 0 {
		t.Fatal("undersized overlay was not rejected before run allocation")
	}
}
