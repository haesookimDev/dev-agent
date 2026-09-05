package daemon

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

type Executor interface {
	Execute(context.Context, RunClient, Claim) error
}

// Executors receive work-scoped operations, including coordinated lease release.
type RunClient interface {
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
	config Config
	logger *slog.Logger
}

var safeID = regexp.MustCompile(`^[a-f0-9-]{36}$`)

func (e LibvirtExecutor) Execute(ctx context.Context, client RunClient, claim Claim) error {
	if !safeID.MatchString(claim.WorkItem.ID) {
		return errors.New("unsafe work item id")
	}
	if _, err := os.Stat(e.config.BaseImage); err != nil {
		return fmt.Errorf("base image unavailable: %w", err)
	}
	runDir := filepath.Join(e.config.WorkRoot, claim.WorkItem.ID)
	if err := os.MkdirAll(runDir, 0700); err != nil {
		return err
	}
	overlay := filepath.Join(runDir, "root.qcow2")
	seed := filepath.Join(runDir, "seed.iso")
	meta := filepath.Join(runDir, "meta-data")
	user := filepath.Join(runDir, "user-data")
	if err := run(ctx, "qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", e.config.BaseImage, overlay, fmt.Sprintf("%dG", e.config.RunResources.DiskGB)); err != nil {
		return err
	}
	if err := os.WriteFile(meta, []byte("instance-id: kelpie-"+claim.WorkItem.ID+"\nlocal-hostname: kelpie-run\n"), 0600); err != nil {
		return err
	}
	assignmentWork := claim.WorkItem
	assignmentWork.Status = "analyzing"
	assignmentWork.Version++
	assignment, err := json.Marshal(assignmentWork)
	if err != nil {
		return err
	}
	assignmentEncoded := base64.URLEncoding.EncodeToString(assignment)
	environment := fmt.Sprintf(
		"KELPIE_CONTROL_URL=%s\nKELPIE_LEASE_TOKEN=%s\nKELPIE_CORRELATION_ID=%s\nKELPIE_ASSIGNMENT=%s\nKELPIE_WORK_ROOT=/workspace\n",
		e.config.ControlURL, claim.LeaseToken, claim.WorkItem.CorrelationID, assignmentEncoded,
	)
	environmentEncoded := base64.StdEncoding.EncodeToString([]byte(environment))
	cloudConfig := fmt.Sprintf(`#cloud-config
ssh_pwauth: false
write_files:
  - path: /run/kelpie/assignment.env
    owner: kelpie:kelpie
    permissions: '0600'
    encoding: b64
    content: %s
runcmd:
  - [ systemctl, start, kelpie-runner.service ]
`, environmentEncoded)
	if err := os.WriteFile(user, []byte(cloudConfig), 0600); err != nil {
		return err
	}
	if err := run(ctx, "cloud-localds", seed, user, meta); err != nil {
		return err
	}
	name := "kelpie-" + claim.WorkItem.ID
	args := []string{
		"--connect", "qemu:///system", "--name", name,
		"--memory", fmt.Sprint(e.config.RunResources.MemoryMB), "--vcpus", fmt.Sprint(e.config.RunResources.CPU),
		"--import", "--noautoconsole", "--os-variant", "ubuntu24.04",
		"--disk", overlay + ",format=qcow2,bus=virtio", "--disk", seed + ",device=cdrom",
		"--network", "network=default,model=virtio", "--graphics", "vnc,listen=127.0.0.1",
	}
	if err := run(ctx, "virt-install", args...); err != nil {
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

func run(ctx context.Context, name string, args ...string) error {
	command := exec.CommandContext(ctx, name, args...)
	output, err := command.CombinedOutput()
	if err != nil {
		return fmt.Errorf("%s failed: %w: %s", name, err, strings.TrimSpace(string(output)))
	}
	return nil
}
