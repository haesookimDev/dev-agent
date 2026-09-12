package daemon

import (
	"context"
	"encoding/json"
	"math"
	"os"
	"os/exec"
	"syscall"
	"time"
)

// qemu-img create permits an explicit overlay size smaller than its backing
// image. That truncates the guest-visible disk, not the partition/filesystem.
// Refuse it before any owned resource is created; never silently over-reserve.
func validateBaseCapacity(ctx context.Context, path string, diskGB int) error {
	if diskGB < 1 || uint64(diskGB) > math.MaxInt64/(1<<30) {
		return diagnosticError{kind: vmImageCapacity}
	}
	ctx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	command := exec.CommandContext(ctx, "qemu-img", "info", "--output=json", "-f", "qcow2", path)
	command.Env = []string{"PATH=" + os.Getenv("PATH"), "LC_ALL=C", "LANG=C"}
	command.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	command.Cancel = func() error { return syscall.Kill(-command.Process.Pid, syscall.SIGKILL) }
	command.WaitDelay = time.Second
	var output boundedVMOutput
	command.Stdout = &output
	if err := command.Run(); err != nil {
		return privateFailure(err, vmBaseImage)
	}
	var info struct {
		Format string `json:"format"`
		Size   int64  `json:"virtual-size"`
	}
	if json.Unmarshal(output.buffer.Bytes(), &info) != nil || info.Format != "qcow2" || info.Size <= 0 {
		return diagnosticError{kind: vmBaseImage}
	}
	if info.Size > int64(diskGB)*(1<<30) {
		return diagnosticError{kind: vmImageCapacity}
	}
	return nil
}
