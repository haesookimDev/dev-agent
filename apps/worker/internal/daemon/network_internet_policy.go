package daemon

import (
	"fmt"
	"net/netip"
	"slices"
	"strings"
)

func (network runNetwork) internetPolicyXML() ([]byte, error) {
	control, err := parseGuestControl(network.ControlOrigin, network.ControlIPv4)
	if err != nil || !network.valid(network.UUID) || network.Version != 3 {
		return nil, errRunNetwork
	}
	// Reuse the exact anti-spoofing and gateway-only ARP rules from version 2.
	base := network
	base.Version, base.DNSIPv4, base.DeniedIPv4 = 2, "", ""
	data, err := base.policyXML()
	if err != nil {
		return nil, err
	}
	const boundary = "  <rule action='accept' direction='out' priority='-800'>"
	head, _, found := strings.Cut(string(data), boundary)
	if !found {
		return nil, errRunNetwork
	}
	var result strings.Builder
	result.WriteString(head)
	// L2 accepts only the exact API exception before destination/source denies.
	fmt.Fprintf(&result, "  <rule action='accept' direction='out' priority='-850'><ip srcipaddr='%s' dstipaddr='%s' dstmacaddr='%s' protocol='tcp' dstportstart='%d' dstportend='%d'/></rule>\n", network.Guest, control.IPv4, network.GatewayMAC, control.Port, control.Port)
	fmt.Fprintf(&result, "  <rule action='accept' direction='in' priority='-850'><ip srcipaddr='%s' dstipaddr='%s' dstmacaddr='%s' protocol='tcp' srcportstart='%d' srcportend='%d'/></rule>\n", control.IPv4, network.Guest, network.GuestMAC, control.Port, control.Port)
	for _, value := range append(slices.Clone(internetReservedIPv4), strings.Split(network.DeniedIPv4, ",")...) {
		prefix := netip.MustParsePrefix(value)
		fmt.Fprintf(&result, "  <rule action='drop' direction='out' priority='-840'><ip dstipaddr='%s' dstipmask='%d'/></rule>\n", prefix.Addr(), prefix.Bits())
		fmt.Fprintf(&result, "  <rule action='drop' direction='in' priority='-840'><ip srcipaddr='%s' srcipmask='%d'/></rule>\n", prefix.Addr(), prefix.Bits())
	}
	for _, protocol := range []string{"tcp", "udp", "icmp"} {
		fmt.Fprintf(&result, "  <rule action='accept' direction='out' priority='-800'><ip srcipaddr='%s' dstmacaddr='%s' protocol='%s'/></rule>\n", network.Guest, network.GatewayMAC, protocol)
		fmt.Fprintf(&result, "  <rule action='accept' direction='in' priority='-800'><ip dstipaddr='%s' dstmacaddr='%s' protocol='%s'/></rule>\n", network.Guest, network.GuestMAC, protocol)
	}
	result.WriteString("  <rule action='drop' direction='inout' priority='-700'><mac/></rule>\n")
	// Directional stateful L3 rules admit VM-initiated connections and their
	// replies, not unsolicited inbound connections. L2 deny rules run first.
	for _, protocol := range []string{"tcp", "udp", "icmp"} {
		fmt.Fprintf(&result, "  <rule action='accept' direction='out' priority='0'><%s srcipaddr='%s'/></rule>\n", protocol, network.Guest)
	}
	result.WriteString("  <rule action='drop' direction='inout' priority='1000' statematch='false'><all/></rule>\n</filter>\n")
	return []byte(result.String()), nil
}
