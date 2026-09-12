//go:build linux && libvirt_integration

package daemon

import (
	"context"
	"encoding/json"
	"net/netip"
	"os"
	"strconv"
	"strings"
	"testing"
	"time"
)

// Invoke as root through unshare --net on the disposable lab. Never install a
// rule in PID 1's network namespace. No guest/host traffic is enabled here.
func TestIsolatedKernelNetworkLog(t *testing.T) {
	requireIsolatedLogNamespace(t)
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

func requireIsolatedLogNamespace(t *testing.T) {
	t.Helper()
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
}

func TestIsolatedKernelNetworkLogHookFailure(t *testing.T) {
	requireIsolatedLogNamespace(t)
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	f := newLogHookFixture(t)
	f.hook.query = queryNetworkLog
	if err := f.apply("start", "begin"); err != nil {
		t.Fatal(err)
	}
	if err := f.apply("port-created", "begin"); err != nil {
		t.Fatal(err)
	}
	record, err := f.hook.active(ctx, f.network)
	if err != nil || deleteNetworkLog(ctx, queryNetworkLog, record.Handle) != nil {
		t.Fatal("could not remove the exact owned test logger")
	}
	if err := f.apply("port-created", "begin"); err == nil {
		t.Fatal("missing kernel logger allowed attachment")
	}
	// Recreate identical rules under a new kernel handle. Neither attachment
	// nor cleanup may adopt them. This namespace disappears at process exit.
	rules, _ := networkLogRules(f.network)
	if _, err := queryNetworkLog(ctx, rules, "--file", "-"); err != nil {
		t.Fatal(err)
	}
	if err := f.apply("port-created", "begin"); err == nil {
		t.Fatal("replacement logger allowed attachment")
	}
	if err := f.apply("stopped", "end"); err == nil {
		t.Fatal("cleanup adopted a replacement kernel table")
	}
	if present, err := networkLogPresent(ctx, queryNetworkLog, f.network); err != nil || !present {
		t.Fatal("replacement table was deleted")
	}
	t.Log("actual kernel logger disappearance/replacement refused attachment and foreign-handle deletion; journal health injected only in this namespace test")
}

func TestDedicatedLibvirtLoggedControlPackets(t *testing.T) {
	if os.Getenv("KELPIE_LIBVIRT_TEST_ACK") != "disposable-host-only" || os.Getenv("KELPIE_LIBVIRT_CONTROL_ORIGIN") == "" {
		t.Skip("requires disposable host with installed root logging hook and an explicit HTTPS endpoint")
	}
	control, err := parseGuestControl(os.Getenv("KELPIE_LIBVIRT_CONTROL_ORIGIN"), os.Getenv("KELPIE_LIBVIRT_CONTROL_IPV4"))
	if err != nil {
		t.Fatal(err)
	}
	dedicatedNetworkPackets(t, &control, true, nil)
}

func packetVerifyNetworkLog(t *testing.T, ctx context.Context, network runNetwork, control *guestControl, cloneIPv4 string, stopped bool) {
	t.Helper()
	dnsIPv4 := network.DNSIPv4
	// The root hook owns only the immutable network definition. Reconstructing
	// it avoids treating control or public-egress policy fields as receipt data.
	network, err := networkAt(network.UUID, netip.MustParsePrefix(network.CIDR))
	if err != nil {
		t.Fatal(err)
	}
	root, err := os.OpenRoot(networkLogDirectory)
	if err != nil {
		t.Fatal("root logging receipt directory is unavailable")
	}
	defer root.Close()
	hook := networkLogHook{root: root, owner: 0}
	phase := "active"
	if stopped {
		phase = "stopped"
	}
	record, exists, err := hook.read(network, phase)
	if err != nil || !exists {
		t.Fatal("exact root-owned logging receipt missing")
	}
	query := func(ctx context.Context, input []byte, args ...string) ([]byte, error) {
		if len(input) != 0 {
			t.Fatal("packet observation attempted a firewall mutation")
		}
		return packetHostQuery(t, ctx, "/usr/bin/sudo", append([]string{"-n", "/usr/sbin/nft"}, args...)...), nil
	}
	if stopped {
		if present, err := networkLogPresent(ctx, query, network); err != nil || present {
			t.Fatal("hook did not remove its logging table")
		}
		t.Log("actual libvirt stopped hook confirmed root receipt and physical logging-table removal")
		return
	}
	fingerprint, handle, err := networkLogSnapshot(ctx, query, network)
	if err != nil || fingerprint != record.Fingerprint || handle != record.Handle {
		t.Fatal("live kernel logging differs from the root receipt")
	}
	packetHostQuery(t, ctx, "/usr/bin/sudo", "-n", "/usr/bin/journalctl", "--sync")
	data := packetHostQuery(t, ctx, "/usr/bin/sudo", "-n", "/usr/bin/journalctl", "--dmesg", "--no-pager", "--output=json", "--lines=50", "--grep=^kelpie-egress:"+network.UUID+" ")
	controlMatched, dnsMatched, publicHTTPSMatched := false, false, false
	for _, line := range strings.Split(strings.TrimSpace(string(data)), "\n") {
		var row struct {
			Message string `json:"MESSAGE"`
		}
		if json.Unmarshal([]byte(line), &row) != nil || !strings.HasPrefix(row.Message, "kelpie-egress:"+network.UUID+" ") {
			t.Fatal("kernel journal response is not scoped to this test run")
		}
		if packetFlowMatches(row.Message, network, control.IPv4, "TCP", strconv.Itoa(int(control.Port))) {
			controlMatched = true
		}
		if cloneIPv4 != "" && packetFlowMatches(row.Message, network, dnsIPv4, "UDP", "53") {
			dnsMatched = true
		}
		if cloneIPv4 != "" && packetFlowMatches(row.Message, network, cloneIPv4, "TCP", "443") {
			publicHTTPSMatched = true
		}
	}
	if !controlMatched {
		t.Fatal("successful guest TLS connection has no matching kernel flow record")
	}
	if cloneIPv4 != "" && (!dnsMatched || !publicHTTPSMatched) {
		t.Fatal("public DNS or shallow-clone HTTPS flow has no matching kernel record")
	}
	if cloneIPv4 != "" {
		t.Log("actual control TLS, public DNS UDP/53, and public clone TCP/443 flows recorded with exact IP/protocol/port journal fields; no payload published")
	} else {
		t.Log("actual guest TLS flow recorded in the kernel journal with exact run UUID, bridge, source, destination, protocol and port; no payload published")
	}
}
