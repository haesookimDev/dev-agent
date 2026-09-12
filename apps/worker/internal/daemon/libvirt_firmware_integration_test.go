//go:build linux && libvirt_integration

package daemon

import (
	"context"
	"crypto/rand"
	"encoding/json"
	"encoding/xml"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

// Exercise production image/seed preparation, virt-install boot arguments and
// cleanup, including its real owned network. NIC/graphics attachment is
// replaced with "none" for this blank fixture. HTTP is synthetic;
// this is NOT Golden Image/Runner/guest OS or production-network acceptance.
func TestDedicatedLibvirtExecutorFirmware(t *testing.T) {
	if os.Getenv("KELPIE_LIBVIRT_TEST_ACK") != "disposable-host-only" {
		t.Skip("requires explicit disposable-host acknowledgement")
	}
	if runtime.GOARCH != "arm64" {
		t.Skip("the dedicated UEFI fixture targets the ARM64 image boot contract")
	}
	if os.Geteuid() == 0 {
		t.Fatal("run as the unprivileged Worker user")
	}
	if _, err := os.Stat("/dev/kvm"); err != nil {
		t.Fatal("native KVM is required")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Minute)
	defer cancel()
	data, err := queryVirsh(ctx, "list", "--all", "--uuid")
	if err != nil || strings.TrimSpace(string(data)) != "" {
		t.Fatal("test requires a successful empty domain inventory")
	}
	tools := t.TempDir() // No VM artifact is placed in this recursively cleaned directory.
	shim := `#!/usr/bin/python3
import os
import subprocess
import sys
import time
args = sys.argv[1:]
run_id = args[args.index("--uuid") + 1]
network = args[args.index("--network") + 1]
assert network.startswith("network=kelpie-net-" + run_id + ",model=virtio,mac=")
assert network.endswith(",filterref.filter=kelpie-filter-" + run_id + ",trustGuestRxFilters=no")
for option, expected in (("--network", network), ("--graphics", "vnc,listen=127.0.0.1")):
    if args.count(option) != 1:
        sys.exit(91)
    index = args.index(option) + 1
    if index >= len(args) or args[index] != expected:
        sys.exit(92)
    args[index] = "none"
# The no-NIC fixture must not recreate an interface through XML parameters.
# Keep the production backing-image protection and every other device setting.
args = [value for index, value in enumerate(args)
        if not (value == "--xml" and args[index + 1].startswith("./devices/interface"))
        and not (index > 0 and args[index - 1] == "--xml" and value.startswith("./devices/interface"))]
command = ["/usr/bin/virt-install", *args]
environment = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}
ready = os.environ.get("KELPIE_TEST_LAUNCH_READY")
if ready:
    result = subprocess.run(command, env=environment)
    if result.returncode:
        sys.exit(result.returncode)
    descriptor = os.open(ready, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    time.sleep(120)
else:
    os.execve(command[0], command, environment)
`
	if err := os.WriteFile(filepath.Join(tools, "virt-install"), []byte(shim), 0700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", tools+":/usr/sbin:/usr/bin:/sbin:/bin")
	for _, scenario := range []string{"terminal", "transition-rejected", "launch-cancelled"} {
		if !t.Run(scenario, func(t *testing.T) { realExecutorFirmware(t, ctx, scenario) }) {
			break
		}
	}
}

func realExecutorFirmware(t *testing.T, ctx context.Context, scenario string) {
	root, err := os.MkdirTemp("/var/tmp", "kelpie-firmware-")
	if err != nil {
		t.Fatal(err)
	}
	store, err := openRunStore(root)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	baseDir, err := os.MkdirTemp("/var/tmp", "kelpie-firmware-base-")
	if err != nil {
		t.Fatal(err)
	}
	baseRoot, err := os.OpenRoot(baseDir)
	if err != nil {
		t.Fatal(err)
	}
	defer baseRoot.Close()
	uid, ok := hypervisorUID()
	if !ok || grantDirectorySearch(baseRoot, ".", uid) != nil {
		t.Fatal("fixture base directory could not grant exact hypervisor search")
	}
	basePath := filepath.Join(baseDir, "base.qcow2")
	if err := os.WriteFile(basePath, nil, 0600); err != nil {
		t.Fatal(err)
	}
	labCommand(t, ctx, "qemu-img", "create", "-f", "qcow2", basePath, "64M")
	baseFile, err := os.Open(basePath)
	if err != nil {
		t.Fatal(err)
	}
	defer baseFile.Close()
	baseInfo, err := baseFile.Stat()
	if err != nil || !privateOwned(baseInfo, false) {
		t.Fatal("fixture base is not private")
	}
	baseACL := "user::rw-,user:" + uid + ":r--,group::---,mask::r--,other::---"
	if _, err := aclCommand(baseFile, "setfacl", "--set="+baseACL); err != nil || baseFile.Sync() != nil {
		t.Fatal("fixture QEMU read grant failed")
	}
	claim := resourceClaim()
	claim.WorkItem = WorkItem{ID: storeTestWork, Status: "provisioning", Version: 2}
	var id [16]byte
	if _, err := rand.Read(id[:]); err != nil {
		t.Fatal(err)
	}
	id[6], id[8] = id[6]&0x0f|0x40, id[8]&0x3f|0x80
	claim.LeaseID = fmt.Sprintf("%x-%x-%x-%x-%x", id[:4], id[4:6], id[6:8], id[8:10], id[10:])
	cleanup := newVMCleanup(store, claim.LeaseID)
	t.Logf("preserved ownership journal: %s/%s", root, claim.LeaseID)
	defer func() {
		finish, cancel := context.WithTimeout(context.Background(), time.Minute)
		defer cancel()
		if err := cleanup.Cleanup(finish); err != nil {
			t.Error("cleanup unconfirmed; preserve owned blank base and journal")
			return
		}
		ids, err := cleanup.domains(finish)
		if err != nil || len(ids) != 0 {
			t.Error("domain inventory no longer empty; preserve the fixture base")
			return
		}
		info, err := os.Lstat(basePath)
		acl, aclErr := readDirectoryACL(baseFile)
		if err != nil || !os.SameFile(baseInfo, info) || aclErr != nil || strings.ReplaceAll(strings.TrimSpace(string(acl)), "\n", ",") != baseACL {
			t.Error("fixture base identity or read-only ACL changed; preserve it")
			return
		}
		if _, err := aclCommand(baseFile, "setfacl", "--set=user::rw-,group::---,other::---"); err != nil || baseFile.Sync() != nil {
			t.Error("base ACL restoration failed")
			return
		}
		info, err = os.Lstat(basePath)
		if err != nil || !privateOwned(info, false) || baseRoot.Remove("base.qcow2") != nil {
			t.Error("could not remove the exact private fixture base")
			return
		}
		if os.Remove(baseDir) != nil {
			t.Error("could not remove the empty fixture base directory")
		}
	}()
	var running, provisioned, releases, failures atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case strings.HasSuffix(r.URL.Path, "/transition"):
			run, err := store.Load(claim.LeaseID)
			nvram, identityErr := cleanup.verifyDomain(r.Context(), run.Record)
			stopped, stateErr := cleanup.stopped(r.Context())
			data, xmlErr := queryVirsh(r.Context(), "dumpxml", claim.LeaseID)
			var domain struct {
				Type   string `xml:"type,attr"`
				Loader struct {
					ReadOnly string `xml:"readonly,attr"`
					Type     string `xml:"type,attr"`
				} `xml:"os>loader"`
				Interfaces []struct{} `xml:"devices>interface"`
			}
			if err != nil || run.Phase != "running" || identityErr != nil || !nvram || stateErr != nil || stopped ||
				xmlErr != nil || xml.Unmarshal(data, &domain) != nil || domain.Type != "kvm" ||
				domain.Loader.ReadOnly != "yes" || domain.Loader.Type != "pflash" || len(domain.Interfaces) != 0 {
				t.Error("executor did not create a running KVM with owned NVRAM/read-only firmware and no NIC")
				w.WriteHeader(http.StatusConflict)
				return
			}
			running.Add(1)
			if scenario == "transition-rejected" {
				w.WriteHeader(http.StatusConflict)
				return
			}
			_ = json.NewEncoder(w).Encode(WorkItem{ID: storeTestWork, Status: "analyzing", Version: 3})
		case strings.HasSuffix(r.URL.Path, "/events"):
			var event AgentEvent
			if json.NewDecoder(r.Body).Decode(&event) != nil {
				t.Error("invalid event")
			}
			if event.EventType == "vm.provisioned" {
				provisioned.Add(1)
			}
			if event.EventType == "worker.failed" {
				failures.Add(1)
			}
			w.WriteHeader(http.StatusNoContent)
		case strings.HasSuffix(r.URL.Path, "/release"):
			run, err := store.Load(claim.LeaseID)
			ids, inventoryErr := cleanup.domains(r.Context())
			if err != nil || run.Phase != "cleaned" || inventoryErr != nil || len(ids) != 0 {
				t.Error("API release preceded VM/NVRAM cleanup")
				w.WriteHeader(http.StatusConflict)
				return
			}
			releases.Add(1)
			w.WriteHeader(http.StatusNoContent)
		case r.Method == http.MethodGet:
			_ = json.NewEncoder(w).Encode(WorkItem{ID: storeTestWork, Status: "failed", Version: 4})
		default:
			t.Error("unexpected API request")
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()
	d := resourceDaemon(server)
	d.config.BaseImage, d.config.WorkRoot, d.config.ControlURL = basePath, root, server.URL
	d.config.GuestControlURL, d.config.GuestControlIPv4 = "https://control.example.test", "192.0.2.7"
	d.config.NetworkPool = "10.240.0.0/24"
	d.config.RunResources = Resources{CPU: 1, MemoryMB: 512, DiskGB: 1}
	d.tracker = NewTracker(d.config.RunResources)
	d.tracker.Reserve(d.config.RunResources)
	d.executor = LibvirtExecutor{config: d.config, logger: d.logger, store: store}
	if scenario == "launch-cancelled" {
		ready := filepath.Join(t.TempDir(), "launch-ready") // No VM artifact.
		t.Setenv("KELPIE_TEST_LAUNCH_READY", ready)
		runCtx, stop := context.WithCancel(ctx)
		done := make(chan struct{})
		go func() { d.execute(runCtx, claim); close(done) }()
		deadline := time.Now().Add(15 * time.Second)
		observed := false
		for time.Now().Before(deadline) {
			if _, err := os.Stat(ready); err == nil {
				run, err := store.Load(claim.LeaseID)
				nvram, identityErr := cleanup.verifyDomain(ctx, run.Record)
				stopped, stateErr := cleanup.stopped(ctx)
				observed = err == nil && run.Phase == "prepared" && identityErr == nil && nvram && stateErr == nil && !stopped
				break
			}
			select {
			case <-done:
				deadline = time.Now()
			case <-time.After(10 * time.Millisecond):
			}
		}
		stop()
		<-done // Join bounded physical cleanup before closing the store/server.
		if !observed {
			t.Fatal("did not observe owned running VM while the launch command was still pending")
		}
	} else {
		d.execute(ctx, claim)
	}
	wantRunning, wantProvisioned, wantFailures := int32(1), int32(1), int32(0)
	if scenario == "transition-rejected" {
		wantProvisioned, wantFailures = 0, 1
	}
	if scenario == "launch-cancelled" {
		wantRunning, wantProvisioned, wantFailures = 0, 0, 1
	}
	if running.Load() != wantRunning || provisioned.Load() != wantProvisioned || failures.Load() != wantFailures || releases.Load() != 1 {
		t.Fatalf("unexpected lifecycle counts: running=%d provisioned=%d failures=%d releases=%d", running.Load(), provisioned.Load(), failures.Load(), releases.Load())
	}
	run, err := store.Load(claim.LeaseID)
	if err != nil || run.Phase != "released" {
		t.Fatal("release was not durable")
	}
	for _, name := range runArtifacts {
		if _, err := os.Lstat(filepath.Join(root, claim.LeaseID, name)); !errors.Is(err, os.ErrNotExist) {
			t.Fatal("owned artifact remains after release")
		}
	}
	assertResources(t, d, d.config.RunResources, 0)
	t.Log("production Executor -> real KVM/owned UEFI -> physical cleanup -> synthetic API ACK -> durable release")
}
