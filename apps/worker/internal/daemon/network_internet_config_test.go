package daemon

import (
	"fmt"
	"net/netip"
	"slices"
	"strings"
	"testing"
)

func TestGuestInternetRejectsUnsafeDNSAndAmbiguousDenials(t *testing.T) {
	for _, dns := range []string{"", "::1", "::ffff:1.1.1.1", "01.1.1.1", " 1.1.1.1", "1.1.1.1\n", "192.0.0.9", "169.254.169.254", "10.0.0.1", "192.168.5.15", "100.64.0.1", "224.0.0.1", "240.0.0.1", "203.0.113.1", "127.0.0.1"} {
		if _, err := parseGuestInternet(dns, "203.0.113.0/24"); err == nil {
			t.Fatalf("unsafe DNS accepted: %q", dns)
		}
	}
	// Include both ends of every denied range, not only representative values.
	for _, value := range internetReservedIPv4 {
		prefix := netip.MustParsePrefix(value)
		last := prefix.Addr().As4()
		for bit := prefix.Bits(); bit < 32; bit++ {
			last[bit/8] |= 1 << (7 - bit%8)
		}
		for _, address := range []netip.Addr{prefix.Addr(), netip.AddrFrom4(last)} {
			if publicGuestIPv4(address.String()) {
				t.Fatalf("special-use boundary %s accepted from %s", address, value)
			}
		}
	}
	for _, denied := range []string{"", "0.0.0.0/0", "203.0.113.1/24", "2001:db8::/32", "::ffff:192.0.2.1/128", "203.0.113.0/24,", "203.0.113.0/24,192.0.2.0/24", "203.0.113.0/24,203.0.113.0/24", "1.1.1.1/32", "1.0.0.0/8", "203.0.113.0/24\n", strings.Repeat("203.0.113.0/24,", 80)} {
		if _, err := parseGuestInternet("1.1.1.1", denied); err == nil {
			t.Fatalf("ambiguous or unsafe deny policy accepted: %q", denied)
		}
	}
	for _, dns := range []string{"1.1.1.1", "8.8.8.8", "9.9.9.9"} {
		policy, err := parseGuestInternet(dns, "192.0.2.0/24,203.0.113.0/24")
		if err != nil || policy.DNSIPv4 != dns || policy.DeniedIPv4 != "192.0.2.0/24,203.0.113.0/24" {
			t.Fatal("canonical public policy changed", err)
		}
	}
}

func TestGuestInternetSnapshotsAllHostAddressesAndControl(t *testing.T) {
	control, err := parseGuestControl("https://api.example.test:8443", "192.0.2.7")
	if err != nil {
		t.Fatal(err)
	}
	config, err := parseGuestInternet("1.1.1.1", "203.0.113.0/24")
	if err != nil {
		t.Fatal(err)
	}
	host := networkHostInventory{complete: true, hostIPv4: []netip.Addr{netip.MustParseAddr("127.0.0.1"), netip.MustParseAddr("192.168.5.15"), netip.MustParseAddr("8.8.8.8")}}
	policy, err := config.withHost(host, control)
	if err != nil || policy.DeniedIPv4 != "127.0.0.1/32,192.0.2.7/32,192.168.5.15/32,203.0.113.0/24,8.8.8.8/32" {
		t.Fatal("host/control boundary not preserved", err)
	}
	if repeated, err := policy.withHost(host, control); err != nil || repeated != policy {
		t.Fatal("repeated snapshot not canonical", err)
	}
	for _, invalid := range []networkHostInventory{
		{}, {complete: true}, {hostIPv4: host.hostIPv4},
		{complete: true, hostIPv4: []netip.Addr{netip.MustParseAddr(control.IPv4)}},
		{complete: true, hostIPv4: []netip.Addr{netip.MustParseAddr(config.DNSIPv4)}},
		{complete: true, hostIPv4: []netip.Addr{netip.MustParseAddr("::1")}},
		{complete: true, hostIPv4: []netip.Addr{{}}},
	} {
		if _, err := config.withHost(invalid, control); err == nil {
			t.Fatal("incomplete or unsafe Host inventory admitted")
		}
	}
	if _, err := config.withHost(host, guestControl{}); err == nil {
		t.Fatal("unvalidated control accepted")
	}
	control.Port++
	if _, err := config.withHost(host, control); err == nil {
		t.Fatal("inconsistent control accepted")
	}
}

func TestGuestInternetBoundsCombinedHostPolicy(t *testing.T) {
	var prefixes []string
	for index := range 33 {
		prefixes = append(prefixes, fmt.Sprintf("203.0.113.%d/32", index))
	}
	slices.Sort(prefixes)
	if _, err := parseGuestInternet("1.1.1.1", strings.Join(prefixes, ",")); err == nil {
		t.Fatal("more than 32 denial prefixes admitted")
	}
	policy, err := parseGuestInternet("1.1.1.1", strings.Join(prefixes[:32], ","))
	if err != nil {
		t.Fatal("32 canonical prefixes refused", err)
	}
	control, _ := parseGuestControl("https://api.example.test", "192.0.2.7")
	host := networkHostInventory{complete: true, hostIPv4: []netip.Addr{netip.MustParseAddr("127.0.0.1")}}
	if _, err := policy.withHost(host, control); err == nil {
		t.Fatal("Host/control additions exceeded bounded policy")
	}
}
