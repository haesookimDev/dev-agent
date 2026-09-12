package daemon

import (
	"context"
	"os"
	"os/exec"
	"syscall"
	"time"
)

func networkJournalReady(ctx context.Context) error {
	info, err := os.Lstat("/run/systemd/journal/socket")
	if err != nil || info.Mode()&os.ModeSocket == 0 {
		return errRunNetwork
	}
	ctx, cancel := context.WithTimeout(ctx, time.Second)
	defer cancel()
	command := exec.CommandContext(ctx, "/usr/bin/systemctl", "is-active", "--quiet", "systemd-journald.service")
	command.Env = []string{"PATH=/usr/bin:/bin", "LANG=C", "LC_ALL=C"}
	command.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	command.Cancel = func() error { return syscall.Kill(-command.Process.Pid, syscall.SIGKILL) }
	command.WaitDelay = time.Second
	if command.Run() != nil {
		return errRunNetwork
	}
	return nil
}
