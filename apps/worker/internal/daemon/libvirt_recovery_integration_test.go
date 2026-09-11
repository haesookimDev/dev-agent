//go:build linux && libvirt_integration

package daemon

import (
	"context"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
	"time"
)

// Recover only a preserved synthetic run from TestDedicatedLibvirtCleanupBeforeRelease.
// This is not a production cleanup CLI and never fabricates an API acknowledgement.
func TestDedicatedLibvirtRecordedRecovery(t *testing.T) {
	if os.Getenv("KELPIE_LIBVIRT_TEST_ACK") != "disposable-host-only" || os.Getenv("KELPIE_LIBVIRT_TEST_RECOVER") == "" {
		t.Skip("requires explicit disposable-host acknowledgement and one preserved test root")
	}
	path := os.Getenv("KELPIE_LIBVIRT_TEST_RECOVER")
	if os.Geteuid() == 0 || filepath.Clean(path) != path || filepath.Dir(path) != "/var/tmp" ||
		!regexp.MustCompile(`^kelpie-lifecycle-[0-9]+$`).MatchString(filepath.Base(path)) {
		t.Fatal("recovery target is not an unprivileged disposable test run")
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
	defer cancel()
	data, err := queryVirsh(ctx, "list", "--all", "--uuid")
	if err != nil || strings.TrimSpace(string(data)) != "" {
		t.Fatal("recorded recovery requires a successful empty domain inventory")
	}
	store, err := openRunStore(path)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	runs, err := store.List()
	if err != nil || len(runs) != 1 || runs[0].Record.WorkID != storeTestWork || runs[0].Record.Resources != testResources {
		t.Fatal("preserved record does not match the synthetic lifecycle fixture")
	}
	if runs[0].Phase == "released" {
		t.Fatal("completed evidence does not need recovery")
	}
	cleanup := newVMCleanup(store, runs[0].Record.RunID)
	if err := cleanup.Cleanup(ctx); err != nil {
		t.Fatal(err)
	}
	run, err := store.Load(runs[0].Record.RunID)
	if err != nil || run.Phase != "cleaned" {
		t.Fatal("recovery lost cleanup evidence or fabricated release")
	}
	t.Log("preserved synthetic artifacts cleaned; journal retained; no API release invented")
}
