package daemon

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"time"
)

const networkLogDirectory = "/var/lib/kelpie-network-guard"

// Records contain only network identity, kernel identity and lifecycle state.
// They are written exclusively by the root hook, never by the Worker or VM.
// A boot ID prevents reused kernel handles after reboot becoming authority.
type networkLogRecord struct {
	Version     int        `json:"version"`
	Network     runNetwork `json:"network"`
	BootID      string     `json:"boot_id"`
	Phase       string     `json:"phase"`
	Fingerprint string     `json:"fingerprint"`
	Handle      uint64     `json:"handle"`
}

type networkLogHook struct {
	root   *os.Root
	lock   *os.File
	owner  uint32
	bootID string
	query  networkLogCommand
	ready  func(context.Context) error
}

// RunNetworkLogHook is a libvirt network.d executable entry point, not a
// privileged Worker RPC. It never calls libvirt, reads Worker credentials,
// executes supplied commands or grants traffic. No configurable root path.
func RunNetworkLogHook(ctx context.Context, args []string, input io.Reader) error {
	if len(args) != 4 || args[3] != "-" {
		return errRunNetwork
	}
	if !strings.HasPrefix(args[0], "kelpie-net-") {
		return nil // leave unrelated networks to their own hooks
	}
	if runtime.GOOS != "linux" || os.Geteuid() != 0 {
		return errRunNetwork
	}
	data, err := io.ReadAll(io.LimitReader(input, (256<<10)+1))
	if err != nil {
		return errRunNetwork
	}
	network, err := networkLogIdentity(args[0], data)
	if err != nil {
		return errRunNetwork
	}
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	boot, err := os.ReadFile("/proc/sys/kernel/random/boot_id")
	if err != nil || !workUUID.MatchString(strings.TrimSpace(string(boot))) {
		return errRunNetwork
	}
	// The fixed parent chain must not be writable by unprivileged accounts.
	for _, path := range []string{"/", "/var", "/var/lib"} {
		info, err := os.Lstat(path)
		if err != nil || !logOwned(info, 0, true) {
			return errRunNetwork
		}
	}
	hook, err := openNetworkLogHook(networkLogDirectory, 0, strings.TrimSpace(string(boot)))
	if err != nil {
		return errRunNetwork
	}
	defer hook.close()
	hook.query, hook.ready = queryNetworkLog, networkJournalReady
	return hook.apply(ctx, network, args[1], args[2])
}

func logOwned(info os.FileInfo, owner uint32, directory bool) bool {
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || stat.Uid != owner || info.Mode()&(os.ModeSymlink|os.ModeSetuid|os.ModeSetgid|os.ModeSticky) != 0 {
		return false
	}
	if directory {
		return info.IsDir() && info.Mode().Perm()&0022 == 0 && info.Mode().Perm()&0700 == 0700
	}
	return info.Mode().IsRegular() && stat.Nlink == 1 && info.Mode().Perm() == 0644
}

func openNetworkLogHook(path string, owner uint32, bootID string) (*networkLogHook, error) {
	if !filepath.IsAbs(path) || filepath.Clean(path) != path || !workUUID.MatchString(bootID) {
		return nil, errRunNetwork
	}
	// Installation creates this directory. Runtime never chmods/chowns an
	// existing path, follows its final symlink, or adopts an unsafe directory.
	before, err := os.Lstat(path)
	if err != nil || !logOwned(before, owner, true) || before.Mode().Perm() != 0755 {
		return nil, errRunNetwork
	}
	root, err := os.OpenRoot(path)
	if err != nil {
		return nil, errRunNetwork
	}
	fail := func() (*networkLogHook, error) { _ = root.Close(); return nil, errRunNetwork }
	after, err := root.Stat(".")
	if err != nil || !os.SameFile(before, after) {
		return fail()
	}
	lock, err := root.OpenFile(".lock", os.O_RDWR|os.O_CREATE|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0600)
	if err != nil {
		return fail()
	}
	info, err := lock.Stat()
	if err != nil || !privateOwned(info, false) || owner != uint32(os.Geteuid()) || info.Size() != 0 || syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB) != nil {
		_ = lock.Close()
		return fail()
	}
	return &networkLogHook{root: root, lock: lock, owner: owner, bootID: bootID}, nil
}

func (h *networkLogHook) close() {
	_ = h.lock.Close()
	_ = h.root.Close()
}

func (h *networkLogHook) read(network runNetwork, phase string) (networkLogRecord, bool, error) {
	name := network.UUID + "." + phase + ".json"
	file, err := h.root.OpenFile(name, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
	if errors.Is(err, os.ErrNotExist) {
		return networkLogRecord{}, false, nil
	}
	if err != nil {
		return networkLogRecord{}, false, errRunNetwork
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil || !logOwned(info, h.owner, false) || info.Size() < 1 || info.Size() > 4096 {
		return networkLogRecord{}, false, errRunNetwork
	}
	data, err := io.ReadAll(io.LimitReader(file, 4097))
	var record networkLogRecord
	if err != nil || len(data) > 4096 || json.Unmarshal(data, &record) != nil {
		return record, false, errRunNetwork
	}
	canonical, _ := json.Marshal(record)
	// Our immutable records have a single canonical encoding. This rejects
	// duplicates, case aliases, unknown fields and hand-edited partial records.
	if !bytes.Equal(data, canonical) || record.Version != 1 || record.Network != network || record.Phase != phase ||
		!workUUID.MatchString(record.BootID) || (phase == "pending" && (record.Handle != 0 || record.Fingerprint != "")) ||
		(phase != "pending" && (record.Handle == 0 || len(record.Fingerprint) != 64 || strings.Trim(record.Fingerprint, "0123456789abcdef") != "")) {
		return record, false, errRunNetwork
	}
	return record, true, nil
}

func (h *networkLogHook) write(record networkLogRecord) error {
	data, err := json.Marshal(record)
	if err != nil || len(data) > 4096 {
		return errRunNetwork
	}
	file, err := h.root.OpenFile(record.Network.UUID+"."+record.Phase+".json", os.O_WRONLY|os.O_CREATE|os.O_EXCL|syscall.O_NOFOLLOW, 0644)
	if err != nil {
		return errRunNetwork
	}
	_, writeErr := file.Write(data)
	syncErr, closeErr := file.Sync(), file.Close()
	directory, err := h.root.Open(".")
	if err != nil {
		return errRunNetwork
	}
	defer directory.Close()
	if writeErr != nil || syncErr != nil || closeErr != nil || directory.Sync() != nil {
		return errRunNetwork
	}
	return nil
}

func (h *networkLogHook) apply(ctx context.Context, network runNetwork, operation, phase string) error {
	if ctx.Err() != nil || network.Version != 1 || !network.valid(network.UUID) {
		return errRunNetwork
	}
	if operation == "start" && phase == "begin" {
		return h.start(ctx, network)
	}
	if operation == "stopped" && phase == "end" {
		return h.stop(ctx, network)
	}
	if phase == "begin" && (operation == "started" || operation == "port-created" || operation == "updated") {
		_, err := h.active(ctx, network)
		return err
	}
	if operation == "port-deleted" && phase == "begin" {
		return nil // missing logging must not prevent removal of a guest NIC
	}
	return errRunNetwork
}

func (h *networkLogHook) start(ctx context.Context, network runNetwork) error {
	for _, phase := range []string{"pending", "active", "stopped"} {
		if _, exists, err := h.read(network, phase); err != nil || exists {
			return errRunNetwork // one run never reuses an earlier log identity
		}
	}
	if h.ready(ctx) != nil {
		return errRunNetwork
	}
	if present, err := networkLogPresent(ctx, h.query, network); err != nil || present {
		return errRunNetwork
	}
	record := networkLogRecord{Version: 1, Network: network, BootID: h.bootID, Phase: "pending"}
	if err := h.write(record); err != nil {
		return err
	}
	rules, err := networkLogRules(network)
	if err != nil {
		return err
	}
	if _, err := h.query(ctx, rules, "--file", "-"); err != nil {
		return errRunNetwork
	}
	record.Fingerprint, record.Handle, err = networkLogSnapshot(ctx, h.query, network)
	if err != nil {
		return err
	}
	record.Phase = "active"
	return h.write(record)
}

func (h *networkLogHook) active(ctx context.Context, network runNetwork) (networkLogRecord, error) {
	pending, exists, err := h.read(network, "pending")
	if err != nil || !exists || pending.BootID != h.bootID || h.ready(ctx) != nil {
		return networkLogRecord{}, errRunNetwork
	}
	if _, exists, err := h.read(network, "stopped"); err != nil || exists {
		return networkLogRecord{}, errRunNetwork
	}
	record, exists, err := h.read(network, "active")
	if err != nil || !exists || record.BootID != h.bootID {
		return networkLogRecord{}, errRunNetwork
	}
	fingerprint, handle, err := networkLogSnapshot(ctx, h.query, network)
	if err != nil || record.Fingerprint != fingerprint || record.Handle != handle {
		return networkLogRecord{}, errRunNetwork
	}
	return record, nil
}

func (h *networkLogHook) stop(ctx context.Context, network runNetwork) error {
	pending, exists, err := h.read(network, "pending")
	if err != nil || !exists {
		return errRunNetwork
	}
	record, exists, err := h.read(network, "active")
	if err != nil || !exists || record.BootID != pending.BootID {
		return errRunNetwork // ambiguous create/receipt crash gap needs recovery
	}
	stopped, already, err := h.read(network, "stopped")
	if err != nil {
		return err
	}
	present, err := networkLogPresent(ctx, h.query, network)
	if err != nil {
		return err
	}
	if already {
		record.Phase = "stopped"
		if present || record != stopped {
			return errRunNetwork
		}
		return nil
	}
	if present {
		// Journald may be down during shutdown: removal is still safe once
		// the recorded boot, exact rules and handle have been established.
		fingerprint, handle, err := networkLogSnapshot(ctx, h.query, network)
		if err != nil || record.BootID != h.bootID || record.Fingerprint != fingerprint || record.Handle != handle {
			return errRunNetwork
		}
		if deleteNetworkLog(ctx, h.query, record.Handle) != nil {
			return errRunNetwork
		}
	}
	if present, err := networkLogPresent(ctx, h.query, network); err != nil || present {
		return errRunNetwork
	}
	record.Phase = "stopped"
	return h.write(record)
}
