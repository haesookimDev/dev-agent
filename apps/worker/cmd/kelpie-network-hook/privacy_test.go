package main

import (
	"context"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestHookProcessPrivateDiagnostics(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	binary := filepath.Join(t.TempDir(), "network-hook")
	if exec.CommandContext(ctx, "go", "build", "-o", binary, ".").Run() != nil {
		t.Fatal("hook binary build failed")
	}
	command := exec.CommandContext(ctx, binary, "synthetic-private-input")
	command.Env = []string{"PATH=/usr/bin:/bin", "SYNTHETIC_PRIVATE_VALUE=not-for-logs"}
	command.Stdin = strings.NewReader("synthetic-private-hook-xml")
	output, err := command.CombinedOutput()
	if err == nil || string(output) != "kelpie network logging is unconfirmed\n" {
		t.Fatal("hook failure exposed details or did not refuse invalid input")
	}
	command = exec.CommandContext(ctx, binary, "default", "start", "begin", "-")
	command.Env = []string{"PATH=/usr/bin:/bin"}
	command.Stdin = strings.NewReader("unrelated network")
	if output, err := command.CombinedOutput(); err != nil || len(output) != 0 {
		t.Fatal("hook intercepted an unrelated network")
	}
}
