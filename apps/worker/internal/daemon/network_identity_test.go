package daemon

import (
	"errors"
	"fmt"
	"net"
	"net/netip"
	"reflect"
	"slices"
	"strings"
	"testing"
)

const networkTestRun = "dd2031f8-1725-49d1-8b06-1422de3485b4"

func TestRunNetworkIdentityIsCanonical(t *testing.T) {
	first, err := allocateRunNetwork(networkTestRun, "10.240.0.0/24", nil, nil)
	if err != nil || !first.valid(networkTestRun) {
		t.Fatalf("invalid allocation: %v", err)
	}
	second, err := allocateRunNetwork(networkTestRun, "10.240.0.0/24", nil, nil)
	if err != nil || first != second {
		t.Fatal("allocation is not deterministic")
	}
	if len(first.Bridge) > 15 || !strings.HasPrefix(first.Bridge, "kp") ||
		first.Name != "kelpie-net-"+networkTestRun || first.Filter != "kelpie-filter-"+networkTestRun {
		t.Fatal("unsafe device identity")
	}
	subnet := netip.MustParsePrefix(first.CIDR)
	if subnet.Bits() != 30 || !netip.MustParsePrefix("10.240.0.0/24").Contains(subnet.Addr()) ||
		first.Gateway != subnet.Addr().Next().String() || first.Guest != subnet.Addr().Next().Next().String() {
		t.Fatal("invalid /30 address layout")
	}
	for _, address := range []string{first.GuestMAC, first.GatewayMAC} {
		mac, err := net.ParseMAC(address)
		if err != nil || len(mac) != 6 || mac[0]&3 != 2 || mac.String() != address {
			t.Fatal("MAC is not canonical locally administered unicast")
		}
	}
	if first.GuestMAC == first.GatewayMAC {
		t.Fatal("guest and gateway share MAC")
	}
	// Every field is part of the canonical identity, including policy version.
	for index := 0; index < reflect.TypeOf(first).NumField(); index++ {
		changed := first
		field := reflect.ValueOf(&changed).Elem().Field(index)
		if field.Kind() == reflect.Int {
			field.SetInt(2)
		} else {
			field.SetString(field.String() + "-changed")
		}
		if changed.valid(networkTestRun) {
			t.Fatalf("accepted modified field %s", reflect.TypeOf(first).Field(index).Name)
		}
	}
	if first.valid("aa2031f8-1725-49d1-8b06-1422de3485b4") {
		t.Fatal("accepted another run's identity")
	}
}

func TestRunNetworkPoolRejectsAmbiguousOrUnsafeInput(t *testing.T) {
	for _, pool := range []string{
		"", "10.0.0.1/24", "10.0.0.0/15", "10.0.0.0/31", "10.0.0.0/32", "10.0.0.0/024",
		"10.0.0.0/24\n", " 10.0.0.0/24", "10.00.0.0/24", "172.15.0.0/16", "172.32.0.0/16",
		"192.169.0.0/16", "169.254.0.0/16", "100.64.0.0/16", "127.0.0.0/16", "0.0.0.0/16",
		"203.0.113.0/24", "8.8.8.0/24", "224.0.0.0/16", "240.0.0.0/16", "::ffff:10.0.0.0/120",
		"fd00::/64", "::/0", "10.0.0.0/24;touch /tmp/marker",
	} {
		t.Run(pool, func(t *testing.T) {
			if _, err := allocateRunNetwork(networkTestRun, pool, nil, nil); !errors.Is(err, errRunNetwork) {
				t.Fatalf("accepted invalid pool: %v", err)
			}
		})
	}
	for _, pool := range []string{"10.0.0.0/16", "10.255.255.252/30", "172.16.0.0/16", "172.31.255.252/30", "192.168.0.0/16", "192.168.255.252/30"} {
		if _, err := allocateRunNetwork(networkTestRun, pool, nil, nil); err != nil {
			t.Fatalf("rejected valid pool %s", pool)
		}
	}
	for _, runID := range []string{"", "../../other", strings.ToUpper(networkTestRun), "dd2031f8-1725-59d1-8b06-1422de3485b4"} {
		if _, err := allocateRunNetwork(runID, "10.240.0.0/24", nil, nil); !errors.Is(err, errRunNetwork) {
			t.Fatal("accepted unsafe run identity")
		}
	}
}

func TestRunNetworkAllocationExcludesOwnedAndHostSubnets(t *testing.T) {
	var occupied []runNetwork
	// /27 has eight /30s; a host /28 consumes four, a /32 inside a fifth
	// consumes that whole /30. Leave exactly three distinct allocations.
	excluded := []netip.Prefix{netip.MustParsePrefix("10.240.0.0/28"), netip.MustParsePrefix("10.240.0.18/32")}
	for index := 0; index < 3; index++ {
		runID := fmt.Sprintf("dd2031f8-1725-49d1-8b06-%012x", index)
		network, err := allocateRunNetwork(runID, "10.240.0.0/27", occupied, excluded)
		if err != nil {
			t.Fatal(err)
		}
		subnet := netip.MustParsePrefix(network.CIDR)
		for _, reserved := range excluded {
			if reserved.Overlaps(subnet) {
				t.Fatal("overlaps host subnet")
			}
		}
		for _, prior := range occupied {
			if netip.MustParsePrefix(prior.CIDR).Overlaps(subnet) || network.Bridge == prior.Bridge ||
				network.GuestMAC == prior.GuestMAC || network.GatewayMAC == prior.GatewayMAC {
				t.Fatal("collides with another run")
			}
		}
		occupied = append(occupied, network)
	}
	if _, err := allocateRunNetwork(networkTestRun, "10.240.0.0/27", occupied, excluded); !errors.Is(err, errRunNetwork) {
		t.Fatal("exhaustion fell back to an occupied address")
	}
	// Inventory order is irrelevant and inputs are never mutated.
	before := slices.Clone(occupied)
	first, err := allocateRunNetwork(networkTestRun, "10.240.0.0/24", occupied, excluded)
	if err != nil || !slices.Equal(occupied, before) {
		t.Fatal("failed or mutated inventory")
	}
	slices.Reverse(occupied)
	slices.Reverse(excluded)
	second, err := allocateRunNetwork(networkTestRun, "10.240.0.0/24", occupied, excluded)
	if err != nil || first != second {
		t.Fatal("inventory order changed proposal")
	}
}

func TestRunNetworkAllocationRejectsUntrustedInventory(t *testing.T) {
	first, err := allocateRunNetwork(networkTestRun, "10.240.0.0/24", nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	other := "aa2031f8-1725-49d1-8b06-1422de3485b4"
	overlapping, _ := networkAt(other, netip.MustParsePrefix(first.CIDR))
	corrupted := first
	corrupted.Bridge = "eth0"
	for _, inventory := range [][]runNetwork{{first}, {first, first}, {first, overlapping}, {corrupted}, make([]runNetwork, 4097)} {
		if _, err := allocateRunNetwork(networkTestRun, "10.240.0.0/24", inventory, nil); !errors.Is(err, errRunNetwork) {
			t.Fatal("adopted existing identity or accepted inconsistent inventory")
		}
	}
	third := "bb2031f8-1725-49d1-8b06-1422de3485b4"
	if _, err := allocateRunNetwork(third, "10.240.0.0/24", []runNetwork{first, overlapping}, nil); !errors.Is(err, errRunNetwork) {
		t.Fatal("accepted conflicting subnets for two otherwise valid runs")
	}
	for _, inventory := range [][]netip.Prefix{
		{{}}, {netip.MustParsePrefix("::/0")}, {netip.MustParsePrefix("10.0.0.1/24")},
		{netip.MustParsePrefix("0.0.0.0/0")}, make([]netip.Prefix, 4097),
	} {
		if _, err := allocateRunNetwork(other, "10.240.0.0/24", nil, inventory); !errors.Is(err, errRunNetwork) {
			t.Fatal("accepted invalid or fully excluded inventory")
		}
	}
}

func TestRunNetworkIdentityRejectsInvalidSubnet(t *testing.T) {
	for _, subnet := range []netip.Prefix{{}, netip.MustParsePrefix("10.0.0.1/30"), netip.MustParsePrefix("10.0.0.0/24"), netip.MustParsePrefix("8.8.8.0/30"), netip.MustParsePrefix("::ffff:10.0.0.0/126")} {
		if _, err := networkAt(networkTestRun, subnet); !errors.Is(err, errRunNetwork) {
			t.Fatal("accepted unsafe subnet")
		}
	}
}
