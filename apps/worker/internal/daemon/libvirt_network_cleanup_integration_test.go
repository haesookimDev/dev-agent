//go:build linux && libvirt_integration

package daemon

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// Creates only recorded networks/filters on an explicitly disposable host.
// No guest is attached, so this verifies lifecycle, not packet isolation.
func TestDedicatedLibvirtNetworkCleanup(t *testing.T) {
	if os.Getenv("KELPIE_LIBVIRT_TEST_ACK") != "disposable-host-only" {
		t.Skip("requires libvirt_integration tag and explicit disposable-host acknowledgement")
	}
	if os.Geteuid() == 0 {
		t.Fatal("run as the unprivileged Worker user")
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
	defer cancel()
	domains, err := queryVirsh(ctx, "list", "--all", "--uuid")
	if err != nil || strings.TrimSpace(string(domains)) != "" {
		t.Fatal("requires an empty dedicated-host domain inventory")
	}
	for _, active := range []bool{false, true} {
		name := "defined"
		if active {
			name = "active"
		}
		if !t.Run(name, func(t *testing.T) { realNetworkCleanup(t, ctx, active) }) {
			break
		}
	}
}

// Resume a journal explicitly retained by the dedicated fixture in a new test
// process. Never scan /var/tmp or infer ownership from a resource-name prefix.
func TestDedicatedLibvirtNetworkRecovery(t *testing.T) {
	root := os.Getenv("KELPIE_LIBVIRT_NETWORK_RECOVERY_ROOT")
	if os.Getenv("KELPIE_LIBVIRT_TEST_ACK") != "disposable-host-only" || root == "" {
		t.Skip("requires explicit disposable-host acknowledgement and a retained fixture root")
	}
	if os.Geteuid() == 0 || filepath.Dir(root) != "/var/tmp" || !strings.HasPrefix(filepath.Base(root), "kelpie-network-") {
		t.Fatal("requires the exact retained fixture root and unprivileged Worker")
	}
	if _, err := os.Lstat(root); err != nil {
		t.Fatal("the retained fixture must already exist")
	}
	store, err := openRunStore(root)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	runs, err := store.List()
	if err != nil || len(runs) != 1 || runs[0].Record.Schema != 3 || runs[0].Record.WorkID != storeTestWork {
		t.Fatal("requires one canonical fixture network journal")
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
	defer cancel()
	cleanup := newVMCleanup(store, runs[0].Record.RunID)
	if err := cleanup.Cleanup(ctx); err != nil {
		t.Fatal(err)
	}
	if err := cleanup.Released(); err != nil {
		t.Fatal(err)
	}
	t.Log("new process recovered recorded network/filter/bridge cleanup; synthetic release acknowledged")
}

func realNetworkCleanup(t *testing.T, ctx context.Context, active bool) {
	before, err := queryVirsh(ctx, "net-list", "--all", "--uuid")
	if err != nil {
		t.Fatal(err)
	}
	root, err := os.MkdirTemp("/var/tmp", "kelpie-network-")
	if err != nil {
		t.Fatal(err)
	}
	store, err := openRunStore(root)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	// The UUID comes from the dedicated host's random source, not user input.
	data, err := os.ReadFile("/proc/sys/kernel/random/uuid")
	if err != nil {
		t.Fatal(err)
	}
	run, err := store.CreateNetworked(storeTestWork, strings.TrimSpace(string(data)), testResources, "10.240.0.0/24", nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("preserved network ownership journal: %s/%s", root, run.Record.RunID)
	cleanup := newVMCleanup(store, run.Record.RunID)
	defer func() {
		finish, cancel := context.WithTimeout(context.Background(), time.Minute)
		defer cancel()
		if err := cleanup.Cleanup(finish); err != nil {
			t.Errorf("owned network cleanup unconfirmed; retain the journal: %v", err)
		}
	}()
	network := *run.Record.Network
	for _, definition := range []struct {
		command string
		body    func() ([]byte, error)
	}{{"nwfilter-define", network.quarantineXML}, {"net-define", network.definitionXML}} {
		body, err := definition.body()
		if err != nil {
			t.Fatal(err)
		}
		path := filepath.Join(t.TempDir(), definition.command+".xml")
		if err := os.WriteFile(path, body, 0600); err != nil {
			t.Fatal(err)
		}
		if _, err := cleanup.command(ctx, definition.command, path, "--validate"); err != nil {
			t.Fatalf("generated definition rejected: %s", definition.command)
		}
	}
	if exists, err := cleanup.inspectNetwork(ctx, network); err != nil || !exists {
		dump, _ := queryVirsh(ctx, "net-dumpxml", network.UUID)
		t.Fatalf("defined owned network did not match: %v\n%s", err, dump)
	}
	if exists, err := cleanup.inspectFilter(ctx, network); err != nil || !exists {
		dump, _ := queryVirsh(ctx, "nwfilter-dumpxml", network.UUID)
		t.Fatalf("defined quarantine did not match: %v\n%s", err, dump)
	}
	if active {
		if _, err := cleanup.command(ctx, "net-start", network.UUID); err != nil {
			t.Fatal("owned network did not start")
		}
		if err := cleanup.bridgeAbsent(network); err == nil {
			t.Fatal("active owned bridge was not observed")
		}
	}
	if err := cleanup.Cleanup(ctx); err != nil {
		t.Fatal(err)
	}
	if err := cleanup.Released(); err != nil {
		t.Fatal(err)
	}
	final, err := store.Load(run.Record.RunID)
	if err != nil || final.Phase != "released" {
		t.Fatal("cleanup did not retain its durable acknowledgement")
	}
	after, err := queryVirsh(ctx, "net-list", "--all", "--uuid")
	if err != nil || string(before) != string(after) {
		t.Fatal("network cleanup changed pre-existing inventory")
	}
	t.Log("owned network/filter/bridge absent; pre-existing networks preserved; durable released")
}
