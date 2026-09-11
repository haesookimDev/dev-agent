package daemon

import (
	"context"
	"net"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"testing"
)

func provisionFixture(t *testing.T) (networkProvisioner, *networkCleanupFixture) {
	t.Helper()
	f := newNetworkCleanupFixture(t)
	f.defined, f.active, f.filter, f.bridge = false, false, false, false
	network := f.run.Record.Network
	reader := inventoryFixture(t)
	reader.interfaces = func() ([]net.Interface, error) {
		devices := []net.Interface{{Name: "eth0"}}
		if f.bridge {
			mac, _ := net.ParseMAC(network.GatewayMAC)
			devices = append(devices, net.Interface{Name: network.Bridge, HardwareAddr: mac})
		}
		return devices, nil
	}
	reader.addresses = func(device net.Interface) ([]net.Addr, error) {
		address, bits := "192.168.5.15", 24
		if device.Name == network.Bridge {
			address, bits = network.Gateway, 30
		}
		return []net.Addr{&net.IPNet{IP: net.ParseIP(address), Mask: net.CIDRMask(bits, 32)}}, nil
	}
	reader.query = func(ctx context.Context, args ...string) ([]byte, error) {
		if !slices.Contains([]string{"nwfilter-define", "net-define", "net-start"}, args[0]) {
			return f.query(ctx, args...)
		}
		f.calls = append(f.calls, args[0])
		if ctx.Err() != nil || f.refuse == args[0] {
			return nil, errRunNetwork
		}
		stored, err := f.cleanup.store.Load(network.UUID)
		if err != nil || stored.Phase != "prepared" || !reflect.DeepEqual(stored.Record, f.run.Record) {
			t.Fatal("host mutation preceded canonical durable ownership")
		}
		if args[0] == "net-start" {
			if len(args) != 2 || args[1] != network.UUID || !f.defined || !f.filter {
				t.Fatal("activated before owned definition and quarantine")
			}
			f.active, f.bridge = true, true
			return nil, nil
		}
		name, expected := "filter.xml", f.filterXML
		if args[0] == "net-define" {
			name, expected = "network.xml", f.networkXML
			if !f.filter {
				t.Fatal("defined network before quarantine")
			}
		}
		path := filepath.Join(f.cleanup.store.root.Name(), network.UUID, name)
		if len(args) != 3 || args[1] != path || args[2] != "--validate" {
			t.Fatal("definition did not use the exact owned XML")
		}
		info, err := os.Lstat(path)
		if err != nil || !privateOwned(info, false) {
			t.Fatal("definition was not a private Worker-owned regular file")
		}
		data, err := os.ReadFile(path)
		if err != nil || string(data) != string(expected) {
			t.Fatal("persisted XML differed from canonical network identity")
		}
		if args[0] == "nwfilter-define" {
			f.filter = true
		} else {
			f.defined = true
		}
		return nil, nil
	}
	return networkProvisioner{store: f.cleanup.store, reader: reader}, f
}

func TestRunNetworkProvisionOwnsQuarantineBeforeActivation(t *testing.T) {
	p, f := provisionFixture(t)
	if err := p.create(context.Background(), f.run.Record.RunID); err != nil {
		t.Fatal(err)
	}
	if !f.defined || !f.active || !f.filter || !f.bridge {
		t.Fatal("network was not activated")
	}
	if err := p.create(context.Background(), f.run.Record.RunID); err == nil {
		t.Fatal("silently adopted or redefined a previous network")
	}
	if err := f.cleanup.Cleanup(context.Background()); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"network.xml", "filter.xml"} {
		if _, err := os.Stat(filepath.Join(p.store.root.Name(), f.run.Record.RunID, name)); !os.IsNotExist(err) {
			t.Fatal("owned XML not removed after physical network cleanup")
		}
	}
}

func TestRunNetworkProvisionLeavesPartialResourcesRecoverable(t *testing.T) {
	for _, command := range []string{"nwfilter-define", "net-define", "net-dumpxml", "net-start"} {
		t.Run(command, func(t *testing.T) {
			p, f := provisionFixture(t)
			f.refuse = command
			if err := p.create(context.Background(), f.run.Record.RunID); err == nil {
				t.Fatal("ignored failed provisioning step")
			}
			run, err := p.store.Load(f.run.Record.RunID)
			if err != nil || run.Phase != "prepared" {
				t.Fatal("lost durable recovery identity or prematurely marked running")
			}
			f.refuse = ""
			if err := f.cleanup.Cleanup(context.Background()); err != nil {
				t.Fatal(err)
			}
			if f.defined || f.active || f.filter || f.bridge {
				t.Fatal("partial provisioning leaked resources")
			}
		})
	}
}

func TestRunNetworkProvisionNeverOverwritesExistingResources(t *testing.T) {
	for _, scenario := range []string{"filter", "network", "definition", "stale-snapshot", "cleanup-started", "invalid-run", "cancelled"} {
		t.Run(scenario, func(t *testing.T) {
			p, f := provisionFixture(t)
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			id := f.run.Record.RunID
			switch scenario {
			case "filter":
				f.filter = true
			case "network":
				f.defined = true
			case "definition":
				path := filepath.Join(p.store.root.Name(), id, "filter.xml")
				if err := os.WriteFile(path, []byte("preserve existing file"), 0600); err != nil {
					t.Fatal(err)
				}
			case "stale-snapshot":
				query := p.reader.query
				p.reader.query = func(ctx context.Context, args ...string) ([]byte, error) {
					data, err := query(ctx, args...)
					if args[0] == "nwfilter-define" && err == nil {
						f.foreignDomain = "<domain><name>foreign</name><uuid>" + storeTestWork + "</uuid><devices><interface><source bridge='" + f.run.Record.Network.Bridge + "'/></interface></devices></domain>"
					}
					return data, err
				}
			case "cleanup-started":
				if _, err := p.store.Advance(id, "cleanup-pending"); err != nil {
					t.Fatal(err)
				}
			case "invalid-run":
				id = "../other"
			case "cancelled":
				cancel()
			}
			if err := p.create(ctx, id); err == nil {
				t.Fatal("accepted conflicting or unowned provisioning state")
			}
			if slices.Contains(f.calls, "net-define") || slices.Contains(f.calls, "net-start") || scenario != "stale-snapshot" && slices.Contains(f.calls, "nwfilter-define") {
				t.Fatal("mutated resources after preflight failure")
			}
			if scenario == "definition" {
				body, _ := os.ReadFile(filepath.Join(p.store.root.Name(), id, "filter.xml"))
				if string(body) != "preserve existing file" {
					t.Fatal("overwrote an existing definition")
				}
			}
		})
	}
}
