package daemon

import (
	"bytes"
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"
)

const storeTestWork = "33333333-3333-4333-8333-333333333333"

func newTestRunStore(t *testing.T) *runStore {
	t.Helper()
	store, err := openRunStore(filepath.Join(t.TempDir(), "runs"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(store.Close)
	return store
}

func createTestRun(t *testing.T, store *runStore) ownedRun {
	t.Helper()
	run, err := store.Create(storeTestWork, testResources)
	if err != nil {
		t.Fatal(err)
	}
	return run
}

func TestRunStoreDurableIdentityAndOrderedPhases(t *testing.T) {
	path := filepath.Join(t.TempDir(), "runs")
	store, err := openRunStore(path)
	if err != nil {
		t.Fatal(err)
	}
	run := createTestRun(t, store)
	if !runUUID.MatchString(run.Record.RunID) || run.Record.Domain != "kelpie-"+run.Record.RunID ||
		run.Record.WorkID != storeTestWork || run.Record.Resources != testResources || run.Phase != "prepared" {
		t.Fatal("ownership identity or initial state differs")
	}
	second := createTestRun(t, store)
	if second.Record.RunID == run.Record.RunID {
		t.Fatal("a retry reused a prior run identity")
	}
	for _, phase := range []string{"running", "cleanup-pending", "cleaned", "released"} {
		previous := run.At
		run, err = store.Advance(run.Record.RunID, phase)
		if err != nil || run.Phase != phase || run.At.Before(previous) {
			t.Fatalf("phase %s did not persist: %v", phase, err)
		}
		again, err := store.Advance(run.Record.RunID, phase)
		if err != nil || again != run {
			t.Fatal("repeating a completed phase must be idempotent")
		}
	}
	store.Close()
	store, err = openRunStore(path)
	if err != nil {
		t.Fatal("closed store could not be reopened")
	}
	defer store.Close()
	reloaded, err := store.Load(run.Record.RunID)
	if err != nil || reloaded != run {
		t.Fatal("identity or journal was lost on reopen")
	}
	runs, err := store.List()
	if err != nil || len(runs) != 2 {
		t.Fatal("recovery inventory lost an attempt")
	}
	data, err := os.ReadFile(filepath.Join(path, run.Record.RunID, "run.json"))
	if err != nil || bytes.Contains(data, []byte("lease")) || bytes.Contains(data, []byte("assignment")) {
		t.Fatal("ownership record must exclude credential/assignment fields")
	}
}

func TestRunStoreRejectsUnsafeRoot(t *testing.T) {
	for _, scenario := range []string{"relative", "root", "unclean", "symlink", "public", "regular"} {
		t.Run(scenario, func(t *testing.T) {
			parent := t.TempDir()
			path := filepath.Join(parent, "runs")
			switch scenario {
			case "relative":
				path = "relative-runs"
			case "root":
				path = string(filepath.Separator)
			case "unclean":
				path += "/../runs"
			case "symlink":
				if err := os.Symlink(parent, path); err != nil {
					t.Fatal(err)
				}
			case "public":
				if err := os.Mkdir(path, 0700); err != nil {
					t.Fatal(err)
				}
				if err := os.Chmod(path, 0770); err != nil {
					t.Fatal(err)
				}
			case "regular":
				if err := os.WriteFile(path, nil, 0600); err != nil {
					t.Fatal(err)
				}
			}
			if store, err := openRunStore(path); err == nil {
				store.Close()
				t.Fatal("unsafe root accepted")
			}
		})
	}
}

func TestRunStoreExclusiveProcessLock(t *testing.T) {
	if path := os.Getenv("KELPIE_TEST_LOCK_ROOT"); path != "" {
		store, err := openRunStore(path)
		if err == nil {
			store.Close()
			os.Exit(3)
		}
		os.Exit(0)
	}
	store := newTestRunStore(t)
	if duplicate, err := openRunStore(store.root.Name()); err == nil {
		duplicate.Close()
		t.Fatal("a second same-process owner acquired the root")
	}
	process := exec.Command(os.Args[0], "-test.run=^TestRunStoreExclusiveProcessLock$")
	process.Env = []string{"KELPIE_TEST_LOCK_ROOT=" + store.root.Name()}
	if err := process.Run(); err != nil {
		t.Fatal("separate process did not reject held ownership")
	}
}

func TestRunStoreRejectsInvalidIdentityResourcesAndTransitions(t *testing.T) {
	store := newTestRunStore(t)
	for _, id := range []string{"", "..", strings.Repeat("-", 36), strings.ToUpper(storeTestWork + "f"), "../" + storeTestWork} {
		if _, err := store.Create(id, testResources); err == nil {
			t.Fatal("unsafe work identity accepted")
		}
		if _, err := store.Load(id); err == nil {
			t.Fatal("unsafe run identity accepted")
		}
	}
	for _, resources := range []Resources{{}, {CPU: -1, MemoryMB: 1, DiskGB: 1}, {CPU: 1, DiskGB: 1}, {CPU: 1, MemoryMB: 1}} {
		if _, err := store.Create(storeTestWork, resources); err == nil {
			t.Fatal("invalid resources accepted")
		}
	}
	run := createTestRun(t, store)
	for _, phase := range []string{"cleaned", "released", "../outside"} {
		if _, err := store.Advance(run.Record.RunID, phase); err == nil {
			t.Fatal("phase skipped mandatory cleanup")
		}
	}
	for _, phase := range []string{"cleanup-pending", "cleaned", "released"} {
		if _, err := store.Advance(run.Record.RunID, phase); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := store.Advance(run.Record.RunID, "running"); err == nil {
		t.Fatal("released attempt restarted")
	}
}

func TestRunStoreRejectsCorruptOrUnownedRecords(t *testing.T) {
	for _, scenario := range []string{"truncated", "duplicate", "unknown", "mismatch", "oversize", "public", "symlink", "hardlink", "fifo", "directory", "missing", "skipped-phase"} {
		t.Run(scenario, func(t *testing.T) {
			store := newTestRunStore(t)
			run := createTestRun(t, store)
			path := filepath.Join(store.root.Name(), run.Record.RunID, "run.json")
			original, err := os.ReadFile(path)
			if err != nil {
				t.Fatal(err)
			}
			if err := os.Remove(path); err != nil {
				t.Fatal(err)
			}
			data := original
			switch scenario {
			case "truncated":
				data = []byte("{")
			case "duplicate":
				data = bytes.Replace(data, []byte(`"schema":1`), []byte(`"schema":1,"schema":1`), 1)
			case "unknown":
				data = bytes.Replace(data, []byte(`"schema":1`), []byte(`"secret":"test-only","schema":1`), 1)
			case "mismatch":
				run.Record.Domain = "other-domain"
				data, _ = json.Marshal(run.Record)
				data = append(data, '\n')
			case "oversize":
				data = bytes.Repeat([]byte("x"), maxRunRecord+1)
			case "symlink", "hardlink":
				outside := filepath.Join(t.TempDir(), "record")
				if err := os.WriteFile(outside, original, 0600); err != nil {
					t.Fatal(err)
				}
				if scenario == "symlink" {
					err = os.Symlink(outside, path)
				} else {
					err = os.Link(outside, path)
				}
				if err != nil {
					t.Fatal(err)
				}
			case "fifo":
				if err := syscall.Mkfifo(path, 0600); err != nil {
					t.Fatal(err)
				}
			case "directory":
				if err := os.Mkdir(path, 0700); err != nil {
					t.Fatal(err)
				}
			}
			if !strings.Contains("symlink hardlink fifo directory missing", scenario) {
				if err := os.WriteFile(path, data, 0600); err != nil {
					t.Fatal(err)
				}
			}
			if scenario == "public" {
				if err := os.Chmod(path, 0644); err != nil {
					t.Fatal(err)
				}
			}
			if scenario == "skipped-phase" {
				if err := store.writeExclusive(run.Record.RunID+"/cleaned.json", runPhase{RunID: run.Record.RunID, Phase: "cleaned", At: time.Now().UTC()}); err != nil {
					t.Fatal(err)
				}
			}
			if _, err := store.Load(run.Record.RunID); err == nil {
				t.Fatal("invalid ownership record accepted")
			}
			if _, err := store.List(); err == nil {
				t.Fatal("recovery ignored an invalid record")
			}
		})
	}
}

func TestRunStoreUnknownDirectoryBlocksRecoveryAndPreservesFiles(t *testing.T) {
	store := newTestRunStore(t)
	path := filepath.Join(store.root.Name(), "legacy-or-unowned")
	if err := os.Mkdir(path, 0700); err != nil {
		t.Fatal(err)
	}
	if _, err := store.List(); err == nil {
		t.Fatal("unknown resources were silently adopted or ignored")
	}
	if _, err := os.Stat(path); err != nil {
		t.Fatal("unowned directory was modified")
	}
}

func TestRunStoreExclusiveWritePreservesPriorRecord(t *testing.T) {
	store := newTestRunStore(t)
	run := createTestRun(t, store)
	name := run.Record.RunID + "/run.json"
	if err := store.writeExclusive(name, runRecord{}); !errors.Is(err, errRunStore) {
		t.Fatal("record overwrite was not rejected")
	}
	loaded, err := store.Load(run.Record.RunID)
	if err != nil || loaded != run {
		t.Fatal("existing record was replaced")
	}
}
