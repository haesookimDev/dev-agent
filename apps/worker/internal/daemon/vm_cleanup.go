package daemon

import (
	"bytes"
	"context"
	"encoding/xml"
	"errors"
	"io"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"syscall"
	"time"
)

var errVMCleanup = errors.New("VM cleanup is unconfirmed; reservation retained")

type vmQuery func(context.Context, ...string) ([]byte, error)

type boundedVMOutput struct{ buffer bytes.Buffer }

func (b *boundedVMOutput) Write(data []byte) (int, error) {
	if len(data) > (256<<10)-b.buffer.Len() {
		return 0, errVMCleanup
	}
	return b.buffer.Write(data)
}

func queryVirsh(ctx context.Context, args ...string) ([]byte, error) {
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	command := exec.CommandContext(ctx, "virsh", append([]string{"--connect", "qemu:///system"}, args...)...)
	command.Env = []string{"PATH=" + os.Getenv("PATH"), "LC_ALL=C", "LANG=C"}
	command.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	command.Cancel = func() error { return syscall.Kill(-command.Process.Pid, syscall.SIGKILL) }
	command.WaitDelay = time.Second
	var output boundedVMOutput
	command.Stdout = &output
	// stderr is discarded, never embedded in errors, logs or retained buffers.
	if command.Run() != nil {
		return nil, errVMCleanup
	}
	return output.buffer.Bytes(), nil
}

type vmCleanup struct {
	store      *runStore
	runID      string
	query      vmQuery
	grace      time.Duration
	poll       time.Duration
	interfaces func() ([]net.Interface, error)
}

func newVMCleanup(store *runStore, runID string) *vmCleanup {
	return &vmCleanup{store: store, runID: runID, query: queryVirsh, grace: 20 * time.Second, poll: time.Second}
}

func (c *vmCleanup) command(ctx context.Context, args ...string) ([]byte, error) {
	data, err := c.query(ctx, args...)
	if err != nil || len(data) > 256<<10 {
		return nil, errVMCleanup
	}
	return data, nil
}

func (c *vmCleanup) domains(ctx context.Context, filters ...string) ([]string, error) {
	data, err := c.command(ctx, append([]string{"list", "--all", "--uuid"}, filters...)...)
	if err != nil {
		return nil, err
	}
	var ids []string
	seen := map[string]bool{}
	for _, line := range strings.Split(strings.TrimSpace(string(data)), "\n") {
		if line == "" {
			continue
		}
		if !workUUID.MatchString(line) || seen[line] || len(ids) >= 1024 {
			return nil, errVMCleanup
		}
		seen[line] = true
		ids = append(ids, line)
	}
	return ids, nil
}

type domainIdentity struct {
	XMLName      xml.Name `xml:"domain"`
	Names        []string `xml:"name"`
	UUIDs        []string `xml:"uuid"`
	Descriptions []string `xml:"description"`
	Disks        []struct {
		Type   string `xml:"type,attr"`
		Source struct {
			File string `xml:"file,attr"`
		} `xml:"source"`
	} `xml:"devices>disk"`
	NVRAM []string `xml:"os>nvram"`
}

func domainOwner(record runRecord) string {
	return "kelpie-owned-v1:" + record.RunID + ":" + record.WorkID
}

func validDomainXML(data []byte) bool {
	decoder := xml.NewDecoder(bytes.NewReader(data))
	depth, roots := 0, 0
	for {
		token, err := decoder.Token()
		if errors.Is(err, io.EOF) {
			return depth == 0 && roots == 1
		}
		if err != nil {
			return false
		}
		switch value := token.(type) {
		case xml.StartElement:
			if depth == 0 {
				roots++
				if roots != 1 || value.Name.Local != "domain" || value.Name.Space != "" {
					return false
				}
			}
			depth++
			if depth > 64 {
				return false
			}
			seen := map[xml.Name]bool{}
			for _, attr := range value.Attr {
				if seen[attr.Name] {
					return false
				}
				seen[attr.Name] = true
			}
		case xml.EndElement:
			depth--
		case xml.CharData:
			if depth == 0 && len(bytes.TrimSpace(value)) != 0 {
				return false
			}
		case xml.Directive:
			return false
		}
	}
}

func (c *vmCleanup) verifyDomain(ctx context.Context, record runRecord) (bool, error) {
	data, err := c.command(ctx, "dumpxml", record.RunID)
	if err != nil {
		return false, err
	}
	var domain domainIdentity
	if !validDomainXML(data) || xml.Unmarshal(data, &domain) != nil || len(domain.Names) != 1 || domain.Names[0] != record.Domain ||
		len(domain.UUIDs) != 1 || domain.UUIDs[0] != record.RunID ||
		len(domain.Descriptions) != 1 || domain.Descriptions[0] != domainOwner(record) || len(domain.Disks) != 2 {
		return false, errVMCleanup
	}
	directory := filepath.Join(c.store.root.Name(), record.RunID)
	disks := []string{filepath.Join(directory, "root.qcow2"), filepath.Join(directory, "seed.iso")}
	for _, disk := range domain.Disks {
		if disk.Type != "file" || !slices.Contains(disks, disk.Source.File) {
			return false, errVMCleanup
		}
		disks = slices.DeleteFunc(disks, func(path string) bool { return path == disk.Source.File })
	}
	if len(disks) != 0 || len(domain.NVRAM) > 1 {
		return false, errVMCleanup
	}
	if len(domain.NVRAM) == 1 && domain.NVRAM[0] != filepath.Join(directory, "nvram.fd") {
		return false, errVMCleanup
	}
	return len(domain.NVRAM) == 1, nil
}

func (c *vmCleanup) stopped(ctx context.Context) (bool, error) {
	data, err := c.command(ctx, "domstate", c.runID)
	if err != nil {
		return false, err
	}
	state := strings.TrimSpace(string(data))
	if state == "shut off" {
		return true, nil
	}
	if !slices.Contains([]string{"running", "paused", "idle", "in shutdown", "pmsuspended", "crashed"}, state) {
		return false, errVMCleanup
	}
	return false, nil
}

func (c *vmCleanup) waitStopped(ctx context.Context, limit time.Duration) (bool, error) {
	deadline := time.Now().Add(limit)
	for {
		stopped, err := c.stopped(ctx)
		if err != nil || stopped {
			return stopped, err
		}
		if !time.Now().Before(deadline) {
			return false, nil
		}
		select {
		case <-ctx.Done():
			return false, errVMCleanup
		case <-time.After(c.poll):
		}
	}
}

func (c *vmCleanup) stopDomain(ctx context.Context, record runRecord) error {
	nvram, err := c.verifyDomain(ctx, record)
	if err != nil {
		return err
	}
	stopped, err := c.stopped(ctx)
	if err != nil {
		return err
	}
	if !stopped {
		// An unsuccessful ACPI request is not proof of shutdown. Observe the
		// UUID, then recheck ownership before any bounded force-stop attempt.
		_, _ = c.command(ctx, "shutdown", record.RunID, "--mode", "acpi")
		stopped, err = c.waitStopped(ctx, c.grace)
		if err != nil {
			return err
		}
		if !stopped {
			if _, err := c.verifyDomain(ctx, record); err != nil {
				return err
			}
			if _, err := c.command(ctx, "destroy", record.RunID); err != nil {
				return err
			}
			stopped, err = c.waitStopped(ctx, 10*time.Second)
			if err != nil || !stopped {
				return errVMCleanup
			}
		}
	}
	// Undefining an active domain does not stop it. Never use undefine alone
	// as evidence that disks are safe to delete, or ask virsh to wipe storage.
	if _, err := c.verifyDomain(ctx, record); err != nil {
		return err
	}
	args := []string{"undefine", record.RunID}
	if nvram {
		args = append(args, "--nvram")
	}
	_, err = c.command(ctx, args...)
	return err
}

func (c *vmCleanup) verifyNoReferences(ctx context.Context, ids []string) error {
	persistent, err := c.domains(ctx, "--persistent")
	if err != nil {
		return err
	}
	for _, uuid := range persistent {
		if !slices.Contains(ids, uuid) {
			return errVMCleanup
		}
	}
	for _, uuid := range ids {
		if uuid == c.runID {
			return errVMCleanup
		}
		views := [][]string{{"dumpxml", uuid}}
		if slices.Contains(persistent, uuid) {
			views = append(views, []string{"dumpxml", uuid, "--inactive"})
		}
		for _, args := range views {
			data, err := c.command(ctx, args...)
			if err != nil {
				return err
			}
			if err := c.verifyXMLReferences(data); err != nil {
				return err
			}
		}
	}
	return nil
}

func (c *vmCleanup) verifyXMLReferences(data []byte) error {
	directory := filepath.Join(c.store.root.Name(), c.runID)
	if !validDomainXML(data) {
		return errVMCleanup
	}
	var domain domainIdentity
	if xml.Unmarshal(data, &domain) != nil {
		return errVMCleanup
	}
	for _, value := range domain.NVRAM {
		path := filepath.Clean(value)
		if path == directory || strings.HasPrefix(path, directory+string(filepath.Separator)) {
			return errVMCleanup
		}
	}
	decoder := xml.NewDecoder(bytes.NewReader(data))
	for {
		token, err := decoder.Token()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			return errVMCleanup
		}
		if content, ok := token.(xml.CharData); ok {
			path := filepath.Clean(strings.TrimSpace(string(content)))
			if path == directory || strings.HasPrefix(path, directory+string(filepath.Separator)) {
				return errVMCleanup
			}
		}
		if element, ok := token.(xml.StartElement); ok {
			for _, attr := range element.Attr {
				path := filepath.Clean(attr.Value)
				if path == directory || strings.HasPrefix(path, directory+string(filepath.Separator)) {
					return errVMCleanup
				}
			}
		}
	}
	return nil
}

var runArtifacts = []string{"root.qcow2", "seed.iso", "meta-data", "user-data", "nvram.fd", "network.xml", "filter.xml"}

func (c *vmCleanup) removeArtifacts() error {
	dir, err := c.store.root.Open(c.runID)
	if err != nil {
		return errVMCleanup
	}
	entries, readErr := dir.ReadDir(32)
	_ = dir.Close()
	if readErr != nil && !errors.Is(readErr, io.EOF) || len(entries) >= 32 {
		return errVMCleanup
	}
	for _, entry := range entries {
		if !slices.Contains(runArtifacts, entry.Name()) &&
			!slices.Contains([]string{"run.json", "running.json", "cleanup-pending.json", "cleaned.json", "released.json"}, entry.Name()) {
			return errVMCleanup
		}
	}
	for _, artifact := range runArtifacts {
		path := c.runID + "/" + artifact
		info, err := c.store.root.Lstat(path)
		if errors.Is(err, os.ErrNotExist) {
			continue
		}
		// libvirt creates NVRAM and can retain ownership of readonly seeds
		// after stop. Permit its QEMU UID only for these three exact disk paths,
		// after domain absence/reference checks; metadata remains Worker-only.
		disk := artifact == "root.qcow2" || artifact == "seed.iso" || artifact == "nvram.fd"
		if err != nil || (!privateOwned(info, false) && !(disk && privateHypervisorArtifact(info))) {
			return errVMCleanup
		}
		if err := c.store.root.Remove(path); err != nil {
			return errVMCleanup
		}
	}
	return c.store.syncDirectory(c.runID)
}

func (c *vmCleanup) Cleanup(ctx context.Context) error {
	run, err := c.store.Load(c.runID)
	if err != nil {
		return errVMCleanup
	}
	if run.Phase != "cleaned" && run.Phase != "released" {
		if _, err := c.store.Advance(c.runID, "cleanup-pending"); err != nil {
			return errVMCleanup
		}
	}
	ids, err := c.domains(ctx)
	if err != nil {
		return err
	}
	if slices.Contains(ids, c.runID) {
		if run.Phase == "cleaned" || run.Phase == "released" {
			return errVMCleanup
		}
		if err := c.stopDomain(ctx, run.Record); err != nil {
			return err
		}
	}
	ids, err = c.domains(ctx)
	if err != nil {
		return err
	}
	if err := c.verifyNoReferences(ctx, ids); err != nil {
		return err
	}
	if err := c.cleanupNetwork(ctx, run); err != nil {
		return err
	}
	if run.Phase == "cleaned" || run.Phase == "released" {
		for _, artifact := range runArtifacts {
			if _, err := c.store.root.Lstat(c.runID + "/" + artifact); !errors.Is(err, os.ErrNotExist) {
				return errVMCleanup
			}
		}
		return nil
	}
	if err := c.removeArtifacts(); err != nil {
		return errVMCleanup
	}
	_, err = c.store.Advance(c.runID, "cleaned")
	if err != nil {
		return errVMCleanup
	}
	return nil
}

func (c *vmCleanup) Released() error {
	_, err := c.store.Advance(c.runID, "released")
	if err != nil {
		return errVMCleanup
	}
	return nil
}
