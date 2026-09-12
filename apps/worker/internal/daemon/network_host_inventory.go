package daemon

import (
	"context"
	"encoding/json"
	"encoding/xml"
	"net"
	"net/netip"
	"slices"
	"strconv"
	"strings"
	"time"
)

// A fresh, read-only collision snapshot, not ownership or firewall proof.
// Recollect immediately before creation; external host configuration remains
// an administrator-controlled boundary, not something the Worker may rewrite.
type networkHostInventory struct {
	complete   bool
	excluded   []netip.Prefix
	hostIPv4   []netip.Addr
	interfaces []net.Interface
	xml        []*networkXMLNode
}

type networkInventoryReader struct {
	query      vmQuery
	interfaces func() ([]net.Interface, error)
	addresses  func(net.Interface) ([]net.Addr, error)
	routes     func(context.Context) ([]byte, error)
}

func hostNetworkReader() networkInventoryReader {
	return networkInventoryReader{query: queryVirsh, interfaces: net.Interfaces,
		addresses: func(device net.Interface) ([]net.Addr, error) { return device.Addrs() }, routes: queryHostIPv4Routes}
}

func (inventory *networkHostInventory) exclude(prefix netip.Prefix) error {
	if !prefix.IsValid() || prefix.Addr().Is4In6() {
		return errRunNetwork
	}
	if !prefix.Addr().Is4() {
		return nil // Allocation is strictly IPv4; IPv6 filtering is a separate gate.
	}
	prefix = prefix.Masked()
	if !slices.Contains(inventory.excluded, prefix) {
		if len(inventory.excluded) >= 4096 {
			return errRunNetwork
		}
		inventory.excluded = append(inventory.excluded, prefix)
	}
	return nil
}

func (inventory networkHostInventory) available(network runNetwork) bool {
	if !inventory.complete || !network.valid(network.UUID) {
		return false
	}
	if network.Version == 2 && slices.Contains(inventory.hostIPv4, netip.MustParseAddr(network.ControlIPv4)) {
		return false // An explicit endpoint never grants access to the Worker host.
	}
	subnet, _ := netip.ParsePrefix(network.CIDR)
	for _, prefix := range inventory.excluded {
		if prefix.Overlaps(subnet) {
			return false
		}
	}
	for _, device := range inventory.interfaces {
		mac := device.HardwareAddr.String()
		if device.Name == network.Bridge || mac == network.GuestMAC || mac == network.GatewayMAC {
			return false
		}
	}
	for _, node := range inventory.xml {
		if networkIdentityReferenced(node, network) {
			return false
		}
	}
	return true
}

func networkIdentityReferenced(node *networkXMLNode, network runNetwork) bool {
	values := []string{network.UUID, network.Name, network.Filter, network.Bridge}
	if slices.Contains(values, node.Text) {
		return true
	}
	// MAC spelling/case in foreign XML cannot hide an identity collision.
	for _, attr := range node.Attrs {
		if slices.Contains(values, attr.Value) {
			return true
		}
		mac, err := net.ParseMAC(attr.Value)
		if err == nil && (mac.String() == network.GuestMAC || mac.String() == network.GatewayMAC) {
			return true
		}
	}
	for _, child := range node.Children {
		if networkIdentityReferenced(child, network) {
			return true
		}
	}
	return false
}

func (reader networkInventoryReader) read(ctx context.Context) (networkHostInventory, error) {
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	var inventory networkHostInventory
	devices, err := reader.interfaces()
	if err != nil || len(devices) == 0 || len(devices) > 1024 {
		return inventory, errRunNetwork
	}
	names := map[string]bool{}
	for _, device := range devices {
		if device.Name == "" || names[device.Name] {
			return networkHostInventory{}, errRunNetwork
		}
		names[device.Name] = true
		addresses, err := reader.addresses(device)
		if err != nil || len(addresses) > 4096 {
			return networkHostInventory{}, errRunNetwork
		}
		for _, address := range addresses {
			if address == nil {
				return networkHostInventory{}, errRunNetwork
			}
			prefix, err := netip.ParsePrefix(address.String())
			if err != nil || inventory.exclude(prefix) != nil {
				return networkHostInventory{}, errRunNetwork
			}
			if prefix.Addr().Is4() && !slices.Contains(inventory.hostIPv4, prefix.Addr()) {
				if len(inventory.hostIPv4) >= 4096 {
					return networkHostInventory{}, errRunNetwork
				}
				inventory.hostIPv4 = append(inventory.hostIPv4, prefix.Addr())
			}
		}
	}
	inventory.interfaces = devices
	routes, err := reader.routes(ctx)
	if err != nil || inventory.routeExclusions(routes) != nil {
		return networkHostInventory{}, errRunNetwork
	}
	query := &vmCleanup{query: reader.query}
	bytesRead := 0
	for _, kind := range []string{"network", "domain"} {
		list, dump := query.networkIDs, "net-dumpxml"
		if kind == "domain" {
			list, dump = query.domains, "dumpxml"
		}
		ids, err := list(ctx)
		if err != nil {
			return networkHostInventory{}, errRunNetwork
		}
		persistent, err := list(ctx, "--persistent")
		if err != nil {
			return networkHostInventory{}, errRunNetwork
		}
		for _, id := range persistent {
			if !slices.Contains(ids, id) {
				return networkHostInventory{}, errRunNetwork
			}
		}
		for _, id := range ids {
			views := [][]string{{dump, id}}
			if slices.Contains(persistent, id) {
				views = append(views, []string{dump, id, "--inactive"})
			}
			for _, args := range views {
				data, err := query.command(ctx, args...)
				bytesRead += len(data)
				if err != nil || bytesRead > 4<<20 {
					return networkHostInventory{}, errRunNetwork
				}
				node, err := parseNetworkXML(data, kind)
				if err != nil || node.child("uuid") == nil || node.child("uuid").Text != id || node.child("name") == nil || node.child("name").Text == "" {
					return networkHostInventory{}, errRunNetwork
				}
				if kind == "network" && inventory.xmlExclusions(node) != nil {
					return networkHostInventory{}, errRunNetwork
				}
				inventory.xml = append(inventory.xml, node)
			}
		}
	}
	if ctx.Err() != nil {
		return networkHostInventory{}, errRunNetwork
	}
	inventory.complete = true
	return inventory, nil
}

func (inventory *networkHostInventory) xmlExclusions(node *networkXMLNode) error {
	for _, child := range node.Children {
		if child.Name.Space != "" || (child.Name.Local != "ip" && child.Name.Local != "route") {
			continue
		}
		address, prefixText, mask := child.attr("address"), child.attr("prefix"), child.attr("netmask")
		family := child.attr("family")
		if family != "" && family != "ipv4" && family != "ipv6" || prefixText != "" && mask != "" {
			return errRunNetwork
		}
		route := child.Name == (xml.Name{Local: "route"})
		if route && address == "" {
			address = "0.0.0.0"
			if family == "ipv6" {
				address = "::"
			}
		}
		ip, err := netip.ParseAddr(address)
		if err != nil || ip.Zone() != "" || ip.Is4In6() || ip.Is6() != (family == "ipv6") {
			return errRunNetwork
		}
		bits := 0
		if mask != "" {
			parsed, err := netip.ParseAddr(mask)
			if err != nil || !parsed.Is4() || !ip.Is4() {
				return errRunNetwork
			}
			value := parsed.As4()
			var size int
			bits, size = net.IPMask(value[:]).Size()
			if size != 32 {
				return errRunNetwork
			}
		} else if prefixText != "" {
			bits, err = strconv.Atoi(prefixText)
			if err != nil || strconv.Itoa(bits) != prefixText {
				return errRunNetwork
			}
		} else if !route {
			return errRunNetwork // No implicit/classful address-mask assumptions.
		}
		prefix := netip.PrefixFrom(ip, bits)
		if !prefix.IsValid() || !route && bits == 0 {
			return errRunNetwork
		}
		if !route || bits != 0 {
			if err := inventory.exclude(prefix); err != nil {
				return err
			}
		}
		if route {
			gateway, err := netip.ParseAddr(child.attr("gateway"))
			if err != nil || gateway.Zone() != "" || gateway.Is4() != ip.Is4() || inventory.exclude(netip.PrefixFrom(gateway, gateway.BitLen())) != nil {
				return errRunNetwork
			}
		}
	}
	return nil
}

func (inventory *networkHostInventory) routeExclusions(data []byte) error {
	if len(data) > 256<<10 || validateWorkerRecoveryJSON(data) != nil {
		return errRunNetwork
	}
	var routes []map[string]json.RawMessage
	if json.Unmarshal(data, &routes) != nil || routes == nil || len(routes) > 4096 {
		return errRunNetwork
	}
	for _, route := range routes {
		var destination string
		if json.Unmarshal(route["dst"], &destination) != nil || destination == "" {
			return errRunNetwork
		}
		if destination != "default" {
			if !strings.Contains(destination, "/") {
				destination += "/32"
			}
			prefix, err := netip.ParsePrefix(destination)
			if err != nil || !prefix.Addr().Is4() || prefix != prefix.Masked() || inventory.exclude(prefix) != nil {
				return errRunNetwork
			}
		}
		if err := inventory.routeGateways(route); err != nil {
			return err
		}
	}
	return nil
}

func (inventory *networkHostInventory) routeGateways(route map[string]json.RawMessage) error {
	// Encapsulation and external nexthop IDs require additional inventories.
	// Never treat an unsupported route as proof that the pool is unoccupied.
	for _, field := range []string{"via", "encap", "nhid"} {
		if _, present := route[field]; present {
			return errRunNetwork
		}
	}
	for _, field := range []string{"gateway", "prefsrc"} {
		if data, present := route[field]; present {
			var value string
			if json.Unmarshal(data, &value) != nil {
				return errRunNetwork
			}
			ip, err := netip.ParseAddr(value)
			if err != nil || !ip.Is4() || inventory.exclude(netip.PrefixFrom(ip, 32)) != nil {
				return errRunNetwork
			}
		}
	}
	if data, present := route["nexthops"]; present {
		var next []map[string]json.RawMessage
		if json.Unmarshal(data, &next) != nil || len(next) == 0 || len(next) > 1024 {
			return errRunNetwork
		}
		for _, hop := range next {
			if hop == nil || hop["nexthops"] != nil || inventory.routeGateways(hop) != nil {
				return errRunNetwork
			}
		}
	}
	return nil
}
