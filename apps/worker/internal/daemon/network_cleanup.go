package daemon

import (
	"bytes"
	"context"
	"io"
	"net"
	"os"
	"slices"
	"strings"
	"syscall"
)

func networkUUIDInventory(data []byte) ([]string, error) {
	ids := strings.Fields(string(data))
	seen := map[string]bool{}
	for _, id := range ids {
		if !workUUID.MatchString(id) || seen[id] || len(ids) > 1024 {
			return nil, errVMCleanup
		}
		seen[id] = true
	}
	return ids, nil
}

func (c *vmCleanup) networkIDs(ctx context.Context, filters ...string) ([]string, error) {
	args := []string{"net-list", "--uuid"}
	if !slices.Contains(filters, "--inactive") {
		args = append(args, "--all")
	}
	data, err := c.command(ctx, append(args, filters...)...)
	if err != nil {
		return nil, err
	}
	return networkUUIDInventory(data)
}

// virsh has no machine-readable nwfilter-list or binding-list option on the
// supported libvirt 10 host. Require its C-locale header and bounded rows.
func networkTable(data []byte, header string, columns int) ([][]string, error) {
	lines := strings.Split(strings.TrimSpace(string(data)), "\n")
	if len(lines) < 2 || strings.Join(strings.Fields(lines[0]), " ") != header ||
		len(strings.TrimSpace(lines[1])) < 5 || strings.Trim(strings.TrimSpace(lines[1]), "-") != "" || len(lines) > 1026 {
		return nil, errVMCleanup
	}
	var rows [][]string
	seen := map[string]bool{}
	for _, line := range lines[2:] {
		fields := strings.Fields(line)
		if len(fields) != columns || seen[fields[0]] {
			return nil, errVMCleanup
		}
		seen[fields[0]] = true
		rows = append(rows, fields)
	}
	return rows, nil
}

func (c *vmCleanup) inspectNetwork(ctx context.Context, network runNetwork) (bool, error) {
	ids, err := c.networkIDs(ctx)
	if err != nil {
		return false, err
	}
	persistent, err := c.networkIDs(ctx, "--persistent")
	if err != nil {
		return false, err
	}
	autostart, err := c.networkIDs(ctx, "--autostart")
	if err != nil || slices.Contains(autostart, network.UUID) {
		return false, errVMCleanup
	}
	for _, id := range persistent {
		if !slices.Contains(ids, id) {
			return false, errVMCleanup
		}
	}
	expected, err := network.definitionXML()
	if err != nil {
		return false, errVMCleanup
	}
	for _, id := range ids {
		views := [][]string{{"net-dumpxml", id}}
		if slices.Contains(persistent, id) {
			views = append(views, []string{"net-dumpxml", id, "--inactive"})
		}
		for _, args := range views {
			data, err := c.command(ctx, args...)
			if err != nil {
				return false, err
			}
			node, err := parseNetworkXML(data, "network")
			if err != nil || node.child("uuid") == nil || node.child("uuid").Text != id || node.child("name") == nil || node.child("name").Text == "" {
				return false, errVMCleanup
			}
			if id == network.UUID {
				if !slices.Contains(persistent, id) || !matchesNetworkXML(data, expected, "network") {
					return false, errVMCleanup
				}
			} else if node.references(network.Name, network.Bridge, network.UUID, network.Filter, network.GuestMAC, network.GatewayMAC) {
				return false, errVMCleanup
			}
		}
	}
	return slices.Contains(ids, network.UUID), nil
}

func (c *vmCleanup) inspectFilter(ctx context.Context, network runNetwork) (bool, error) {
	data, err := c.command(ctx, "nwfilter-list")
	if err != nil {
		return false, err
	}
	rows, err := networkTable(data, "UUID Name", 2)
	if err != nil {
		return false, err
	}
	expected, err := network.policyXML()
	if err != nil {
		return false, errVMCleanup
	}
	found := false
	for _, row := range rows {
		if !workUUID.MatchString(row[0]) {
			return false, errVMCleanup
		}
		data, err := c.command(ctx, "nwfilter-dumpxml", row[0])
		if err != nil {
			return false, err
		}
		node, err := parseNetworkXML(data, "filter")
		if err != nil || node.child("uuid") == nil || node.child("uuid").Text != row[0] || node.attr("name") != row[1] {
			return false, errVMCleanup
		}
		if row[0] == network.UUID {
			if !matchesNetworkXML(data, expected, "filter") {
				return false, errVMCleanup
			}
			found = true
		} else if node.references(network.Filter, network.UUID) {
			return false, errVMCleanup
		}
	}
	data, err = c.command(ctx, "nwfilter-binding-list")
	if err != nil {
		return false, err
	}
	bindings, err := networkTable(data, "Port Dev Filter", 2)
	if err != nil {
		return false, err
	}
	for _, row := range bindings {
		if row[1] == network.Filter {
			return false, errVMCleanup
		}
	}
	return found, nil
}

func (c *vmCleanup) networkUnreferenced(ctx context.Context, network runNetwork) error {
	ids, err := c.domains(ctx)
	if err != nil || slices.Contains(ids, c.runID) {
		return errVMCleanup
	}
	persistent, err := c.domains(ctx, "--persistent")
	if err != nil {
		return err
	}
	for _, id := range persistent {
		if !slices.Contains(ids, id) {
			return errVMCleanup
		}
	}
	for _, id := range ids {
		views := [][]string{{"dumpxml", id}}
		if slices.Contains(persistent, id) {
			views = append(views, []string{"dumpxml", id, "--inactive"})
		}
		for _, args := range views {
			data, err := c.command(ctx, args...)
			if err != nil {
				return err
			}
			node, err := parseNetworkXML(data, "domain")
			if err != nil || node.child("uuid") == nil || node.child("uuid").Text != id ||
				node.child("name") == nil || node.child("name").Text == "" ||
				node.references(network.Name, network.Bridge, network.Filter, network.UUID, network.GuestMAC, network.GatewayMAC) {
				return errVMCleanup
			}
		}
	}
	return nil
}

func (c *vmCleanup) bridgeAbsent(network runNetwork) error {
	inspect := c.interfaces
	if inspect == nil {
		inspect = net.Interfaces
	}
	interfaces, err := inspect()
	if err != nil {
		return errVMCleanup
	}
	for _, device := range interfaces {
		if device.Name == network.Bridge {
			return errVMCleanup
		}
	}
	return nil
}

func (c *vmCleanup) networkCreationRecorded(network runNetwork) bool {
	// A reservation alone does not authorize adopting an existing resource.
	// Provisioning durably writes both exact definitions only after rejecting
	// existing resources, and before its first host mutation. Keep that intent
	// through partial failures; absence/tampering must retain the reservation.
	for _, definition := range []struct {
		name string
		body func() ([]byte, error)
	}{{"network.xml", network.definitionXML}, {"filter.xml", network.policyXML}} {
		expected, err := definition.body()
		if err != nil {
			return false
		}
		file, err := c.store.root.OpenFile(c.runID+"/"+definition.name, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
		if err != nil {
			return false
		}
		info, statErr := file.Stat()
		if statErr != nil || !privateOwned(info, false) || info.Size() != int64(len(expected)) {
			_ = file.Close()
			return false
		}
		data, readErr := io.ReadAll(io.LimitReader(file, int64(len(expected))+1))
		closeErr := file.Close()
		if readErr != nil || closeErr != nil || !bytes.Equal(data, expected) {
			return false
		}
	}
	return true
}

func (c *vmCleanup) cleanupNetwork(ctx context.Context, run ownedRun) error {
	if run.Record.Network == nil {
		return nil // legacy records never authorize changes to shared networking
	}
	network := *run.Record.Network
	verify := func() (bool, bool, error) {
		if err := c.networkUnreferenced(ctx, network); err != nil {
			return false, false, err
		}
		netExists, err := c.inspectNetwork(ctx, network)
		if err != nil {
			return false, false, err
		}
		filterExists, err := c.inspectFilter(ctx, network)
		return netExists, filterExists, err
	}
	netExists, filterExists, err := verify()
	if err != nil {
		return err
	}
	if run.Phase == "cleaned" || run.Phase == "released" {
		if netExists || filterExists {
			return errVMCleanup // do not delete resources reappearing after cleanup
		}
		return c.bridgeAbsent(network)
	}
	if (netExists || filterExists) && !c.networkCreationRecorded(network) {
		return errVMCleanup
	}
	if netExists {
		inactive, err := c.networkIDs(ctx, "--inactive")
		if err != nil {
			return err
		}
		if !slices.Contains(inactive, network.UUID) {
			data, err := c.command(ctx, "net-port-list", network.UUID)
			if err != nil {
				return err
			}
			ports, err := networkTable(data, "UUID", 1)
			if err != nil || len(ports) != 0 {
				return errVMCleanup
			}
			if _, _, err := verify(); err != nil {
				return err
			}
			if _, err := c.command(ctx, "net-destroy", network.UUID); err != nil {
				return err
			}
		}
		if _, _, err := verify(); err != nil {
			return err
		}
		inactive, err = c.networkIDs(ctx, "--inactive")
		if err != nil || !slices.Contains(inactive, network.UUID) {
			return errVMCleanup
		}
		if _, err := c.command(ctx, "net-undefine", network.UUID); err != nil {
			return err
		}
	}
	if filterExists {
		remainingNetwork, _, err := verify()
		if err != nil || remainingNetwork {
			return errVMCleanup
		}
		if err := c.bridgeAbsent(network); err != nil {
			return err
		}
		if _, err := c.command(ctx, "nwfilter-undefine", network.UUID); err != nil {
			return err
		}
	}
	netExists, filterExists, err = verify()
	if err != nil || netExists || filterExists {
		return errVMCleanup
	}
	return c.bridgeAbsent(network)
}
