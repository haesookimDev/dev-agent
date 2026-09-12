package daemon

import (
	"context"
	"net/netip"
	"os"
	"path/filepath"
	"testing"
)

func TestWorkerReadsRootLoggingLifecycleWithoutPrivilegedCommands(t *testing.T) {
	f := newLogHookFixture(t)
	network := internetNetworkFixture(t)
	f.network, _ = networkAt(network.UUID, netip.MustParsePrefix(network.CIDR))
	f.body = networkLogExpected(f.network, 123, 1, 2)
	// In production this reader is root-owned/read-only. The unit fixture uses
	// the current UID only to construct immutable files without privileges.
	reader := &networkLogHook{root: f.hook.root, owner: f.hook.owner, bootID: f.hook.bootID}
	if err := reader.verifyReceipt(network, "absent", ""); err != nil {
		t.Fatal(err)
	}
	if err := reader.verifyReceipt(network, "active", reader.bootID); err == nil {
		t.Fatal("missing logger accepted")
	}
	if err := f.apply("start", "begin"); err != nil {
		t.Fatal(err)
	}
	if err := reader.verifyReceipt(network, "absent", ""); err == nil {
		t.Fatal("stale UUID receipt admitted")
	}
	if err := reader.verifyReceipt(network, "active", reader.bootID); err != nil {
		t.Fatal("exact new logging identity was rejected", err)
	}
	if err := reader.verifyReceipt(network, "stopped", reader.bootID); err == nil {
		t.Fatal("unfinished logger accepted as cleaned")
	}
	reader.bootID = "00000000-0000-4000-8000-000000000001"
	if err := reader.verifyReceipt(network, "active", f.hook.bootID); err == nil {
		t.Fatal("previous-boot active receipt admitted")
	}
	if err := f.apply("stopped", "end"); err != nil {
		t.Fatal(err)
	}
	if err := reader.verifyReceipt(network, "stopped", f.hook.bootID); err != nil {
		t.Fatal("completed pre-reboot lifecycle was lost", err)
	}
	if err := reader.verifyReceipt(network, "active", f.hook.bootID); err == nil {
		t.Fatal("stopped logger admitted")
	}
	path := filepath.Join(f.hook.root.Name(), network.UUID+".stopped.json")
	if err := os.Chmod(path, 0666); err != nil {
		t.Fatal(err)
	}
	if err := reader.verifyReceipt(network, "stopped", f.hook.bootID); err == nil {
		t.Fatal("writable completion receipt accepted")
	}
}

func TestInternetStartIntentIsExclusiveAndSurvivesFailedStart(t *testing.T) {
	p, f := internetProvisionFixture(t)
	p.logReceipt = func(runNetwork, string, string) (string, error) { return networkTestRun, nil }
	f.refuse = "net-start"
	if err := p.create(context.Background(), networkTestRun); err == nil {
		t.Fatal("failed network start admitted a NIC")
	}
	intent, exists, err := p.store.networkIntent(networkTestRun)
	if err != nil || !exists || intent.BootID != networkTestRun {
		t.Fatal("failed start lost its durable intent")
	}
	if p.store.beginNetwork(networkTestRun, networkTestRun) == nil {
		t.Fatal("start intent was overwritten")
	}
	f.refuse = ""
	f.cleanup.logReceipt = func(_ runNetwork, phase, boot string) (string, error) {
		if phase != "stopped" || boot != networkTestRun {
			t.Fatal("uncertain start was mistaken for never-started networking")
		}
		return "", errRunNetwork
	}
	if f.cleanup.Cleanup(context.Background()) == nil {
		t.Fatal("missing stop evidence released an uncertain start")
	}
	path := filepath.Join(p.store.root.Name(), networkTestRun, "network-start.json")
	if err := os.WriteFile(path, []byte(`{"run_id":"invalid"}`), 0600); err != nil {
		t.Fatal(err)
	}
	if _, _, err := p.store.networkIntent(networkTestRun); err == nil || f.cleanup.Cleanup(context.Background()) == nil {
		t.Fatal("corrupted intent became absence or completion evidence")
	}
}

func TestInternetCleanupBeforeStartRequiresUnusedLoggingIdentity(t *testing.T) {
	p, f := internetProvisionFixture(t)
	f.cleanup.logReceipt = func(_ runNetwork, phase, boot string) (string, error) {
		if phase != "unused" || boot != "" {
			t.Fatal("never-started run required a fabricated root stop receipt")
		}
		return networkTestRun, nil
	}
	if err := f.cleanup.Cleanup(context.Background()); err != nil {
		t.Fatal("never-started allocation could not be reclaimed", err)
	}
	if _, exists, err := p.store.networkIntent(networkTestRun); err != nil || exists {
		t.Fatal("cleanup manufactured a start intent")
	}
}

func TestLegacyCleanupDoesNotAdoptInternetStartIntent(t *testing.T) {
	f := newNetworkCleanupFixture(t)
	path := filepath.Join(f.cleanup.store.root.Name(), networkTestRun, "network-start.json")
	if err := os.WriteFile(path, []byte("unowned legacy file"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := f.cleanup.Cleanup(context.Background()); err == nil {
		t.Fatal("legacy cleanup adopted new-schema metadata")
	}
	if data, err := os.ReadFile(path); err != nil || string(data) != "unowned legacy file" {
		t.Fatal("legacy cleanup modified an unknown file")
	}
}
