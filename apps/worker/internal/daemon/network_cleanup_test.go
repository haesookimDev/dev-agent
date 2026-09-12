package daemon

import (
	"context"
	"errors"
	"net"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

type networkCleanupFixture struct {
	cleanup                                              *vmCleanup
	run                                                  ownedRun
	defined, active, filter, bridge                      bool
	refuse, foreignDomain, foreignFilter, binding, ports string
	networkXML, filterXML                                []byte
	calls                                                []string
}

func newNetworkCleanupFixture(t *testing.T) *networkCleanupFixture {
	t.Helper()
	f := networkCleanupReservationFixture(t)
	p := networkProvisioner{store: f.cleanup.store}
	for name, body := range map[string][]byte{"network.xml": f.networkXML, "filter.xml": f.filterXML} {
		if err := p.writeDefinition(f.run.Record.RunID, name, body); err != nil {
			t.Fatal(err)
		}
	}
	return f
}

func networkCleanupReservationFixture(t *testing.T) *networkCleanupFixture {
	t.Helper()
	store := newTestRunStore(t)
	run, err := store.CreateNetworked(storeTestWork, networkTestRun, testResources, "10.240.0.0/30", nil)
	if err != nil {
		t.Fatal(err)
	}
	f := &networkCleanupFixture{run: run, defined: true, active: true, filter: true, bridge: true}
	f.networkXML, _ = run.Record.Network.definitionXML()
	f.filterXML, _ = run.Record.Network.quarantineXML()
	f.cleanup = newVMCleanup(store, run.Record.RunID)
	f.cleanup.query = f.query
	f.cleanup.interfaces = func() ([]net.Interface, error) {
		if f.bridge {
			return []net.Interface{{Name: run.Record.Network.Bridge}}, nil
		}
		return nil, nil
	}
	if err := os.WriteFile(filepath.Join(store.root.Name(), networkTestRun, "user-data"), []byte("synthetic"), 0600); err != nil {
		t.Fatal(err)
	}
	return f
}

func TestRunNetworkCleanupRequiresPrivateCreationIntent(t *testing.T) {
	for _, scenario := range []string{"absent", "partial", "tampered", "public", "symlink", "hardlink", "directory"} {
		t.Run(scenario, func(t *testing.T) {
			f := newNetworkCleanupFixture(t)
			path := filepath.Join(f.cleanup.store.root.Name(), networkTestRun, "network.xml")
			switch scenario {
			case "absent", "partial", "symlink", "directory":
				if err := os.Remove(path); err != nil {
					t.Fatal(err)
				}
				if scenario == "absent" {
					if err := os.Remove(filepath.Join(filepath.Dir(path), "filter.xml")); err != nil {
						t.Fatal(err)
					}
				} else if scenario == "symlink" {
					if err := os.Symlink("filter.xml", path); err != nil {
						t.Fatal(err)
					}
				} else if scenario == "directory" {
					if err := os.Mkdir(path, 0700); err != nil {
						t.Fatal(err)
					}
				}
			case "tampered":
				body := strings.Replace(string(f.networkXML), "stp='off'", "stp='on' ", 1)
				if err := os.WriteFile(path, []byte(body), 0600); err != nil {
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
			if err := f.cleanup.Cleanup(context.Background()); err == nil {
				t.Fatal("accepted missing or untrusted creation intent")
			}
			if !f.active || !f.defined || !f.filter || !f.bridge || slices.Contains(f.calls, "net-destroy") || slices.Contains(f.calls, "net-undefine") || slices.Contains(f.calls, "nwfilter-undefine") {
				t.Fatal("mutated resources without private creation intent")
			}
			if err := f.cleanup.Released(); err == nil {
				t.Fatal("released an unconfirmed reservation")
			}
		})
	}
}

func (f *networkCleanupFixture) query(ctx context.Context, args ...string) ([]byte, error) {
	f.calls = append(f.calls, args[0])
	if ctx.Err() != nil || args[0] == f.refuse {
		return nil, errVMCleanup
	}
	switch args[0] {
	case "list":
		if f.foreignDomain != "" {
			return []byte(storeTestWork + "\n"), nil
		}
		return nil, nil
	case "dumpxml":
		return []byte(f.foreignDomain), nil
	case "net-list":
		if slices.Contains(args, "--autostart") || !f.defined {
			return nil, nil
		}
		if slices.Contains(args, "--inactive") && f.active && !slices.Contains(args, "--all") {
			return nil, nil
		}
		return []byte(networkTestRun + "\n"), nil
	case "net-dumpxml":
		return f.networkXML, nil
	case "nwfilter-list":
		body := " UUID Name\n----------\n"
		if f.filter {
			body += networkTestRun + " " + f.run.Record.Network.Filter + "\n"
		}
		if f.foreignFilter != "" {
			body += storeTestWork + " foreign-filter\n"
		}
		return []byte(body), nil
	case "nwfilter-dumpxml":
		if args[1] == storeTestWork {
			return []byte(f.foreignFilter), nil
		}
		return f.filterXML, nil
	case "nwfilter-binding-list":
		return []byte(" Port Dev Filter\n----------\n" + f.binding), nil
	case "net-port-list":
		return []byte(" UUID\n----------\n" + f.ports), nil
	case "net-destroy":
		f.active, f.bridge = false, false
	case "net-undefine":
		if f.active {
			return nil, errors.New("undefine is not network shutdown")
		}
		f.defined = false
	case "nwfilter-undefine":
		if f.defined || f.bridge {
			return nil, errors.New("filter removal preceded network cleanup")
		}
		f.filter = false
	default:
		return nil, errVMCleanup
	}
	if len(args) != 2 || args[1] != networkTestRun {
		return nil, errors.New("mutation did not target the exact owned UUID")
	}
	return nil, nil
}

func TestRunNetworkCleanupBeforeArtifactsAndRelease(t *testing.T) {
	for _, state := range []string{"active", "inactive", "filter-only", "absent"} {
		t.Run(state, func(t *testing.T) {
			f := newNetworkCleanupFixture(t)
			if state != "active" {
				f.active, f.bridge = false, false
			}
			if state == "filter-only" || state == "absent" {
				f.defined = false
			}
			if state == "absent" {
				f.filter = false
			}
			if err := f.cleanup.Cleanup(context.Background()); err != nil {
				t.Fatal(err)
			}
			run, err := f.cleanup.store.Load(networkTestRun)
			if err != nil || run.Phase != "cleaned" || f.defined || f.active || f.filter || f.bridge {
				t.Fatal("cleanup marker preceded network absence")
			}
			if _, err := os.Stat(filepath.Join(f.cleanup.store.root.Name(), networkTestRun, "user-data")); !os.IsNotExist(err) {
				t.Fatal("artifact was not removed after network cleanup")
			}
			if err := f.cleanup.Released(); err != nil {
				t.Fatal(err)
			}
			if err := f.cleanup.Cleanup(context.Background()); err != nil {
				t.Fatal("released absence recheck failed")
			}
		})
	}
}

func TestRunNetworkCleanupPreservesUncertainResources(t *testing.T) {
	for _, scenario := range []string{"network-owner", "network-extra", "connections", "filter-rule", "foreign-domain", "foreign-filter", "binding", "port", "bridge", "net-list", "net-destroy", "net-undefine", "nwfilter-list", "nwfilter-binding-list", "nwfilter-undefine"} {
		t.Run(scenario, func(t *testing.T) {
			f := newNetworkCleanupFixture(t)
			network := f.run.Record.Network
			switch scenario {
			case "network-owner":
				f.networkXML = []byte(strings.ReplaceAll(string(f.networkXML), "version='1'", "version='2'"))
			case "network-extra":
				f.networkXML = []byte(strings.ReplaceAll(string(f.networkXML), "</network>", "<route address='10.0.0.0' prefix='8' gateway='10.240.0.2'/></network>"))
			case "connections":
				f.networkXML = []byte(strings.ReplaceAll(string(f.networkXML), "<network ", "<network connections='1' "))
			case "filter-rule":
				f.filterXML = []byte(strings.ReplaceAll(string(f.filterXML), "action='drop'", "action='accept'"))
			case "foreign-domain":
				f.foreignDomain = "<domain><devices><interface><source network='" + network.Name + "'/></interface></devices></domain>"
			case "foreign-filter":
				f.foreignFilter = "<filter name='foreign-filter'><uuid>" + storeTestWork + "</uuid><filterref filter='" + network.Filter + "'/></filter>"
			case "binding":
				f.binding = "vnet99 " + network.Filter + "\n"
			case "port":
				f.ports = storeTestWork + "\n"
			case "bridge":
				f.defined, f.filter = false, false // orphan bridge is never deleted by name alone
			default:
				f.refuse = scenario
			}
			if err := f.cleanup.Cleanup(context.Background()); !errors.Is(err, errVMCleanup) {
				t.Fatalf("accepted uncertainty: %v", err)
			}
			run, err := f.cleanup.store.Load(networkTestRun)
			if err != nil || run.Phase != "cleanup-pending" {
				t.Fatal("uncertainty marked as cleaned")
			}
			if _, err := os.Stat(filepath.Join(f.cleanup.store.root.Name(), networkTestRun, "user-data")); err != nil {
				t.Fatal("deleted artifact before network proof")
			}
			if err := f.cleanup.Released(); err == nil {
				t.Fatal("released unclean network")
			}
		})
	}
}

func TestRunNetworkCleanupNeverDeletesReappearingResources(t *testing.T) {
	f := newNetworkCleanupFixture(t)
	if err := f.cleanup.Cleanup(context.Background()); err != nil {
		t.Fatal(err)
	}
	f.filter = true
	f.calls = nil
	if err := f.cleanup.Cleanup(context.Background()); err == nil || slices.Contains(f.calls, "nwfilter-undefine") {
		t.Fatal("deleted a filter that reappeared after durable cleanup")
	}
}

func TestRunNetworkCleanupChecksInactiveDomainReferences(t *testing.T) {
	f := newNetworkCleanupFixture(t)
	f.foreignDomain = "<domain><name>foreign</name><uuid>" + storeTestWork + "</uuid><devices/></domain>"
	f.cleanup.query = func(ctx context.Context, args ...string) ([]byte, error) {
		if args[0] == "dumpxml" && slices.Contains(args, "--inactive") {
			return []byte(strings.Replace(f.foreignDomain, "<devices/>", "<devices><interface><source bridge='"+f.run.Record.Network.Bridge+"'/></interface></devices>", 1)), nil
		}
		return f.query(ctx, args...)
	}
	if err := f.cleanup.Cleanup(context.Background()); err == nil || slices.Contains(f.calls, "net-destroy") {
		t.Fatal("ignored a foreign inactive configuration's bridge reference")
	}
}

func TestRunNetworkInventoryParsersFailClosed(t *testing.T) {
	for _, body := range []string{"", "UUID Name", "UUID Name\nnot-a-separator", "UUID Name\n-----\nuuid name extra", "UUID Name\n-----\nuuid name\nuuid again"} {
		if _, err := networkTable([]byte(body), "UUID Name", 2); err == nil {
			t.Fatal("accepted incomplete or malformed filter table")
		}
	}
	for _, body := range []string{"invalid", networkTestRun + "\n" + networkTestRun} {
		if _, err := networkUUIDInventory([]byte(body)); err == nil {
			t.Fatal("accepted malformed or duplicate network UUID")
		}
	}
}
