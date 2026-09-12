//go:build linux && libvirt_integration

package daemon

import (
	"context"
	"os"
	"testing"
	"time"
)

// Invoke as root through unshare --net on the disposable lab. Never install a
// rule in PID 1's network namespace. No guest/host traffic is enabled here.
func TestIsolatedKernelNetworkLog(t *testing.T) {
	if os.Getenv("KELPIE_NETWORK_LOG_TEST_ACK") != "disposable-isolated-netns-only" {
		t.Skip("requires an explicitly acknowledged disposable network namespace")
	}
	self, err := os.Stat("/proc/self/ns/net")
	if err != nil {
		t.Fatal(err)
	}
	init, err := os.Stat("/proc/1/ns/net")
	if err != nil || os.Geteuid() != 0 || os.SameFile(self, init) {
		t.Fatal("requires root in a new network namespace, never the host namespace")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	network := logTestNetwork(t)
	present, err := networkLogPresent(ctx, queryNetworkLog, network)
	if err != nil || present {
		t.Fatal("cannot prove test table absence")
	}
	rules, err := networkLogRules(network)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := queryNetworkLog(ctx, rules, "--file", "-"); err != nil {
		t.Fatal("kernel rejected generated logging policy")
	}
	first, handle, err := networkLogSnapshot(ctx, queryNetworkLog, network)
	if err != nil {
		t.Fatal("cannot read created logging policy")
	}
	if _, err := queryNetworkLog(ctx, rules, "--file", "-"); err == nil {
		t.Fatal("existing table was adopted")
	}
	again, sameHandle, err := networkLogSnapshot(ctx, queryNetworkLog, network)
	if err != nil || again != first || sameHandle != handle {
		t.Fatal("unchanged table identity changed")
	}
	if err := deleteNetworkLog(ctx, queryNetworkLog, handle); err != nil {
		t.Fatal("handle-scoped deletion failed")
	}
	if present, err := networkLogPresent(ctx, queryNetworkLog, network); err != nil || present {
		t.Fatal("deleted table still present or inventory unavailable")
	}
	t.Log("actual kernel accepted scoped logging rules, rejected adoption, preserved identity and confirmed handle-scoped removal; no guest-flow claim")
}
