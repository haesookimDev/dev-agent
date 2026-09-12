package daemon

import (
	"context"
	"encoding/json"
	"net/netip"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

func controlNetworkFixture(t *testing.T) runNetwork {
	t.Helper()
	network, err := networkAt(networkTestRun, netip.MustParsePrefix("10.240.0.0/30"))
	if err != nil {
		t.Fatal(err)
	}
	network.Version, network.ControlOrigin, network.ControlIPv4 = 2, "https://control.example.test:8443", "192.0.2.7"
	return network
}

func TestControlPolicyBindsOnlyRecordedEndpoint(t *testing.T) {
	network := controlNetworkFixture(t)
	data, err := network.policyXML()
	if err != nil {
		t.Fatal(err)
	}
	node, err := parseNetworkXML(data, "filter")
	if err != nil || len(node.Children) != 12 {
		t.Fatal("invalid bounded control policy")
	}
	if strings.Contains(string(data), "filterref") || strings.Contains(string(data), "<udp") || strings.Contains(string(data), "<ipv6") {
		t.Fatal("control policy introduced unrelated egress")
	}
	// Regression from libvirt 10: it emits 0x800, not the input spelling
	// 0x0800. Generate its canonical spelling without weakening XML identity.
	if strings.Count(string(data), "protocoltype='0x800'") != 4 {
		t.Fatal("ARP protocol spelling will fail the real libvirt round trip")
	}
	if !strings.Contains(string(data), "dstipaddr='192.0.2.7' dstportstart='8443' dstportend='8443'") ||
		!strings.Contains(string(data), "priority='1000' statematch='false'><all/>") {
		t.Fatal("missing exact TCP endpoint or final unconditional drop")
	}
	seed, err := network.guestNetworkConfig()
	if err != nil || !strings.Contains(string(seed), "addresses: ['10.240.0.2/30']") || !strings.Contains(string(seed), "via: '10.240.0.1'") || !strings.Contains(string(seed), "dhcp4: false") {
		t.Fatal("static guest config did not match policy")
	}
	legacy, _ := networkAt(network.UUID, netip.MustParsePrefix(network.CIDR))
	policy, _ := legacy.policyXML()
	quarantine, _ := legacy.quarantineXML()
	if string(policy) != string(quarantine) {
		t.Fatal("legacy record acquired egress")
	}
	for _, altered := range []runNetwork{
		{Version: 2, UUID: network.UUID, CIDR: network.CIDR},
		func() runNetwork { n := network; n.Version = 1; return n }(),
		func() runNetwork { n := network; n.ControlIPv4 = network.Gateway; return n }(),
	} {
		if _, err := altered.policyXML(); err == nil {
			t.Fatal("accepted malformed policy identity")
		}
		if (networkHostInventory{complete: true}).available(altered) {
			t.Fatal("accepted malformed host candidate")
		}
	}
	inventory := networkHostInventory{complete: true, hostIPv4: []netip.Addr{netip.MustParseAddr(network.ControlIPv4)}}
	if inventory.available(network) {
		t.Fatal("control endpoint allowed Worker host access")
	}
}

func TestControlPolicyStoredBeforeEffectsAndRecovered(t *testing.T) {
	store := newTestRunStore(t)
	control, _ := parseGuestControl("https://control.example.test:8443", "192.0.2.7")
	run, err := store.CreateControlled(storeTestWork, networkTestRun, testResources, "10.240.0.0/30", nil, control)
	if err != nil || run.Record.Schema != 4 || *run.Record.Network != controlNetworkFixture(t) {
		t.Fatal("control identity not persisted")
	}
	loaded, err := store.Load(networkTestRun)
	if err != nil || *loaded.Record.Network != *run.Record.Network {
		t.Fatal("control identity changed on load")
	}
	for _, schema := range []int{1, 2, 3, 5} {
		altered := run.Record
		altered.Schema = schema
		data, _ := json.Marshal(altered)
		if err := os.WriteFile(filepath.Join(store.root.Name(), networkTestRun, "run.json"), append(data, '\n'), 0600); err != nil {
			t.Fatal(err)
		}
		if _, err := store.Load(networkTestRun); err == nil {
			t.Fatal("cross-schema policy accepted")
		}
	}
}

func TestControlEndpointCannotPointIntoTaskAllocationPool(t *testing.T) {
	for _, address := range []string{"10.240.0.2", "10.240.128.2", "10.240.255.254"} {
		t.Run(address, func(t *testing.T) {
			store := newTestRunStore(t)
			control, _ := parseGuestControl("https://control.example.test", address)
			_, err := store.CreateControlled(storeTestWork, networkTestRun, testResources, "10.240.0.0/16", nil, control)
			runs, listErr := store.List()
			if err == nil || listErr != nil || len(runs) != 0 {
				t.Fatal("control exception authorized a present or future task VM")
			}
		})
	}
}

func TestControlPolicyCleanupRejectsBroadenedFilter(t *testing.T) {
	for _, broaden := range []bool{false, true} {
		f := controlCleanupFixture(t)
		if broaden {
			f.filterXML = []byte(strings.ReplaceAll(string(f.filterXML), "dstportend='8443'", "dstportend='65535'"))
		}
		err := f.cleanup.Cleanup(context.Background())
		if broaden && (err == nil || !f.defined || !f.filter) {
			t.Fatal("adopted or deleted altered filter")
		}
		if !broaden && (err != nil || f.defined || f.filter || f.bridge) {
			t.Fatal("recorded policy did not clean up", err)
		}
	}
}

func controlCleanupFixture(t *testing.T) *networkCleanupFixture {
	t.Helper()
	f := networkCleanupReservationFixture(t)
	network := controlNetworkFixture(t)
	f.run.Record.Schema, f.run.Record.Network = 4, &network
	data, _ := json.Marshal(f.run.Record)
	if err := os.WriteFile(filepath.Join(f.cleanup.store.root.Name(), networkTestRun, "run.json"), append(data, '\n'), 0600); err != nil {
		t.Fatal(err)
	}
	f.networkXML, _ = network.definitionXML()
	f.filterXML, _ = network.policyXML()
	p := networkProvisioner{store: f.cleanup.store}
	for name, body := range map[string][]byte{"network.xml": f.networkXML, "filter.xml": f.filterXML} {
		if err := p.writeDefinition(f.run.Record.RunID, name, body); err != nil {
			t.Fatal(err)
		}
	}
	return f
}

func TestControlPolicyRequiresExactPrivateCreationIntent(t *testing.T) {
	for _, scenario := range []string{"intact", "absent", "missing-network", "missing-filter", "v1-filter", "tampered-filter", "public", "symlink", "hardlink", "directory"} {
		t.Run(scenario, func(t *testing.T) {
			f := controlCleanupFixture(t)
			root := filepath.Join(f.cleanup.store.root.Name(), networkTestRun)
			path := filepath.Join(root, "filter.xml")
			switch scenario {
			case "absent", "missing-network":
				if err := os.Remove(filepath.Join(root, "network.xml")); err != nil {
					t.Fatal(err)
				}
			}
			switch scenario {
			case "absent", "missing-filter", "symlink", "directory":
				if err := os.Remove(path); err != nil {
					t.Fatal(err)
				}
				if scenario == "symlink" {
					if err := os.Symlink("network.xml", path); err != nil {
						t.Fatal(err)
					}
				} else if scenario == "directory" {
					if err := os.Mkdir(path, 0700); err != nil {
						t.Fatal(err)
					}
				}
			case "v1-filter", "tampered-filter":
				body := []byte(strings.Replace(string(f.filterXML), "priority='1000'", "priority='1001'", 1))
				if scenario == "v1-filter" {
					body, _ = f.run.Record.Network.quarantineXML()
				}
				if err := os.WriteFile(path, body, 0600); err != nil {
					t.Fatal(err)
				}
			case "public":
				if err := os.Chmod(path, 0644); err != nil {
					t.Fatal(err)
				}
			case "hardlink":
				if err := os.Link(path, filepath.Join(t.TempDir(), "linked.xml")); err != nil {
					t.Fatal(err)
				}
			}
			err := f.cleanup.Cleanup(context.Background())
			if scenario == "intact" {
				if err != nil || f.active || f.defined || f.filter || f.bridge {
					t.Fatal("exact V2 creation intent did not authorize its own cleanup", err)
				}
				if err := f.cleanup.Released(); err != nil {
					t.Fatal(err)
				}
				return
			}
			if err == nil || !f.active || !f.defined || !f.filter || !f.bridge ||
				slices.Contains(f.calls, "net-destroy") || slices.Contains(f.calls, "net-undefine") || slices.Contains(f.calls, "nwfilter-undefine") {
				t.Fatal("untrusted V2 creation intent authorized resource mutations")
			}
			if err := f.cleanup.Released(); err == nil {
				t.Fatal("unconfirmed V2 reservation was released")
			}
		})
	}
}
