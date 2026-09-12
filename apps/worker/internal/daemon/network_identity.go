package daemon

import (
	"crypto/sha256"
	"encoding/binary"
	"errors"
	"fmt"
	"net/netip"
)

var errRunNetwork = errors.New("VM network identity unavailable or invalid")

// A proposal, not proof that any network is owned or isolated. Callers must
// persist it before creating resources and verify live ownership on cleanup.
// Version 2 adds only a validated, credential-free control origin and IP pin.
// No credential, DNS lookup or caller-supplied device name belongs here.
type runNetwork struct {
	Version       int    `json:"version"`
	UUID          string `json:"uuid"`
	Name          string `json:"name"`
	Filter        string `json:"filter"`
	Bridge        string `json:"bridge"`
	CIDR          string `json:"cidr"`
	Gateway       string `json:"gateway"`
	Guest         string `json:"guest"`
	GatewayMAC    string `json:"gateway_mac"`
	GuestMAC      string `json:"guest_mac"`
	ControlOrigin string `json:"control_origin,omitempty"`
	ControlIPv4   string `json:"control_ipv4,omitempty"`
}

func privateNetworkPool(value string) (netip.Prefix, error) {
	pool, err := netip.ParsePrefix(value)
	// Bound the search to 16,384 /30s. Never silently normalize host bits or
	// IPv4-mapped IPv6; both would change an operator's configured boundary.
	if err != nil || !pool.Addr().Is4() || pool.Bits() < 16 || pool.Bits() > 30 ||
		pool != pool.Masked() || pool.String() != value || !pool.Addr().IsPrivate() {
		return netip.Prefix{}, errRunNetwork
	}
	return pool, nil
}

func networkAt(runID string, subnet netip.Prefix) (runNetwork, error) {
	if !runUUID.MatchString(runID) || !subnet.IsValid() || subnet.Bits() != 30 ||
		!subnet.Addr().Is4() || !subnet.Addr().IsPrivate() || subnet != subnet.Masked() {
		return runNetwork{}, errRunNetwork
	}
	digest := sha256.Sum256([]byte("kelpie-network-v1:" + runID))
	return runNetwork{
		Version: 1, UUID: runID, Name: "kelpie-net-" + runID, Filter: "kelpie-filter-" + runID,
		Bridge: fmt.Sprintf("kp%x", digest[:6]), CIDR: subnet.String(),
		Gateway: subnet.Addr().Next().String(), Guest: subnet.Addr().Next().Next().String(),
		GatewayMAC: fmt.Sprintf("02:%02x:%02x:%02x:%02x:%02x", digest[6], digest[7], digest[8], digest[9], digest[10]),
		GuestMAC:   fmt.Sprintf("06:%02x:%02x:%02x:%02x:%02x", digest[6], digest[7], digest[8], digest[9], digest[10]),
	}, nil
}

func (network runNetwork) valid(runID string) bool {
	subnet, err := netip.ParsePrefix(network.CIDR)
	if err != nil {
		return false
	}
	want, err := networkAt(runID, subnet)
	if network.Version == 2 {
		control, controlErr := parseGuestControl(network.ControlOrigin, network.ControlIPv4)
		if controlErr != nil || subnet.Contains(netip.MustParseAddr(control.IPv4)) {
			return false
		}
		want.Version, want.ControlOrigin, want.ControlIPv4 = 2, control.Origin, control.IPv4
	}
	return err == nil && network == want
}

// allocateRunNetwork is side-effect free. The single store owner must serialize
// allocation with durable creation and supply all unreleased runs plus current
// host networks/routes. Released records may be omitted only after confirmed
// physical cleanup; this function cannot discover or certify that fact.
func allocateRunNetwork(runID, poolText string, occupied []runNetwork, excluded []netip.Prefix) (runNetwork, error) {
	pool, err := privateNetworkPool(poolText)
	if err != nil || !runUUID.MatchString(runID) || len(occupied) > 4096 || len(excluded) > 4096 {
		return runNetwork{}, errRunNetwork
	}
	names := make(map[string]bool, len(occupied))
	bridges := make(map[string]bool, len(occupied))
	macs := make(map[string]bool, 2*len(occupied))
	blocked := make([]netip.Prefix, 0, len(occupied)+len(excluded))
	for _, network := range occupied {
		if !network.valid(network.UUID) || names[network.Name] || bridges[network.Bridge] ||
			macs[network.GuestMAC] || macs[network.GatewayMAC] {
			return runNetwork{}, errRunNetwork
		}
		subnet, _ := netip.ParsePrefix(network.CIDR) // validated above
		for _, prior := range blocked {
			if prior.Overlaps(subnet) {
				return runNetwork{}, errRunNetwork
			}
		}
		blocked = append(blocked, subnet)
		names[network.Name], bridges[network.Bridge] = true, true
		macs[network.GuestMAC], macs[network.GatewayMAC] = true, true
	}
	for _, subnet := range excluded {
		if !subnet.IsValid() || !subnet.Addr().Is4() || subnet != subnet.Masked() {
			return runNetwork{}, errRunNetwork
		}
		blocked = append(blocked, subnet)
	}
	identity, _ := networkAt(runID, netip.PrefixFrom(pool.Addr(), 30))
	if names[identity.Name] || bridges[identity.Bridge] || macs[identity.GuestMAC] || macs[identity.GatewayMAC] {
		return runNetwork{}, errRunNetwork // no adoption or identity renaming on collision
	}
	base := pool.Addr().As4()
	baseValue := binary.BigEndian.Uint32(base[:])
	slots := uint32(1) << (30 - pool.Bits())
	digest := sha256.Sum256([]byte("kelpie-subnet-v1:" + runID))
	start := binary.BigEndian.Uint32(digest[:4]) % slots
	for offset := uint32(0); offset < slots; offset++ {
		var address [4]byte
		binary.BigEndian.PutUint32(address[:], baseValue+4*((start+offset)%slots))
		subnet := netip.PrefixFrom(netip.AddrFrom4(address), 30)
		available := true
		for _, reserved := range blocked {
			if reserved.Overlaps(subnet) {
				available = false
				break
			}
		}
		if available {
			return networkAt(runID, subnet)
		}
	}
	return runNetwork{}, errRunNetwork // exhaustion never falls back to a shared network
}
