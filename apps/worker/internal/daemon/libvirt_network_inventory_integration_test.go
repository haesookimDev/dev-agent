//go:build linux && libvirt_integration

package daemon

import (
	"context"
	"net/netip"
	"os"
	"slices"
	"testing"
)

// Read-only actual-host inspection. No default network or VM is activated.
func TestDedicatedLibvirtHostNetworkInventory(t *testing.T) {
	if os.Getenv("KELPIE_LIBVIRT_TEST_ACK") != "disposable-host-only" {
		t.Skip("requires explicit disposable-host acknowledgement")
	}
	if os.Geteuid() == 0 {
		t.Fatal("run as the unprivileged Worker user")
	}
	reader := hostNetworkReader()
	inventory, err := reader.read(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	for _, device := range inventory.interfaces {
		addresses, err := device.Addrs()
		if err != nil {
			t.Fatal("could not recheck host addresses")
		}
		for _, address := range addresses {
			prefix, err := netip.ParsePrefix(address.String())
			if err != nil || prefix.Addr().Is4() && !slices.Contains(inventory.excluded, prefix.Masked()) {
				t.Fatal("host address was not excluded from allocation")
			}
		}
	}
	network, err := allocateRunNetwork(networkTestRun, "10.240.0.0/24", nil, inventory.excluded)
	if err != nil || !inventory.available(network) {
		t.Fatal("dedicated lab pool is unavailable; no network was created")
	}
	t.Logf("read-only host inventory: interfaces=%d, excluded IPv4 prefixes=%d, live/inactive XML views=%d; no resources created", len(inventory.interfaces), len(inventory.excluded), len(inventory.xml))
}
