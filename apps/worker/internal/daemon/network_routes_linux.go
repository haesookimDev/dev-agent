package daemon

import (
	"context"
	"os/exec"
	"syscall"
	"time"
)

func queryHostIPv4Routes(ctx context.Context) ([]byte, error) {
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	command := exec.CommandContext(ctx, "/usr/sbin/ip", "-j", "-4", "route", "show", "table", "all")
	command.Env = []string{"PATH=/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL=C", "LANG=C"}
	command.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	command.Cancel = func() error { return syscall.Kill(-command.Process.Pid, syscall.SIGKILL) }
	command.WaitDelay = time.Second
	var output boundedVMOutput
	command.Stdout = &output
	if command.Run() != nil {
		return nil, errRunNetwork
	}
	return output.buffer.Bytes(), nil
}
