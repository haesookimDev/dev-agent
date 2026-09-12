package daemon

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"syscall"
	"time"
)

type Executor interface {
	Execute(context.Context, RunClient, Claim) error
}

// Physical cleanup precedes remote lease release; acknowledgement is recorded
// before the matching local capacity becomes available again.
type ResourceLifecycle interface {
	Cleanup(context.Context) error
	Released() error
}

// Executors receive work-scoped operations, including coordinated lease release.
type RunClient interface {
	BindResources(ResourceLifecycle) error
	Event(context.Context, string, string, AgentEvent) error
	Transition(context.Context, string, string, string, int, string) (WorkItem, error)
	ReadRun(context.Context, string, string) (WorkItem, error)
	Release(context.Context, string, string) error
}

type MockExecutor struct{}

func (MockExecutor) Execute(ctx context.Context, client RunClient, claim Claim) error {
	work := claim.WorkItem
	steps := []struct{ status, message string }{
		{"analyzing", "Analyzing requirements and repository"},
		{"implementing", "Implementing the planned change"},
		{"verifying", "Running tests and interactive verification"},
		{"awaiting_approval", "Verification passed; waiting for PR approval"},
	}
	for _, step := range steps {
		changed, err := client.Transition(ctx, work.ID, claim.LeaseToken, step.status, work.Version, step.message)
		if err != nil {
			return err
		}
		work = changed
		if err := client.Event(ctx, work.ID, claim.LeaseToken, AgentEvent{
			EventType: "mock.progress", Source: "mock-executor", Level: "info",
			Message: step.message, Payload: map[string]any{"status": step.status},
		}); err != nil {
			return err
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(250 * time.Millisecond):
		}
	}
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(2 * time.Second):
		}
		current, err := client.ReadRun(ctx, work.ID, claim.LeaseToken)
		if err != nil {
			return err
		}
		work = current
		if work.Status == "committing" {
			work, err = client.Transition(ctx, work.ID, claim.LeaseToken, "pr_created", work.Version, "Mock PR created")
			if err != nil {
				return err
			}
			work, err = client.Transition(ctx, work.ID, claim.LeaseToken, "completed", work.Version, "Mock delivery completed")
			if err != nil {
				return err
			}
			return client.Release(ctx, work.ID, claim.LeaseToken)
		}
		if work.Status == "implementing" {
			work, err = client.Transition(ctx, work.ID, claim.LeaseToken, "verifying", work.Version, "Mock feedback verification")
			if err != nil {
				return err
			}
			work, err = client.Transition(ctx, work.ID, claim.LeaseToken, "awaiting_approval", work.Version, "Mock revision ready for approval")
			if err != nil {
				return err
			}
		}
		if work.Status == "cancelled" || work.Status == "failed" || work.Status == "completed" {
			return client.Release(ctx, work.ID, claim.LeaseToken)
		}
	}
}

type LibvirtExecutor struct {
	config         Config
	logger         *slog.Logger
	store          *runStore
	prepareAccess  func(*runStore, string) error       // nil uses the real Linux permission boundary.
	networkReader  *networkInventoryReader             // nil uses the real host inventory.
	prepareNetwork func(context.Context, string) error // nil provisions the recorded network.
}

func (e LibvirtExecutor) Execute(ctx context.Context, client RunClient, claim Claim) error {
	if !workUUID.MatchString(claim.WorkItem.ID) {
		return errors.New("unsafe work item id")
	}
	if _, err := os.Stat(e.config.BaseImage); err != nil {
		return privateFailure(err, vmBaseImage)
	}
	if e.store == nil {
		return errRunStore
	}
	control, err := parseGuestControl(e.config.GuestControlURL, e.config.GuestControlIPv4)
	if err != nil {
		return err
	}
	// Validate the backing image without changing it before creating resources.
	if err := run(ctx, "qemu-img", "check", "-f", "qcow2", e.config.BaseImage); err != nil {
		return err
	}
	reader := hostNetworkReader()
	if e.networkReader != nil {
		reader = *e.networkReader
	}
	inventory, err := reader.read(ctx)
	if err != nil {
		return err
	}
	owned, err := e.store.CreateControlled(claim.WorkItem.ID, claim.LeaseID, e.config.RunResources, e.config.NetworkPool, inventory.excluded, control)
	if err != nil {
		return err
	}
	if err := client.BindResources(newVMCleanup(e.store, owned.Record.RunID)); err != nil {
		return err
	}
	if !inventory.available(*owned.Record.Network) {
		return errRunNetwork
	}
	runDir := filepath.Join(e.store.root.Name(), owned.Record.RunID)
	boot, err := libvirtBootArguments(runtime.GOARCH, runDir)
	if err != nil {
		return err
	}
	overlay := filepath.Join(runDir, "root.qcow2")
	seed := filepath.Join(runDir, "seed.iso")
	meta := filepath.Join(runDir, "meta-data")
	user := filepath.Join(runDir, "user-data")
	netConfig := filepath.Join(runDir, "network-config")
	if err := run(ctx, "qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", e.config.BaseImage, overlay, fmt.Sprintf("%dG", e.config.RunResources.DiskGB)); err != nil {
		return err
	}
	if err := privateRunArtifact(e.store, owned.Record.RunID+"/root.qcow2"); err != nil {
		return err
	}
	if err := os.WriteFile(meta, []byte("instance-id: "+owned.Record.Domain+"\nlocal-hostname: kelpie-run\n"), 0600); err != nil {
		return privateFailure(err, vmSeedData)
	}
	cloudConfig, err := guestUserData(control, claim)
	if err != nil {
		return err
	}
	if err := os.WriteFile(user, cloudConfig, 0600); err != nil {
		return privateFailure(err, vmSeedData)
	}
	netBody, err := owned.Record.Network.guestNetworkConfig()
	if err != nil {
		return err
	}
	if err := os.WriteFile(netConfig, netBody, 0600); err != nil {
		return privateFailure(err, vmSeedData)
	}
	if err := run(ctx, "cloud-localds", "--network-config", netConfig, seed, user, meta); err != nil {
		return err
	}
	if err := privateRunArtifact(e.store, owned.Record.RunID+"/seed.iso"); err != nil {
		return err
	}
	prepareAccess := e.prepareAccess
	if prepareAccess == nil {
		prepareAccess = grantHypervisorSearch
	}
	if err := prepareAccess(e.store, owned.Record.RunID); err != nil {
		return err
	}
	prepareNetwork := e.prepareNetwork
	if prepareNetwork == nil {
		prepareNetwork = (networkProvisioner{store: e.store, reader: reader}).create
	}
	if err := prepareNetwork(ctx, owned.Record.RunID); err != nil {
		return err
	}
	name := owned.Record.Domain
	seedDisk := seed + ",device=cdrom"
	if runtime.GOARCH == "arm64" {
		seedDisk += ",bus=scsi"
	}
	args := []string{
		"--connect", "qemu:///system", "--name", name, "--uuid", owned.Record.RunID,
		"--metadata", "description=" + domainOwner(owned.Record),
		"--virt-type", "kvm",
		"--memory", fmt.Sprint(e.config.RunResources.MemoryMB), "--vcpus", fmt.Sprint(e.config.RunResources.CPU),
		"--import", "--noautoconsole", "--os-variant", "ubuntu24.04",
		"--disk", overlay + ",format=qcow2,bus=virtio", "--disk", seedDisk,
		"--graphics", "vnc,listen=127.0.0.1",
	}
	networkArgs, err := libvirtOwnedDeviceArguments(*owned.Record.Network, e.config.BaseImage)
	if err != nil {
		return err
	}
	args = append(args, networkArgs...)
	args = append(args, boot...)
	if err := run(ctx, "virt-install", args...); err != nil {
		return err
	}
	if _, err := e.store.Advance(owned.Record.RunID, "running"); err != nil {
		return err
	}
	work, err := client.Transition(ctx, claim.WorkItem.ID, claim.LeaseToken, "analyzing", claim.WorkItem.Version, "KVM VM provisioned")
	if err != nil {
		return err
	}
	e.logger.Info(
		"vm provisioned",
		"work_id", work.ID,
		"correlation_id", work.CorrelationID,
		"domain", name,
	)
	if err := client.Event(ctx, work.ID, claim.LeaseToken, AgentEvent{
		EventType: "vm.provisioned", Source: "libvirt", Level: "info", Message: "VM is ready",
		Payload: map[string]any{"domain": name, "run_dir": runDir},
	}); err != nil {
		return err
	}
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(5 * time.Second):
		}
		current, err := client.ReadRun(ctx, work.ID, claim.LeaseToken)
		if err != nil {
			return err
		}
		if current.Status == "completed" || current.Status == "failed" || current.Status == "cancelled" {
			return client.Release(ctx, work.ID, claim.LeaseToken)
		}
	}
}

func libvirtBootArguments(architecture, runDir string) ([]string, error) {
	switch architecture {
	case "arm64":
		// Match the ARM Golden Image's UEFI boot contract. The trusted host
		// selects read-only firmware; variables belong to this recorded run.
		return []string{"--boot", "uefi,nvram=" + filepath.Join(runDir, "nvram.fd")}, nil
	case "amd64":
		return nil, nil // Preserve the existing image/host boot selection.
	default:
		return nil, errors.New("unsupported VM host architecture")
	}
}

func run(ctx context.Context, name string, args ...string) error {
	return runWithLimit(ctx, 45*time.Second, name, args...)
}

func runWithLimit(ctx context.Context, limit time.Duration, name string, args ...string) error {
	ctx, cancel := context.WithTimeout(ctx, limit)
	defer cancel()
	command := exec.CommandContext(ctx, name, args...)
	// Stop ordinary helper descendants as well as their parent. The actual VM
	// belongs to libvirt and must still go through the owned physical cleanup.
	command.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	command.Cancel = func() error { return syscall.Kill(-command.Process.Pid, syscall.SIGKILL) }
	command.WaitDelay = time.Second
	// Nil stdout/stderr go to the null device: never buffer or retain potentially
	// sensitive command output. Exit status remains available for diagnosis.
	if err := command.Run(); err != nil {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		var exit *exec.ExitError
		if errors.As(err, &exit) {
			return diagnosticError{kind: vmCommandExit, code: exit.ExitCode()}
		}
		return privateFailure(err, vmCommandStart)
	}
	return nil
}
