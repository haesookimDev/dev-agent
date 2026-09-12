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

func TestDedicatedLibvirtExistingFilterIsNotAdopted(t *testing.T) {
	dedicatedExistingFilter(t, false)
}

func TestDedicatedLibvirtExistingControlFilterIsNotAdopted(t *testing.T) {
	dedicatedExistingFilter(t, true)
}

func dedicatedExistingFilter(t *testing.T, controlled bool) {
	t.Helper()
	if os.Getenv("KELPIE_LIBVIRT_TEST_ACK") != "disposable-host-only" {
		t.Skip("requires explicit disposable-host acknowledgement")
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
	before, err := queryVirsh(ctx, "nwfilter-list")
	if err != nil {
		t.Fatal(err)
	}
	reader := hostNetworkReader()
	inventory, err := reader.read(ctx)
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
	identity, err := os.ReadFile("/proc/sys/kernel/random/uuid")
	if err != nil {
		t.Fatal(err)
	}
	var run ownedRun
	if controlled {
		control, err := parseGuestControl("https://control.example.test:8443", "192.0.2.7")
		if err != nil {
			t.Fatal(err)
		}
		run, err = store.CreateControlled(storeTestWork, strings.TrimSpace(string(identity)), testResources, "10.240.0.0/24", inventory.excluded, control)
	} else {
		run, err = store.CreateNetworked(storeTestWork, strings.TrimSpace(string(identity)), testResources, "10.240.0.0/24", inventory.excluded)
	}
	if err != nil || !inventory.available(*run.Record.Network) {
		t.Fatal("could not reserve an available fixture identity")
	}
	network := *run.Record.Network
	cleanup := newVMCleanup(store, run.Record.RunID)
	if found, err := cleanup.inspectFilter(ctx, network); err != nil || found {
		t.Fatal("fixture filter identity already exists; no mutation permitted")
	}
	t.Logf("preserved collision journal: %s/%s", root, run.Record.RunID)
	// The test, not the Worker provisioner, owns this deliberately pre-existing
	// filter. Its external definition must never count as Worker creation intent.
	body, err := network.policyXML()
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "external-filter.xml")
	if err := os.WriteFile(path, body, 0600); err != nil {
		t.Fatal(err)
	}
	defer func() {
		finish, stop := context.WithTimeout(context.Background(), time.Minute)
		defer stop()
		found, err := cleanup.inspectFilter(finish, network)
		if err != nil {
			t.Error("fixture filter identity unconfirmed; retained for investigation")
			return
		}
		if found {
			// Exact absent-before/create/identity evidence belongs to this test.
			// Production cleanup is required to refuse the same operation below.
			if _, err := cleanup.command(finish, "nwfilter-undefine", network.UUID); err != nil {
				t.Error("test-owned collision filter removal unconfirmed")
				return
			}
		}
		if err := cleanup.Cleanup(finish); err != nil {
			t.Error("collision reservation absence not confirmed")
			return
		}
		if err := cleanup.Released(); err != nil {
			t.Error("synthetic fixture release failed")
		}
		after, err := queryVirsh(finish, "nwfilter-list")
		if err != nil || string(before) != string(after) {
			t.Error("collision test changed pre-existing filter inventory")
		}
	}()
	if _, err := cleanup.command(ctx, "nwfilter-define", path, "--validate"); err != nil {
		t.Fatal("could not create the exact test-owned collision filter")
	}
	p := networkProvisioner{store: store, reader: reader}
	if err := p.create(ctx, run.Record.RunID); err == nil {
		t.Fatal("provisioning adopted a pre-existing filter")
	}
	if err := cleanup.Cleanup(ctx); err == nil {
		t.Fatal("cleanup adopted the rejected pre-existing filter")
	}
	if err := cleanup.Released(); err == nil {
		t.Fatal("released an unresolved filter collision")
	}
	if found, err := cleanup.inspectFilter(ctx, network); err != nil || !found {
		t.Fatal("pre-existing exact-match filter was not preserved")
	}
	if found, err := cleanup.inspectNetwork(ctx, network); err != nil || found {
		t.Fatal("rejected provisioning created a network")
	}
	for _, name := range []string{"network.xml", "filter.xml"} {
		if _, err := store.root.Lstat(run.Record.RunID + "/" + name); !os.IsNotExist(err) {
			t.Fatal("rejected provisioning fabricated creation intent")
		}
	}
	t.Log("actual pre-existing exact-match filter preserved after provisioning/cleanup rejection; reservation retained")
}
