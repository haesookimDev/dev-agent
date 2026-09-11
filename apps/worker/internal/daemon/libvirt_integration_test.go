//go:build linux && libvirt_integration

package daemon

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"sync/atomic"
	"syscall"
	"testing"
	"time"
)

// This opt-in test creates real, networkless KVM domains on an explicitly
// acknowledged disposable host. It never uses an existing image or domain and
// deliberately keeps the ownership journal, including on failure. Do not use
// t.TempDir: its recursive cleanup cannot prove a failed VM has stopped.
func TestDedicatedLibvirtCleanupBeforeRelease(t *testing.T) {
	if os.Getenv("KELPIE_LIBVIRT_TEST_ACK") != "disposable-host-only" {
		t.Skip("requires libvirt_integration tag and explicit disposable-host acknowledgement")
	}
	if os.Geteuid() == 0 {
		t.Fatal("run as the unprivileged Worker user with libvirt and kvm access")
	}
	if _, err := os.Stat("/dev/kvm"); err != nil {
		t.Fatal("native KVM is required")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
	defer cancel()
	data, err := queryVirsh(ctx, "list", "--all", "--uuid")
	if err != nil || len(strings.TrimSpace(string(data))) != 0 {
		t.Fatal("test requires a successful empty domain inventory")
	}
	for _, scenario := range []string{"terminal", "cancelled"} {
		if !t.Run(scenario, func(t *testing.T) { realLibvirtLifecycle(t, ctx, scenario) }) {
			break // Preserve a failed run; do not create more domains around it.
		}
	}
}

func realLibvirtLifecycle(t *testing.T, ctx context.Context, scenario string) {
	root, err := os.MkdirTemp("/var/tmp", "kelpie-lifecycle-")
	if err != nil {
		t.Fatal(err)
	}
	store, err := openRunStore(root)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	owned := createTestRun(t, store)
	t.Logf("preserved ownership journal: %s/%s", root, owned.Record.RunID)
	cleanup := newVMCleanup(store, owned.Record.RunID)
	var commands []string
	cleanup.query = func(ctx context.Context, args ...string) ([]byte, error) {
		commands = append(commands, args[0])
		return queryVirsh(ctx, args...)
	}
	defer func() {
		finish, cancel := context.WithTimeout(context.Background(), time.Minute)
		defer cancel()
		if err := cleanup.Cleanup(finish); err != nil {
			t.Errorf("owned cleanup unconfirmed; preserve the recorded run for investigation: %v", err)
		}
	}()
	runDir := filepath.Join(root, owned.Record.RunID)
	for name, body := range map[string]string{
		"meta-data": "instance-id: " + owned.Record.Domain + "\n",
		"user-data": "#cloud-config\n{}\n",
	} {
		if err := os.WriteFile(filepath.Join(runDir, name), []byte(body), 0600); err != nil {
			t.Fatal(err)
		}
	}
	labCommand(t, ctx, "qemu-img", "create", "-f", "qcow2", filepath.Join(runDir, "root.qcow2"), "64M")
	labCommand(t, ctx, "cloud-localds", filepath.Join(runDir, "seed.iso"), filepath.Join(runDir, "user-data"), filepath.Join(runDir, "meta-data"))
	for _, name := range []string{"root.qcow2", "seed.iso"} {
		if err := privateRunArtifact(store, owned.Record.RunID+"/"+name); err != nil {
			t.Fatal(err)
		}
	}
	if err := grantHypervisorSearch(store, owned.Record.RunID); err != nil {
		t.Fatal(err)
	}
	// A blank disk deliberately never acknowledges ACPI: exercise the bounded
	// force-stop path, not OS/Runner readiness or a normal guest shutdown claim.
	labCommand(t, ctx, "virt-install", "--connect", "qemu:///system",
		"--name", owned.Record.Domain, "--uuid", owned.Record.RunID,
		"--metadata", "description="+domainOwner(owned.Record),
		"--virt-type", "kvm", "--memory", "512", "--vcpus", "1",
		"--import", "--noautoconsole", "--os-variant", "ubuntu24.04",
		"--boot", "uefi,nvram="+filepath.Join(runDir, "nvram.fd"),
		"--disk", filepath.Join(runDir, "root.qcow2")+",format=qcow2,bus=virtio",
		"--disk", filepath.Join(runDir, "seed.iso")+",device=cdrom,bus=scsi",
		"--network", "none", "--graphics", "none")
	if stopped, err := cleanup.stopped(ctx); err != nil || stopped {
		t.Fatal("real KVM domain was not running")
	}
	if _, err := store.Advance(owned.Record.RunID, "running"); err != nil {
		t.Fatal(err)
	}
	var releases atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case strings.HasSuffix(r.URL.Path, "/release"):
			run, err := store.Load(owned.Record.RunID)
			ids, inventoryErr := cleanup.domains(r.Context())
			if err != nil || inventoryErr != nil || run.Phase != "cleaned" || slices.Contains(ids, owned.Record.RunID) {
				t.Error("API release preceded physical cleanup")
				w.WriteHeader(500)
				return
			}
			releases.Add(1)
			w.WriteHeader(http.StatusNoContent)
		case r.Method == http.MethodGet:
			_ = json.NewEncoder(w).Encode(WorkItem{ID: storeTestWork, Status: "failed", Version: 3})
		default:
			w.WriteHeader(http.StatusNoContent)
		}
	}))
	defer server.Close()
	d := resourceDaemon(server)
	d.tracker.Reserve(testResources)
	runCtx, stop := context.WithCancel(ctx)
	defer stop()
	d.executor = executorFunc(func(ctx context.Context, client RunClient, claim Claim) error {
		if err := client.BindResources(cleanup); err != nil {
			return err
		}
		if scenario == "cancelled" {
			stop()
			return ctx.Err()
		}
		return client.Release(ctx, claim.WorkItem.ID, claim.LeaseToken)
	})
	claim := resourceClaim()
	claim.WorkItem.ID = storeTestWork
	d.execute(runCtx, claim)
	if releases.Load() != 1 {
		t.Fatal("expected exactly one acknowledged API release")
	}
	assertResources(t, d, testResources, 0)
	for _, name := range runArtifacts {
		if _, err := os.Lstat(filepath.Join(runDir, name)); !errors.Is(err, os.ErrNotExist) {
			t.Fatal("owned artifact remains after release")
		}
	}
	for _, name := range []string{"shutdown", "destroy", "undefine"} {
		if !slices.Contains(commands, name) {
			t.Fatalf("real %s path was not exercised", name)
		}
	}
	run, err := store.Load(owned.Record.RunID)
	if err != nil || run.Phase != "released" {
		t.Fatal("physical cleanup/API acknowledgement journal is incomplete")
	}
	t.Log("real KVM running -> ACPI -> force-stop -> undefined -> artifacts absent -> API ACK -> capacity returned")
}

func labCommand(t *testing.T, ctx context.Context, name string, args ...string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(ctx, 45*time.Second)
	defer cancel()
	command := exec.CommandContext(ctx, name, args...)
	command.Env = []string{"PATH=/usr/sbin:/usr/bin:/sbin:/bin", "LANG=C.UTF-8"}
	command.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	command.Cancel = func() error { return syscall.Kill(-command.Process.Pid, syscall.SIGKILL) }
	command.WaitDelay = time.Second
	output, err := command.CombinedOutput()
	if err != nil {
		// Inputs are generated blank disks/cloud-init, never a real assignment.
		t.Fatalf("disposable-host %s failed: %v\n%s", name, err, output)
	}
}
