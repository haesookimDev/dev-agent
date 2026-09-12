package daemon

import (
	"errors"
	"net/netip"
	"os"
	"runtime"
	"strings"
	"syscall"
)

const installedNetworkLogHook = "/etc/libvirt/hooks/network.d/50-kelpie-egress"

// Worker-side admission is read-only: no root lock, nft command, helper RPC,
// chmod or configurable privileged paths. Only the installed root hook writes
// receipts. A durable start intent distinguishes never-started runs from a
// missing log lifecycle after a crash; it survives ordinary artifact cleanup.
type networkStartIntent struct {
	RunID  string `json:"run_id"`
	BootID string `json:"boot_id"`
}

func (s *runStore) networkIntent(runID string) (networkStartIntent, bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !runUUID.MatchString(runID) || !validStoreDirectory(s.root, runID) {
		return networkStartIntent{}, false, errRunStore
	}
	var intent networkStartIntent
	err := s.readRecord(runID+"/network-start.json", &intent)
	if errors.Is(err, os.ErrNotExist) {
		return intent, false, nil
	}
	if err != nil || intent.RunID != runID || !workUUID.MatchString(intent.BootID) {
		return intent, false, errRunStore
	}
	return intent, true, nil
}

func (s *runStore) beginNetwork(runID, bootID string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	run, err := s.load(runID)
	if err != nil || run.Record.Schema != 5 || run.Phase != "prepared" || !workUUID.MatchString(bootID) {
		return errRunStore
	}
	return s.writeExclusive(runID+"/network-start.json", networkStartIntent{RunID: runID, BootID: bootID})
}

// No executable is required for cleanup: removing or breaking a hook must not
// prevent physical VM shutdown. Missing completion evidence still retains the
// API reservation. New admission verifies the fixed installed executable.
func openNetworkLogReceipts(admission bool) (*networkLogHook, error) {
	if runtime.GOOS != "linux" {
		return nil, errRunNetwork
	}
	paths := []string{"/", "/var", "/var/lib", networkLogDirectory}
	if admission {
		paths = append(paths, "/etc", "/etc/libvirt", "/etc/libvirt/hooks", "/etc/libvirt/hooks/network.d")
	}
	for _, path := range paths {
		info, err := os.Lstat(path)
		if err != nil || !logOwned(info, 0, true) {
			return nil, errRunNetwork
		}
	}
	if admission {
		info, err := os.Lstat(installedNetworkLogHook)
		if err != nil {
			return nil, errRunNetwork
		}
		stat, ok := info.Sys().(*syscall.Stat_t)
		if !ok || !info.Mode().IsRegular() || stat.Uid != 0 || stat.Nlink != 1 || info.Mode() != 0755 {
			return nil, errRunNetwork
		}
	}
	boot, err := os.ReadFile("/proc/sys/kernel/random/boot_id")
	if err != nil || !workUUID.MatchString(strings.TrimSpace(string(boot))) {
		return nil, errRunNetwork
	}
	before, err := os.Lstat(networkLogDirectory)
	if err != nil || before.Mode().Perm() != 0755 {
		return nil, errRunNetwork
	}
	root, err := os.OpenRoot(networkLogDirectory)
	if err != nil {
		return nil, errRunNetwork
	}
	after, err := root.Stat(".")
	if err != nil || !os.SameFile(before, after) {
		_ = root.Close()
		return nil, errRunNetwork
	}
	return &networkLogHook{root: root, owner: 0, bootID: strings.TrimSpace(string(boot))}, nil
}

func (h *networkLogHook) verifyReceipt(network runNetwork, phase, bootID string) error {
	if !network.valid(network.UUID) {
		return errRunNetwork
	}
	// The root hook reconstructs only the network identity from libvirt XML;
	// policy fields cannot change, broaden or forge that logging identity.
	network, err := networkAt(network.UUID, netip.MustParsePrefix(network.CIDR))
	if err != nil {
		return err
	}
	pending, hasPending, errP := h.read(network, "pending")
	active, hasActive, errA := h.read(network, "active")
	stopped, hasStopped, errS := h.read(network, "stopped")
	if errP != nil || errA != nil || errS != nil {
		return errRunNetwork
	}
	if phase == "absent" {
		if hasPending || hasActive || hasStopped {
			return errRunNetwork
		}
		return nil
	}
	if !workUUID.MatchString(bootID) || !hasPending || !hasActive || pending.BootID != bootID || active.BootID != bootID {
		return errRunNetwork
	}
	if phase == "active" && h.bootID == bootID && !hasStopped {
		return nil
	}
	active.Phase = "stopped"
	if phase == "stopped" && hasStopped && stopped == active {
		return nil
	}
	return errRunNetwork
}

type networkReceiptCheck func(runNetwork, string, string) (string, error)

func checkNetworkLogReceipt(network runNetwork, phase, bootID string) (string, error) {
	reader, err := openNetworkLogReceipts(phase != "stopped" && phase != "unused")
	if err != nil {
		return "", err
	}
	defer reader.root.Close()
	if phase == "unused" {
		phase = "absent"
	}
	return reader.bootID, reader.verifyReceipt(network, phase, bootID)
}

func (c *vmCleanup) loggingStopped(network runNetwork) error {
	if network.Version != 3 {
		return nil
	}
	intent, present, err := c.store.networkIntent(network.UUID)
	if err != nil {
		return errVMCleanup
	}
	phase := "stopped"
	if !present {
		phase = "unused"
	}
	check := c.logReceipt
	if check == nil {
		check = checkNetworkLogReceipt
	}
	if _, err := check(network, phase, intent.BootID); err != nil {
		return errVMCleanup
	}
	return nil
}
