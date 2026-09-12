package daemon

import (
	"context"
	"net/netip"
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"syscall"
	"time"
)

type networkProvisioner struct {
	store      *runStore
	reader     networkInventoryReader
	logReceipt networkReceiptCheck
}

// The caller must bind this recorded run to its lifecycle before invoking
// create, and join this operation before cleanup. Errors retain ownership for
// that cleanup; no resource is adopted or automatically redefined on retry.
// Version 1 stays deny-all; version 2 installs the recorded control-only policy
// before any NIC may attach. Neither version grants general internet access.
// Version 3 additionally requires fresh root logging admission and a durable
// start intent; missing post-start confirmation never authorizes a guest NIC.
func (p networkProvisioner) create(ctx context.Context, runID string) error {
	ctx, cancel := context.WithTimeout(ctx, time.Minute)
	defer cancel()
	run, err := p.store.Load(runID)
	if err != nil || run.Phase != "prepared" || run.Record.Schema < 3 || run.Record.Network == nil {
		return errRunNetwork
	}
	network := *run.Record.Network
	checkReceipt := p.logReceipt
	if checkReceipt == nil {
		checkReceipt = checkNetworkLogReceipt
	}
	var logBoot string
	if network.Version == 3 {
		if _, exists, err := p.store.networkIntent(runID); err != nil || exists {
			return errRunNetwork
		}
		logBoot, err = checkReceipt(network, "absent", "")
		if err != nil || !workUUID.MatchString(logBoot) {
			return errRunNetwork
		}
	}
	cleanup := newVMCleanup(p.store, runID)
	cleanup.query = p.reader.query
	guard := func() error {
		current, err := p.store.Load(runID)
		if err != nil || current.Phase != "prepared" || !reflect.DeepEqual(current.Record, run.Record) || ctx.Err() != nil {
			return errRunNetwork
		}
		return nil
	}
	checkVacant := func() error {
		inventory, err := p.reader.read(ctx)
		if err != nil || !inventory.available(network) {
			return errRunNetwork
		}
		return guard()
	}
	if err := checkVacant(); err != nil {
		return err
	}
	if found, err := cleanup.inspectFilter(ctx, network); err != nil || found {
		return errRunNetwork
	}
	for _, definition := range []struct {
		name string
		body func() ([]byte, error)
	}{{"filter.xml", network.policyXML}, {"network.xml", network.definitionXML}} {
		body, err := definition.body()
		if err != nil || p.writeDefinition(runID, definition.name, body) != nil {
			return errRunNetwork
		}
	}
	if err := guard(); err != nil {
		return err
	}
	path := filepath.Join(p.store.root.Name(), runID)
	if _, err := cleanup.command(ctx, "nwfilter-define", filepath.Join(path, "filter.xml"), "--validate"); err != nil {
		return errRunNetwork
	}
	if found, err := cleanup.inspectFilter(ctx, network); err != nil || !found {
		return errRunNetwork
	}
	// The second snapshot cannot reuse the earlier pre-definition inventory.
	if err := checkVacant(); err != nil {
		return err
	}
	if _, err := cleanup.command(ctx, "net-define", filepath.Join(path, "network.xml"), "--validate"); err != nil {
		return errRunNetwork
	}
	if found, err := cleanup.inspectNetwork(ctx, network); err != nil || !found {
		return errRunNetwork
	}
	if found, err := cleanup.inspectFilter(ctx, network); err != nil || !found {
		return errRunNetwork
	}
	if err := guard(); err != nil {
		return err
	}
	if network.Version == 3 {
		if err := p.store.beginNetwork(runID, logBoot); err != nil {
			return err
		}
	}
	if _, err := cleanup.command(ctx, "net-start", network.UUID); err != nil {
		return errRunNetwork
	}
	if found, err := cleanup.inspectNetwork(ctx, network); err != nil || !found {
		return errRunNetwork
	}
	inactive, err := cleanup.networkIDs(ctx, "--inactive")
	if err != nil || slices.Contains(inactive, network.UUID) || p.bridgeReady(network) != nil {
		return errRunNetwork
	}
	if network.Version == 3 {
		if _, err := checkReceipt(network, "active", logBoot); err != nil {
			return err
		}
	}
	return guard()
}

func (p networkProvisioner) writeDefinition(runID, name string, data []byte) error {
	if !runUUID.MatchString(runID) || (name != "network.xml" && name != "filter.xml") || len(data) > 16<<10 {
		return errRunNetwork
	}
	file, err := p.store.root.OpenFile(runID+"/"+name, os.O_WRONLY|os.O_CREATE|os.O_EXCL|syscall.O_NOFOLLOW, 0600)
	if err != nil {
		return errRunNetwork
	}
	_, writeErr := file.Write(data)
	syncErr := file.Sync()
	closeErr := file.Close()
	if writeErr != nil || syncErr != nil || closeErr != nil || p.store.syncDirectory(runID) != nil {
		return errRunNetwork
	}
	return nil
}

func (p networkProvisioner) bridgeReady(network runNetwork) error {
	devices, err := p.reader.interfaces()
	if err != nil {
		return errRunNetwork
	}
	for _, device := range devices {
		if device.Name != network.Bridge {
			continue
		}
		if device.HardwareAddr.String() != network.GatewayMAC {
			return errRunNetwork
		}
		addresses, err := p.reader.addresses(device)
		if err != nil {
			return errRunNetwork
		}
		found := false
		for _, address := range addresses {
			if address == nil {
				return errRunNetwork
			}
			prefix, err := netip.ParsePrefix(address.String())
			if err != nil || prefix.Addr().Is4In6() {
				return errRunNetwork
			}
			if prefix.Addr().Is4() {
				if prefix.Addr().String() != network.Gateway || prefix.Bits() != 30 || found {
					return errRunNetwork
				}
				found = true
			}
		}
		if found {
			return nil
		}
	}
	return errRunNetwork
}
