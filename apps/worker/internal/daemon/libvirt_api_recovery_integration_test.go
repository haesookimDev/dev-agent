//go:build linux && libvirt_integration

package daemon

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"
)

// The controller supplies one newly isolated API/database and an individual
// Worker credential over stdin, never argv/logs. This fixture prepares a real
// running, networkless blank VM with production ownership records; a new real
// Worker process (without the run token) must reconcile it. It does not claim
// full LibvirtExecutor/guest OS/Runner acceptance or inject a real daemon crash.
func TestDedicatedLibvirtAPIRecovery(t *testing.T) {
	if os.Getenv("KELPIE_LIBVIRT_TEST_ACK") != "disposable-host-only" ||
		os.Getenv("KELPIE_LIBVIRT_API_TEST_ACK") != "isolated-api-only" {
		t.Skip("requires explicit disposable libvirt host and isolated API acknowledgements")
	}
	var input struct {
		ControlURL   string `json:"control_url"`
		WorkerName   string `json:"worker_name"`
		Credential   string `json:"credential"`
		WorkID       string `json:"work_id"`
		WorkerBinary string `json:"worker_binary"`
	}
	decoder := json.NewDecoder(io.LimitReader(os.Stdin, 8192))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&input) != nil || os.Geteuid() == 0 || !workUUID.MatchString(input.WorkID) {
		t.Fatal("invalid isolated recovery fixture")
	}
	endpoint, err := url.Parse(input.ControlURL)
	if err != nil || endpoint.Scheme != "http" || endpoint.Hostname() != "127.0.0.1" ||
		endpoint.Port() == "" || endpoint.User != nil || endpoint.Path != "" || endpoint.RawQuery != "" || endpoint.Fragment != "" {
		t.Fatal("test API must use a loopback-only endpoint")
	}
	info, err := os.Lstat(input.WorkerBinary)
	if err != nil || !filepath.IsAbs(input.WorkerBinary) || !info.Mode().IsRegular() || info.Mode().Perm()&0111 == 0 {
		t.Fatal("current Worker test binary unavailable")
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
	authRoot, err := os.MkdirTemp("/var/tmp", "kelpie-recovery-auth-")
	if err != nil {
		t.Fatal("private fixture credential directory unavailable")
	}
	credentialPath := filepath.Join(authRoot, "worker-token")
	defer func() {
		if err := os.Remove(credentialPath); err != nil && !errors.Is(err, os.ErrNotExist) {
			t.Error("could not remove owned fixture credential")
		}
		if err := os.Remove(authRoot); err != nil {
			t.Error("could not remove empty fixture credential directory")
		}
	}()
	if err := os.WriteFile(credentialPath, []byte(input.Credential), 0600); err != nil {
		t.Fatal("could not store private fixture credential")
	}
	input.Credential = ""
	root, err := os.MkdirTemp("/var/tmp", "kelpie-lifecycle-")
	if err != nil {
		t.Fatal(err)
	}
	store, err := openRunStore(root)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	config := Config{ControlURL: input.ControlURL, WorkerName: input.WorkerName, WorkerTokenFile: credentialPath,
		Executor: "libvirt", CPUTotal: 4, MemoryMBTotal: 8192, DiskGBTotal: 60, RunResources: testResources}
	client := NewClient(input.ControlURL, "")
	client.tokenFile = credentialPath
	worker, err := client.RegisterRecovery(ctx, config)
	if err != nil || worker.ActiveRuns != 0 {
		t.Fatal("isolated Worker could not register without existing reservations")
	}
	claim, err := client.Claim(ctx, worker.ID, testResources)
	if err != nil || claim == nil || claim.WorkItem.ID != input.WorkID || claim.WorkItem.Version != 2 {
		t.Fatal("isolated API did not issue the expected fresh claim")
	}
	owned, err := store.Create(claim.WorkItem.ID, claim.LeaseID, testResources)
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("preserved ownership journal: %s/%s", root, owned.Record.RunID)
	// Reopen only our recorded root after any child has stopped. Never remove
	// its journal or recursively erase a directory around a potentially live VM.
	defer func() {
		store.Close()
		recoveryStore, err := openRunStore(root)
		if err != nil {
			t.Error("fixture cleanup could not regain exclusive ownership")
			return
		}
		defer recoveryStore.Close()
		finish, stop := context.WithTimeout(context.Background(), time.Minute)
		defer stop()
		if err := newVMCleanup(recoveryStore, owned.Record.RunID).Cleanup(finish); err != nil {
			t.Error("owned VM cleanup unconfirmed; preserve its journal for recovery")
		}
	}()
	launchRecoveryBlankVM(t, ctx, store, owned)
	work, err := client.Transition(ctx, claim.WorkItem.ID, claim.LeaseToken, "failed", claim.WorkItem.Version, "Isolated recovery fixture finished")
	if err != nil || work.Status != "failed" || work.Version != 3 {
		t.Fatal("fixture could not reach terminal work through the real API")
	}
	claim.LeaseToken = "" // The new Worker receives only its individual credential file.
	store.Close()
	for attempt := 0; attempt < 2; attempt++ {
		runRecoveryWorkerProcess(t, ctx, input.WorkerBinary, config, root)
	}
	finalStore, err := openRunStore(root)
	if err != nil {
		t.Fatal("new Worker retained its ownership lock after shutdown")
	}
	defer finalStore.Close()
	final, err := finalStore.Load(owned.Record.RunID)
	if err != nil || final.Phase != "released" || final.Record != owned.Record {
		t.Fatal("new Worker did not persist the exact acknowledged lease identity")
	}
	snapshot, err := client.InspectLease(ctx, worker.ID, owned.Record.RunID)
	if err != nil || snapshot.State != "released" || snapshot.WorkStatus != "failed" || snapshot.WorkVersion != 3 {
		t.Fatal("real API did not retain the matching terminal release")
	}
	worker, err = client.RegisterRecovery(ctx, config)
	if err != nil || worker.ActiveRuns != 0 || worker.CPUAvailable != 4 || worker.MemoryMBAvailable != 8192 || worker.DiskGBAvailable != 60 {
		t.Fatal("real API capacity did not converge after Worker restart")
	}
	t.Log("real networkless KVM fixture -> new Worker process without run token -> owned cleanup -> real API ACK -> durable release -> second restart; DB audit checked by controller")
}

func launchRecoveryBlankVM(t *testing.T, ctx context.Context, store *runStore, owned ownedRun) {
	t.Helper()
	runDir := filepath.Join(store.root.Name(), owned.Record.RunID)
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
	labCommand(t, ctx, "virt-install", "--connect", "qemu:///system",
		"--name", owned.Record.Domain, "--uuid", owned.Record.RunID,
		"--metadata", "description="+domainOwner(owned.Record),
		"--virt-type", "kvm", "--memory", "512", "--vcpus", "1",
		"--import", "--noautoconsole", "--os-variant", "ubuntu24.04",
		"--boot", "uefi,nvram="+filepath.Join(runDir, "nvram.fd"),
		"--disk", filepath.Join(runDir, "root.qcow2")+",format=qcow2,bus=virtio",
		"--disk", filepath.Join(runDir, "seed.iso")+",device=cdrom,bus=scsi",
		"--network", "none", "--graphics", "none")
	if stopped, err := newVMCleanup(store, owned.Record.RunID).stopped(ctx); err != nil || stopped {
		t.Fatal("real KVM domain was not running")
	}
	if _, err := store.Advance(owned.Record.RunID, "running"); err != nil {
		t.Fatal(err)
	}
}

func runRecoveryWorkerProcess(t *testing.T, ctx context.Context, binary string, config Config, root string) {
	t.Helper()
	process := exec.CommandContext(ctx, binary)
	process.Env = []string{
		"PATH=/usr/sbin:/usr/bin:/sbin:/bin", "KELPIE_EXECUTOR=libvirt",
		"KELPIE_CONTROL_URL=" + config.ControlURL, "KELPIE_WORKER_NAME=" + config.WorkerName,
		"KELPIE_GUEST_CONTROL_URL=https://control.example.test", "KELPIE_GUEST_CONTROL_IPV4=192.0.2.7",
		"KELPIE_WORKER_TOKEN_FILE=" + config.WorkerTokenFile, "KELPIE_WORK_ROOT=" + root,
		"KELPIE_CPU_TOTAL=4", "KELPIE_MEMORY_MB_TOTAL=8192", "KELPIE_DISK_GB_TOTAL=60",
		"KELPIE_POLL_SECONDS=3600",
	}
	output, err := process.StdoutPipe()
	if err != nil {
		t.Fatal("Worker log pipe unavailable")
	}
	process.Stderr = io.Discard
	if err := process.Start(); err != nil {
		t.Fatal("could not start the owned Worker process")
	}
	done, ready := make(chan error, 1), make(chan struct{}, 1)
	go func() {
		scanner := bufio.NewScanner(output)
		scanner.Buffer(make([]byte, 4096), 4096)
		for scanner.Scan() {
			var record struct {
				Msg string `json:"msg"`
			}
			if json.Unmarshal(scanner.Bytes(), &record) == nil && record.Msg == "worker registered" {
				select {
				case ready <- struct{}{}:
				default:
				}
			}
		}
		done <- process.Wait()
	}()
	finished := false
	defer func() {
		if !finished {
			_ = process.Process.Kill()
			<-done
		}
	}()
	select {
	case <-ready:
	case <-done:
		finished = true
		t.Fatal("new Worker exited before verified admission")
	case <-ctx.Done():
		t.Fatal("new Worker did not complete bounded recovery")
	}
	if err := process.Process.Signal(syscall.SIGTERM); err != nil {
		t.Fatal("could not stop the owned Worker process")
	}
	select {
	case err := <-done:
		finished = true
		if err != nil {
			t.Fatal("owned Worker did not stop cleanly")
		}
	case <-time.After(10 * time.Second):
		t.Fatal("owned Worker shutdown exceeded its bound")
	}
}
