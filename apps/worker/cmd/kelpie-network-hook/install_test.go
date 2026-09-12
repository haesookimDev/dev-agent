package main

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func installerPath(t *testing.T) string {
	t.Helper()
	path, err := filepath.Abs("../../../../infra/host/install-network-hook.sh")
	if err != nil {
		t.Fatal(err)
	}
	return path
}

func TestInstallerRequiresExplicitAcknowledgement(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	command := exec.CommandContext(ctx, "/bin/bash", installerPath(t))
	command.Env = []string{"PATH=/usr/bin:/bin", "LANG=C", "LC_ALL=C"}
	output, err := command.CombinedOutput()
	if err == nil || !strings.Contains(string(output), "requires root, dedicated-idle-host-only acknowledgement") {
		t.Fatal("installer did not stop before host effects without explicit input")
	}
}

func TestInstallerIgnoresCallerSearchPath(t *testing.T) {
	directory := t.TempDir()
	marker := filepath.Join(directory, "unexpected-command")
	// No acknowledgement or arguments: even an actual root test process must
	// stop at the first gate without reaching any privileged host command.
	fake := "#!/bin/sh\n/bin/echo invoked > \"$KELPIE_INSTALL_TEST_MARKER\"\n/bin/echo 0\n"
	if err := os.WriteFile(filepath.Join(directory, "id"), []byte(fake), 0700); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	command := exec.CommandContext(ctx, "/bin/bash", installerPath(t))
	command.Env = []string{"PATH=" + directory, "KELPIE_INSTALL_TEST_MARKER=" + marker, "LANG=C", "LC_ALL=C"}
	if err := command.Run(); err == nil {
		t.Fatal("installer accepted missing explicit input")
	}
	if _, err := os.Lstat(marker); !os.IsNotExist(err) {
		t.Fatal("installer executed a caller-controlled command before validating authority")
	}
}
