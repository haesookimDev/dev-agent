//go:build linux && libvirt_integration

package daemon

import (
	"context"
	"os"
	"os/exec"
	"strings"
	"syscall"
	"testing"
	"time"
)

func TestDedicatedLibvirtNetworkProvisioning(t *testing.T) {
	if os.Getenv("KELPIE_LIBVIRT_TEST_ACK") != "disposable-host-only" {
		t.Skip("requires explicit disposable-host acknowledgement")
	}
	if os.Geteuid() == 0 {
		t.Fatal("run as the unprivileged Worker user")
	}
	for _, interrupted := range []bool{false, true} {
		name := "normal"
		if interrupted {
			name = "cancel-after-activation"
		}
		if !t.Run(name, func(t *testing.T) { realNetworkProvision(t, interrupted) }) {
			break
		}
	}
}

func realNetworkProvision(t *testing.T, interrupted bool) {
	check, done := context.WithTimeout(context.Background(), 10*time.Second)
	defer done()
	domains, err := queryVirsh(check, "list", "--all", "--uuid")
	if err != nil || strings.TrimSpace(string(domains)) != "" {
		t.Fatal("requires an empty dedicated-host domain inventory")
	}
	before, err := queryVirsh(check, "net-list", "--all", "--uuid")
	if err != nil {
		t.Fatal(err)
	}
	root, err := os.MkdirTemp("/var/tmp", "kelpie-network-")
	if err != nil {
		t.Fatal(err)
	}
	store, err := openRunStore(root)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	data, err := os.ReadFile("/proc/sys/kernel/random/uuid")
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
	defer cancel()
	reader := hostNetworkReader()
	inventory, err := reader.read(ctx)
	if err != nil {
		t.Fatal(err)
	}
	run, err := store.CreateNetworked(storeTestWork, strings.TrimSpace(string(data)), testResources, "10.240.0.0/24", inventory.excluded)
	if err != nil || !inventory.available(*run.Record.Network) {
		t.Fatal("could not reserve an available network")
	}
	t.Logf("preserved provisioned network journal: %s/%s", root, run.Record.RunID)
	defer func() {
		// A failed child recovery must not leave a disposable active network.
		// Reopen only this exact retained journal, never scan host resources.
		store.Close()
		reopened, err := openRunStore(root)
		if err != nil {
			t.Errorf("retained network journal could not reopen: %v", err)
			return
		}
		defer reopened.Close()
		finish, stop := context.WithTimeout(context.Background(), time.Minute)
		defer stop()
		if err := newVMCleanup(reopened, run.Record.RunID).Cleanup(finish); err != nil {
			t.Errorf("owned network cleanup unconfirmed; journal retained: %v", err)
		}
	}()
	activated := false
	query := reader.query
	reader.query = func(ctx context.Context, args ...string) ([]byte, error) {
		data, err := query(ctx, args...)
		if args[0] == "net-start" && err == nil {
			activated = true
			if interrupted {
				cancel() // Only after the real libvirt operation succeeds.
			}
		}
		return data, err
	}
	p := networkProvisioner{store: store, reader: reader}
	err = p.create(ctx, run.Record.RunID)
	if !activated || interrupted && err == nil || !interrupted && err != nil {
		t.Fatalf("unexpected provisioning result: %v", err)
	}
	// No VM/guest is attached. A separate OS process performs physical cleanup
	// and a synthetic local release, not API/PostgreSQL reconciliation.
	store.Close()
	binary, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	finish, stop := context.WithTimeout(context.Background(), time.Minute)
	defer stop()
	command := exec.CommandContext(finish, binary, "-test.run", "^TestDedicatedLibvirtNetworkRecovery$", "-test.v")
	command.Env = []string{"PATH=/usr/sbin:/usr/bin:/sbin:/bin", "LANG=C", "LC_ALL=C", "KELPIE_LIBVIRT_TEST_ACK=disposable-host-only", "KELPIE_LIBVIRT_NETWORK_RECOVERY_ROOT=" + root}
	command.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	command.Cancel = func() error { return syscall.Kill(-command.Process.Pid, syscall.SIGKILL) }
	command.WaitDelay = time.Second
	var output boundedVMOutput
	command.Stdout = &output
	if command.Run() != nil {
		t.Fatal("new-process network recovery failed; exact journal retained")
	}
	t.Log(strings.TrimSpace(output.buffer.String()))
	reopened, err := openRunStore(root)
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	final, err := reopened.Load(run.Record.RunID)
	if err != nil || final.Phase != "released" {
		t.Fatal("new process did not acknowledge durable physical cleanup")
	}
	after, err := queryVirsh(finish, "net-list", "--all", "--uuid")
	if err != nil || string(before) != string(after) {
		t.Fatal("network provisioning/recovery changed pre-existing inventory")
	}
	t.Log("production network provisioner verified; new process confirmed network/filter/bridge/XML cleanup")
}
