package daemon

import (
	"context"
	"errors"
	"net"
	"net/netip"
	"slices"
	"strings"
	"testing"
)

func inventoryFixture(t *testing.T) networkInventoryReader {
	t.Helper()
	return networkInventoryReader{
		interfaces: func() ([]net.Interface, error) { return []net.Interface{{Name: "eth0"}}, nil },
		addresses: func(net.Interface) ([]net.Addr, error) {
			return []net.Addr{&net.IPNet{IP: net.ParseIP("192.168.5.15"), Mask: net.CIDRMask(24, 32)},
				&net.IPNet{IP: net.ParseIP("fe80::1"), Mask: net.CIDRMask(64, 128)}}, nil
		},
		routes: func(context.Context) ([]byte, error) {
			return []byte(`[{"dst":"default","gateway":"192.168.5.2"},{"dst":"10.241.0.0/24","table":99}]`), nil
		},
		query: func(_ context.Context, args ...string) ([]byte, error) {
			switch args[0] {
			case "net-list", "list":
				return []byte(storeTestWork + "\n"), nil
			case "net-dumpxml":
				address := "10.242.0.1"
				if slices.Contains(args, "--inactive") {
					address = "10.243.0.1"
				}
				return []byte("<network><name>foreign</name><uuid>" + storeTestWork + "</uuid><ip address='" + address + "' netmask='255.255.255.0'/></network>"), nil
			case "dumpxml":
				return []byte("<domain><name>foreign</name><uuid>" + storeTestWork + "</uuid><devices/></domain>"), nil
			default:
				t.Error("inventory attempted an unexpected/mutating command")
				return nil, errRunNetwork
			}
		},
	}
}

func TestRunNetworkHostInventoryIncludesInactiveAndHostRoutes(t *testing.T) {
	reader := inventoryFixture(t)
	inventory, err := reader.read(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	for _, prefix := range []string{"192.168.5.0/24", "192.168.5.2/32", "10.241.0.0/24", "10.242.0.0/24", "10.243.0.0/24"} {
		if !slices.Contains(inventory.excluded, netip.MustParsePrefix(prefix)) {
			t.Fatalf("missed host/current/inactive exclusion: %s", prefix)
		}
	}
	if len(inventory.excluded) != 5 || len(inventory.xml) != 4 {
		t.Fatal("wrong exclusion/XML inventory")
	}
	for _, pool := range []string{"192.168.5.0/30", "10.241.0.0/30", "10.242.0.0/30", "10.243.0.0/30"} {
		if _, err := allocateRunNetwork(networkTestRun, pool, nil, inventory.excluded); err == nil {
			t.Fatalf("allocated from occupied host prefix %s", pool)
		}
	}
	network, err := allocateRunNetwork(networkTestRun, "10.240.0.0/24", nil, inventory.excluded)
	if err != nil || !inventory.available(network) {
		t.Fatal("default route incorrectly consumed every address")
	}
}

func TestRunNetworkHostInventoryRejectsIdentityCollisions(t *testing.T) {
	network, _ := networkAt(networkTestRun, netip.MustParsePrefix("10.240.0.0/30"))
	if (networkHostInventory{}).available(network) {
		t.Fatal("treated an uncollected inventory as proof of availability")
	}
	for _, scenario := range []string{"bridge", "host-mac", "network-name", "network-uuid", "inactive-domain-mac", "inactive-domain-filter"} {
		t.Run(scenario, func(t *testing.T) {
			reader := inventoryFixture(t)
			query := reader.query
			if scenario == "bridge" || scenario == "host-mac" {
				reader.interfaces = func() ([]net.Interface, error) {
					device := net.Interface{Name: "eth0"}
					if scenario == "bridge" {
						device.Name = network.Bridge
					} else {
						device.HardwareAddr, _ = net.ParseMAC(network.GatewayMAC)
					}
					return []net.Interface{device}, nil
				}
			} else {
				reader.query = func(ctx context.Context, args ...string) ([]byte, error) {
					data, err := query(ctx, args...)
					body := string(data)
					if scenario == "network-name" && args[0] == "net-dumpxml" {
						body = strings.Replace(body, "<name>foreign</name>", "<name>"+network.Name+"</name>", 1)
					}
					if scenario == "network-uuid" {
						body = strings.ReplaceAll(body, storeTestWork, network.UUID)
					}
					if args[0] == "dumpxml" && slices.Contains(args, "--inactive") {
						if scenario == "inactive-domain-mac" {
							body = strings.Replace(body, "<devices/>", "<devices><interface><mac address='"+strings.ToUpper(network.GuestMAC)+"'/></interface></devices>", 1)
						}
						if scenario == "inactive-domain-filter" {
							body = strings.Replace(body, "<devices/>", "<devices><interface><filterref filter='"+network.Filter+"'/></interface></devices>", 1)
						}
					}
					return []byte(body), err
				}
			}
			inventory, err := reader.read(context.Background())
			if err != nil || inventory.available(network) {
				t.Fatal("missed a host or inactive-XML identity collision")
			}
		})
	}
}

func TestRunNetworkHostInventoryFailsWithoutCompleteSnapshot(t *testing.T) {
	for _, scenario := range []string{"interfaces", "addresses", "routes", "net-list", "list", "net-dumpxml", "dumpxml", "mismatched-uuid", "incomplete-persistent", "cancelled"} {
		t.Run(scenario, func(t *testing.T) {
			reader := inventoryFixture(t)
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			switch scenario {
			case "interfaces":
				reader.interfaces = func() ([]net.Interface, error) { return nil, errors.New("private host error") }
			case "addresses":
				reader.addresses = func(net.Interface) ([]net.Addr, error) { return nil, errors.New("private host error") }
			case "routes":
				reader.routes = func(context.Context) ([]byte, error) { return nil, errors.New("private host error") }
			case "cancelled":
				cancel()
			default:
				query := reader.query
				reader.query = func(ctx context.Context, args ...string) ([]byte, error) {
					if scenario == "incomplete-persistent" && args[0] == "net-list" && !slices.Contains(args, "--persistent") {
						return nil, nil
					}
					if scenario == "mismatched-uuid" && args[0] == "dumpxml" {
						return []byte("<domain><name>foreign</name><uuid>" + networkTestRun + "</uuid></domain>"), nil
					}
					if args[0] == scenario {
						return nil, errors.New("private host error")
					}
					return query(ctx, args...)
				}
			}
			inventory, err := reader.read(ctx)
			if !errors.Is(err, errRunNetwork) || len(inventory.excluded) != 0 || len(inventory.xml) != 0 {
				t.Fatal("returned a usable partial snapshot or exposed host error")
			}
		})
	}
}

func TestRunNetworkHostRouteParserRejectsAmbiguity(t *testing.T) {
	for _, body := range []string{"", `null`, `{}`, `[null]`, `[{}]`, `[{"dst":"default","dst":"10.0.0.0/8"}]`,
		`[{"dst":"10.0.0.1/24"}]`, `[{"dst":"::/0"}]`, `[{"dst":"default","gateway":null}]`,
		`[{"dst":"default","nhid":1}]`, `[{"dst":"default","via":{"family":"inet6","host":"::1"}}]`,
		`[{"dst":"default","nexthops":[]}]`, `[{"dst":"default","nexthops":[null]}]`,
		`[{"dst":"default","nexthops":[{"nexthops":[{}]}]}]`} {
		var inventory networkHostInventory
		if inventory.routeExclusions([]byte(body)) == nil {
			t.Fatalf("accepted ambiguous host routes: %s", body)
		}
	}
	var inventory networkHostInventory
	if err := inventory.routeExclusions([]byte(`[{"dst":"default","nexthops":[{"gateway":"10.1.2.3"},{"gateway":"10.2.3.4"}]},{"dst":"10.3.4.5"}]`)); err != nil || len(inventory.excluded) != 3 {
		t.Fatal("lost multipath default gateways or host route")
	}
}

func TestRunNetworkHostXMLAddressMasks(t *testing.T) {
	for _, address := range []string{
		"<ip address='10.0.0.1'/>", "<ip address='10.0.0.1' prefix='24' netmask='255.255.255.0'/>",
		"<ip address='10.0.0.1' netmask='255.0.255.0'/>", "<ip address='10.0.0.1' prefix='33'/>",
		"<ip family='ipv6' address='10.0.0.1' prefix='24'/>", "<ip address='::ffff:10.0.0.1' prefix='24'/>",
		"<route address='10.0.0.0' prefix='24'/>", "<route gateway='::1'/>",
	} {
		node, _ := parseNetworkXML([]byte("<network>"+address+"</network>"), "network")
		var inventory networkHostInventory
		if inventory.xmlExclusions(node) == nil {
			t.Fatalf("accepted ambiguous network address: %s", address)
		}
	}
	node, _ := parseNetworkXML([]byte("<network><ip address='10.1.0.1' prefix='24'/><ip family='ipv6' address='fd00::1' prefix='64'/><route gateway='10.2.0.1'/><route address='10.3.0.0' prefix='24' gateway='10.1.0.2'/></network>"), "network")
	var inventory networkHostInventory
	if inventory.xmlExclusions(node) != nil || len(inventory.excluded) != 4 {
		t.Fatal("lost explicit prefixes or default-route gateway")
	}
}
