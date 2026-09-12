package daemon

import (
	"net/netip"
	"slices"
	"strings"
)

// These non-public/special-use ranges stay denied even when Go considers an
// address global-unicast. Conservatively deny the whole protocol-assignment
// and deprecated relay blocks, including their special-purpose exceptions.
// Reviewed against the IANA IPv4 Special-Purpose Address Registry 2026-09-13:
// https://www.iana.org/assignments/iana-ipv4-special-registry/
var internetReservedIPv4 = []string{
	"0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
	"169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24",
	"192.88.99.0/24", "192.168.0.0/16", "198.18.0.0/15",
	"198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4",
}

type guestInternet struct {
	DNSIPv4    string
	DeniedIPv4 string // sorted canonical comma-separated prefixes; bounded to 32
}

func publicGuestIPv4(text string) bool {
	ip, err := netip.ParseAddr(text)
	if err != nil || !ip.Is4() || ip.String() != text || !ip.IsGlobalUnicast() {
		return false
	}
	for _, value := range internetReservedIPv4 {
		if netip.MustParsePrefix(value).Contains(ip) {
			return false
		}
	}
	return true
}

func parseGuestInternet(dns, denied string) (guestInternet, error) {
	if !publicGuestIPv4(dns) || len(denied) == 0 || len(denied) > 1024 {
		return guestInternet{}, errRunNetwork
	}
	values := strings.Split(denied, ",")
	if len(values) > 32 || !slices.IsSorted(values) {
		return guestInternet{}, errRunNetwork
	}
	for index, value := range values {
		prefix, err := netip.ParsePrefix(value)
		if err != nil || !prefix.Addr().Is4() || prefix.Bits() == 0 || prefix != prefix.Masked() || prefix.String() != value ||
			(index > 0 && value == values[index-1]) || prefix.Contains(netip.MustParseAddr(dns)) {
			return guestInternet{}, errRunNetwork
		}
	}
	return guestInternet{DNSIPv4: dns, DeniedIPv4: denied}, nil
}

// Add every current Host address, including public addresses, and the control
// /32. Recheck fresh inventory before activation; changed Host addressing is
// not silently admitted. Administrators also supply management-plane CIDRs.
func (internet guestInternet) withHost(inventory networkHostInventory, control guestControl) (guestInternet, error) {
	verified, err := parseGuestInternet(internet.DNSIPv4, internet.DeniedIPv4)
	verifiedControl, controlErr := parseGuestControl(control.Origin, control.IPv4)
	if err != nil || verified != internet || controlErr != nil || verifiedControl != control ||
		!inventory.complete || len(inventory.hostIPv4) == 0 || len(inventory.hostIPv4) > 32 {
		return guestInternet{}, errRunNetwork
	}
	values := strings.Split(internet.DeniedIPv4, ",")
	values = append(values, control.IPv4+"/32")
	for _, ip := range inventory.hostIPv4 {
		if !ip.Is4() || ip.String() == control.IPv4 {
			return guestInternet{}, errRunNetwork
		}
		values = append(values, netip.PrefixFrom(ip, 32).String())
	}
	slices.Sort(values)
	values = slices.Compact(values)
	return parseGuestInternet(internet.DNSIPv4, strings.Join(values, ","))
}
