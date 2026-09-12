package daemon

import (
	"strings"
	"testing"
)

// The opt-in VM fixture must match the actual clone destination, never any
// public HTTPS flow (in particular its independent control TLS connection).
func packetFlowMatches(message string, network runNetwork, destination, protocol, port string) bool {
	if !strings.HasPrefix(message, "kelpie-egress:"+network.UUID+" ") {
		return false
	}
	want := map[string]string{"IN": network.Bridge, "SRC": network.Guest, "DST": destination, "PROTO": protocol, "DPT": port}
	seen := map[string]bool{}
	for _, field := range strings.Fields(message) {
		key, value, found := strings.Cut(field, "=")
		if expected, required := want[key]; found && required {
			if seen[key] || value != expected {
				return false
			}
			seen[key] = true
		}
	}
	return len(seen) == len(want)
}

func TestPacketFlowEvidenceRequiresExactDestination(t *testing.T) {
	network := controlNetworkFixture(t)
	message := "kelpie-egress:" + network.UUID + " IN=" + network.Bridge + " OUT=eth0 SRC=" + network.Guest + " DST=8.8.8.8 PROTO=TCP DPT=443 LEN=40"
	if !packetFlowMatches(message, network, "8.8.8.8", "TCP", "443") {
		t.Fatal("exact packet flow rejected")
	}
	for _, invalid := range []string{
		strings.Replace(message, "DST=8.8.8.8", "DST=1.1.1.1", 1),
		strings.Replace(message, network.UUID, "other-run", 1),
		strings.Replace(message, "IN="+network.Bridge, "IN=other-bridge", 1),
		strings.Replace(message, "SRC="+network.Guest, "SRC=10.240.1.2", 1),
		strings.Replace(message, "PROTO=TCP", "PROTO=UDP", 1),
		strings.Replace(message, "DPT=443", "DPT=4430", 1),
		strings.Replace(message, " DPT=443", "", 1),
		message + " DST=8.8.8.8",
	} {
		if packetFlowMatches(invalid, network, "8.8.8.8", "TCP", "443") {
			t.Fatal("unrelated or ambiguous flow became clone evidence")
		}
	}
}
