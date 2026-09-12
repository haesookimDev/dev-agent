package main

import (
	"context"
	"crypto/sha256"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"testing"
	"time"
)

func TestInstallerRequiresApprovedSHA256(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	command := exec.CommandContext(ctx, "/bin/bash", installerPath(t), "/not/a/reviewed/hook")
	command.Env = []string{"PATH=/usr/bin:/bin", "LANG=C", "LC_ALL=C"}
	output, err := command.CombinedOutput()
	if err == nil || !strings.Contains(string(output), "approved SHA256") {
		t.Fatal("installer accepted or failed to require an explicit approved digest")
	}
}

func TestInstallerSourceValidation(t *testing.T) {
	requireLinuxSourcePinning(t)
	root := sourcePinningRoot(t)
	owner := strconv.Itoa(os.Geteuid())
	approved := []byte("reviewed-network-hook\n")
	approvedHash := fmt.Sprintf("%x", sha256.Sum256(approved))

	t.Run("approved", func(t *testing.T) {
		source := writeSourceFixture(t, root, "approved/hook", approved, 0700)
		staged, err := stageSource(t, source, approvedHash, filepath.Join(root, "approved-stage"), owner)
		if err != nil {
			t.Fatal("approved private source was rejected")
		}
		assertApprovedStage(t, staged, approvedHash)
	})

	for _, test := range []struct {
		name  string
		build func(*testing.T) string
	}{
		{"source-symlink", func(t *testing.T) string {
			target := writeSourceFixture(t, root, "source-symlink/target", approved, 0700)
			link := filepath.Join(filepath.Dir(target), "hook")
			if err := os.Symlink(target, link); err != nil {
				t.Fatal(err)
			}
			return link
		}},
		{"parent-symlink", func(t *testing.T) string {
			target := writeSourceFixture(t, root, "parent-symlink/real/hook", approved, 0700)
			link := filepath.Join(root, "parent-symlink", "linked")
			if err := os.Symlink(filepath.Dir(target), link); err != nil {
				t.Fatal(err)
			}
			return filepath.Join(link, "hook")
		}},
		{"hardlink", func(t *testing.T) string {
			source := writeSourceFixture(t, root, "hardlink/hook", approved, 0700)
			if err := os.Link(source, source+".other"); err != nil {
				t.Fatal(err)
			}
			return source
		}},
		{"writable-source", func(t *testing.T) string {
			return writeSourceFixture(t, root, "writable-source/hook", approved, 0720)
		}},
		{"non-executable-source", func(t *testing.T) string {
			return writeSourceFixture(t, root, "non-executable-source/hook", approved, 0600)
		}},
		{"writable-parent", func(t *testing.T) string {
			source := writeSourceFixture(t, root, "writable-parent/hook", approved, 0700)
			if err := os.Chmod(filepath.Dir(source), 0720); err != nil {
				t.Fatal(err)
			}
			return source
		}},
	} {
		t.Run(test.name, func(t *testing.T) {
			source := test.build(t)
			private := "private-source-" + test.name
			output, err := stageSource(t, source, approvedHash, filepath.Join(root, "rejected-stage-"+test.name), owner)
			if err == nil {
				t.Fatal("unsafe source reached root-only staging")
			}
			if strings.Contains(output, source) || strings.Contains(output, private) || strings.Contains(output, approvedHash) {
				t.Fatal("source validation disclosed a private path or approved digest")
			}
		})
	}

	source := writeSourceFixture(t, root, "wrong-digest/hook", []byte("private-unreviewed-hook\n"), 0700)
	output, err := stageSource(t, source, approvedHash, filepath.Join(root, "wrong-digest-stage"), owner)
	if err == nil || strings.Contains(output, source) || strings.Contains(output, approvedHash) || strings.Contains(output, "private-unreviewed-hook") {
		t.Fatal("unapproved source was staged or its private diagnostic escaped")
	}
	foreignOwner := strconv.FormatUint(uint64(os.Geteuid())+1, 10)
	output, err = stageSource(t, source, fmt.Sprintf("%x", sha256.Sum256([]byte("private-unreviewed-hook\n"))), filepath.Join(root, "foreign-owner-stage"), foreignOwner)
	if err == nil || strings.Contains(output, source) {
		t.Fatal("source not owned by the trusted installer identity was staged")
	}
}

func TestInstallerSourceRacesNeverInstallUnapprovedBytes(t *testing.T) {
	requireLinuxSourcePinning(t)
	root := sourcePinningRoot(t)
	owner := strconv.Itoa(os.Geteuid())
	approved := []byte(strings.Repeat("approved-network-hook\n", 1<<17))
	unapproved := []byte(strings.Repeat("private-unreviewed-hook\n", 1<<17))
	approvedHash := fmt.Sprintf("%x", sha256.Sum256(approved))

	for _, race := range []struct {
		name string
		run  func(string, <-chan struct{})
	}{
		{"in-place", func(source string, stop <-chan struct{}) {
			for {
				select {
				case <-stop:
					return
				default:
					_ = os.WriteFile(source, unapproved, 0700)
					_ = os.WriteFile(source, approved, 0700)
				}
			}
		}},
		{"path-swap", func(source string, stop <-chan struct{}) {
			approvedPath, unapprovedPath := source+".approved", source+".unapproved"
			for {
				select {
				case <-stop:
					return
				default:
					_ = os.Rename(source, approvedPath)
					_ = os.Rename(unapprovedPath, source)
					_ = os.Rename(source, unapprovedPath)
					_ = os.Rename(approvedPath, source)
				}
			}
		}},
	} {
		t.Run(race.name, func(t *testing.T) {
			directory := filepath.Join(root, race.name)
			source := writeSourceFixture(t, root, race.name+"/hook", approved, 0700)
			if race.name == "path-swap" {
				writeSourceFixture(t, root, race.name+"/hook.unapproved", unapproved, 0700)
			}
			stop := make(chan struct{})
			done := make(chan struct{})
			go func() {
				defer close(done)
				race.run(source, stop)
			}()
			defer func() {
				close(stop)
				<-done
			}()
			for attempt := 0; attempt < 12; attempt++ {
				staged, err := stageSource(t, source, approvedHash, filepath.Join(directory, fmt.Sprintf("stage-%d", attempt)), owner)
				if err == nil {
					assertApprovedStage(t, staged, approvedHash)
				}
			}
		})
	}
}

func requireLinuxSourcePinning(t *testing.T) {
	t.Helper()
	if runtime.GOOS != "linux" {
		t.Skip("the production installer uses Linux coreutils metadata semantics")
	}
}

func sourcePinningRoot(t *testing.T) string {
	t.Helper()
	root, err := os.MkdirTemp(".", ".network-hook-source-test-")
	if err != nil {
		t.Fatal(err)
	}
	absolute, err := filepath.Abs(root)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.RemoveAll(absolute) })
	if err := os.Chmod(absolute, 0700); err != nil {
		t.Fatal(err)
	}
	return absolute
}

func writeSourceFixture(t *testing.T, root, name string, data []byte, mode os.FileMode) string {
	t.Helper()
	path := filepath.Join(root, name)
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, data, mode); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(path, mode); err != nil {
		t.Fatal(err)
	}
	return path
}

func stageSource(t *testing.T, source, approvedHash, stageParent, owner string) (string, error) {
	t.Helper()
	if err := os.MkdirAll(stageParent, 0700); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	command := exec.CommandContext(ctx, "/bin/bash", "-c", `
source "$1"
stage_hook_binary "$2" "$3" "$4" "$5"
`, "source-test", installerPath(t), source, approvedHash, stageParent, owner)
	command.Env = []string{"PATH=/usr/sbin:/usr/bin:/sbin:/bin", "LANG=C", "LC_ALL=C"}
	output, err := command.CombinedOutput()
	return strings.TrimSpace(string(output)), err
}

func assertApprovedStage(t *testing.T, staged, approvedHash string) {
	t.Helper()
	data, err := os.ReadFile(staged)
	if err != nil {
		t.Fatal("successful staging did not preserve an inspectable file")
	}
	if fmt.Sprintf("%x", sha256.Sum256(data)) != approvedHash {
		t.Fatal("successful staging returned bytes other than the approved digest")
	}
	info, err := os.Lstat(staged)
	if err != nil || !info.Mode().IsRegular() || info.Mode().Perm() != 0700 {
		t.Fatal("successful staging did not produce a private regular executable")
	}
}
