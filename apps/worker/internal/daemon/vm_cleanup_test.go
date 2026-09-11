package daemon

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"
)

type cleanupFixture struct {
	cleanup                            *vmCleanup
	run                                ownedRun
	exists, running, force, refuseStop bool
	foreign, refuse, extraXML          string
	calls                              []string
}

func newCleanupFixture(t *testing.T) *cleanupFixture {
	t.Helper()
	store := newTestRunStore(t)
	run := createTestRun(t, store)
	f := &cleanupFixture{run: run, exists: true, running: true}
	f.cleanup = newVMCleanup(store, run.Record.RunID)
	f.cleanup.query, f.cleanup.grace, f.cleanup.poll = f.query, time.Millisecond, time.Millisecond
	for _, artifact := range runArtifacts[:4] {
		if err := os.WriteFile(filepath.Join(store.root.Name(), run.Record.RunID, artifact), []byte("test-owned"), 0600); err != nil {
			t.Fatal(err)
		}
	}
	return f
}

func (f *cleanupFixture) domainXML() string {
	dir := filepath.Join(f.cleanup.store.root.Name(), f.run.Record.RunID)
	return fmt.Sprintf(`<domain><name>%s</name><uuid>%s</uuid><description>%s</description><devices><disk type="file"><source file="%s"/></disk><disk type="file"><source file="%s"/></disk></devices>%s</domain>`, f.run.Record.Domain, f.run.Record.RunID, domainOwner(f.run.Record), filepath.Join(dir, "root.qcow2"), filepath.Join(dir, "seed.iso"), f.extraXML)
}

func (f *cleanupFixture) query(ctx context.Context, args ...string) ([]byte, error) {
	f.calls = append(f.calls, args[0])
	if args[0] == f.refuse || ctx.Err() != nil {
		return nil, errors.New(privateFixture)
	}
	if args[0] != "list" && len(args) > 1 && args[1] != f.run.Record.RunID && args[1] != storeTestWork {
		return nil, errors.New("command used a name or unowned identity")
	}
	switch args[0] {
	case "list":
		var ids []string
		if f.exists {
			ids = append(ids, f.run.Record.RunID)
		}
		if f.foreign != "" {
			ids = append(ids, storeTestWork)
		}
		return []byte(strings.Join(ids, "\n") + "\n"), nil
	case "dumpxml":
		if args[1] == storeTestWork {
			return []byte(f.foreign), nil
		}
		return []byte(f.domainXML()), nil
	case "domstate":
		if f.running {
			return []byte("running\n"), nil
		}
		return []byte("shut off\n"), nil
	case "shutdown":
		if !f.force && !f.refuseStop {
			f.running = false
		}
	case "destroy":
		if !f.refuseStop {
			f.running = false
		}
	case "undefine":
		if f.running {
			return nil, errors.New("undefine is not shutdown")
		}
		f.exists = false
	default:
		return nil, errors.New("unexpected command")
	}
	return nil, nil
}

func TestVMCleanupStopsBeforeUndefiningAndRemovingOwnedFiles(t *testing.T) {
	for _, force := range []bool{false, true} {
		f := newCleanupFixture(t)
		f.force = force
		if err := f.cleanup.Cleanup(context.Background()); err != nil {
			t.Fatal(err)
		}
		if f.exists || f.running {
			t.Fatal("physical domain was not stopped and undefined")
		}
		for _, artifact := range runArtifacts {
			if _, err := os.Lstat(filepath.Join(f.cleanup.store.root.Name(), f.run.Record.RunID, artifact)); !errors.Is(err, os.ErrNotExist) {
				t.Fatal("owned artifact survived successful cleanup")
			}
		}
		run, err := f.cleanup.store.Load(f.run.Record.RunID)
		if err != nil || run.Phase != "cleaned" {
			t.Fatal("cleanup was not durably confirmed")
		}
		if err := f.cleanup.Released(); err != nil {
			t.Fatal(err)
		}
		if err := f.cleanup.Cleanup(context.Background()); err != nil {
			t.Fatal("idempotent cleanup failed")
		}
		var mutations []string
		for _, call := range f.calls {
			if call == "shutdown" || call == "destroy" || call == "undefine" {
				mutations = append(mutations, call)
			}
		}
		want := []string{"shutdown", "undefine"}
		if force {
			want = []string{"shutdown", "destroy", "undefine"}
		}
		if !reflect.DeepEqual(mutations, want) {
			t.Fatalf("mutation ordering %v; want %v", mutations, want)
		}
	}
}

func TestVMCleanupRetainsRecordsAndDisksWhenStateIsUncertain(t *testing.T) {
	for _, refuse := range []string{"list", "dumpxml", "domstate", "undefine", "destroy"} {
		t.Run(refuse, func(t *testing.T) {
			f := newCleanupFixture(t)
			f.refuse = refuse
			f.force = refuse == "destroy"
			err := f.cleanup.Cleanup(context.Background())
			if !errors.Is(err, errVMCleanup) || strings.Contains(err.Error(), privateFixture) {
				t.Fatal("uncertain cleanup lost fixed failure classification")
			}
			if _, err := os.Stat(filepath.Join(f.cleanup.store.root.Name(), f.run.Record.RunID, "root.qcow2")); err != nil {
				t.Fatal("unconfirmed shutdown removed a disk")
			}
			run, err := f.cleanup.store.Load(f.run.Record.RunID)
			if err != nil || run.Phase != "cleanup-pending" {
				t.Fatal("failed cleanup was not retained for recovery")
			}
			if err := f.cleanup.Released(); err == nil {
				t.Fatal("unconfirmed cleanup allowed release marker")
			}
		})
	}
}

func TestVMCleanupRejectsForeignOwnershipBeforeMutation(t *testing.T) {
	for _, extra := range []string{"<name>foreign</name>", "<uuid>foreign</uuid>", "<description>foreign</description>", "<os><nvram>/unowned/nvram.fd</nvram></os>"} {
		f := newCleanupFixture(t)
		f.extraXML = extra
		if err := f.cleanup.Cleanup(context.Background()); err == nil {
			t.Fatal("foreign/ambiguous domain was accepted")
		}
		for _, call := range f.calls {
			if call == "shutdown" || call == "destroy" || call == "undefine" {
				t.Fatal("unowned domain was mutated")
			}
		}
	}
}

func TestVMCleanupAbsentDomainStillChecksForeignDiskReferences(t *testing.T) {
	for _, reference := range []string{"disk", "nvram", "none"} {
		f := newCleanupFixture(t)
		f.exists, f.running = false, false
		path := filepath.Join(f.cleanup.store.root.Name(), f.run.Record.RunID, "root.qcow2")
		switch reference {
		case "disk":
			f.foreign = fmt.Sprintf(`<domain><devices><disk><source file="%s"/></disk></devices></domain>`, path)
		case "nvram":
			f.foreign = fmt.Sprintf(`<domain><os><nvram>%s</nvram></os></domain>`, path)
		case "none":
			f.foreign = `<domain><devices><disk><source file="/unrelated/root.qcow2"/></disk></devices></domain>`
		}
		err := f.cleanup.Cleanup(context.Background())
		if (err == nil) != (reference == "none") {
			t.Fatal("foreign reference check differs")
		}
		if reference != "none" {
			if _, err := os.Stat(path); err != nil {
				t.Fatal("a referenced disk was removed")
			}
		}
	}
}

func TestVMCleanupChecksInactivePersistentDomainReferences(t *testing.T) {
	f := newCleanupFixture(t)
	f.exists, f.running = false, false
	f.foreign = `<domain><devices><disk><source file="/unrelated/current.qcow2"/></disk></devices></domain>`
	path := filepath.Join(f.cleanup.store.root.Name(), f.run.Record.RunID, "root.qcow2")
	query := f.cleanup.query
	f.cleanup.query = func(ctx context.Context, args ...string) ([]byte, error) {
		if args[0] == "dumpxml" && len(args) == 3 && args[2] == "--inactive" {
			return []byte(fmt.Sprintf(`<domain><devices><disk><source file="%s"/></disk></devices></domain>`, path)), nil
		}
		return query(ctx, args...)
	}
	if err := f.cleanup.Cleanup(context.Background()); err == nil {
		t.Fatal("inactive persistent reference allowed disk removal")
	}
	if _, err := os.Stat(path); err != nil {
		t.Fatal("future boot disk was deleted")
	}
}

func TestVMCleanupDistinguishesTransientFromUnreadablePersistentConfig(t *testing.T) {
	for _, transient := range []bool{true, false} {
		f := newCleanupFixture(t)
		f.exists, f.running = false, false
		f.foreign = `<domain/>`
		inactiveCalls := 0
		query := f.cleanup.query
		f.cleanup.query = func(ctx context.Context, args ...string) ([]byte, error) {
			if args[0] == "list" && len(args) == 4 && args[3] == "--persistent" && transient {
				return nil, nil
			}
			if args[0] == "dumpxml" && len(args) == 3 && args[2] == "--inactive" {
				inactiveCalls++
				return nil, errors.New("test-only inactive lookup failure")
			}
			return query(ctx, args...)
		}
		err := f.cleanup.Cleanup(context.Background())
		if transient && (err != nil || inactiveCalls != 0) {
			t.Fatal("transient VM incorrectly required an inactive definition")
		}
		if !transient && (err == nil || inactiveCalls != 1) {
			t.Fatal("unreadable persistent definition allowed cleanup")
		}
	}
}

func TestVMCleanupRejectsUnownedArtifactsAndNeverReDeletesCleanedRuns(t *testing.T) {
	for _, scenario := range []string{"symlink", "hardlink", "unknown", "reappeared"} {
		t.Run(scenario, func(t *testing.T) {
			f := newCleanupFixture(t)
			f.exists, f.running = false, false
			dir := filepath.Join(f.cleanup.store.root.Name(), f.run.Record.RunID)
			path := filepath.Join(dir, "root.qcow2")
			outside := filepath.Join(t.TempDir(), "preserve")
			if err := os.WriteFile(outside, []byte("preserve"), 0600); err != nil {
				t.Fatal(err)
			}
			switch scenario {
			case "symlink", "hardlink":
				if err := os.Remove(path); err != nil {
					t.Fatal(err)
				}
				var err error
				if scenario == "symlink" {
					err = os.Symlink(outside, path)
				} else {
					err = os.Link(outside, path)
				}
				if err != nil {
					t.Fatal(err)
				}
			case "unknown":
				if err := os.WriteFile(filepath.Join(dir, "unowned"), nil, 0600); err != nil {
					t.Fatal(err)
				}
			case "reappeared":
				if err := f.cleanup.Cleanup(context.Background()); err != nil {
					t.Fatal(err)
				}
				if err := os.WriteFile(path, []byte("new-owner"), 0600); err != nil {
					t.Fatal(err)
				}
			}
			if err := f.cleanup.Cleanup(context.Background()); err == nil {
				t.Fatal("unowned artifact accepted")
			}
			data, err := os.ReadFile(outside)
			if err != nil || string(data) != "preserve" {
				t.Fatal("foreign file changed")
			}
			if _, err := os.Lstat(path); err != nil {
				t.Fatal("unowned path removed")
			}
		})
	}
}

func TestVMCleanupCancellationCannotConfirmShutdown(t *testing.T) {
	f := newCleanupFixture(t)
	f.refuseStop = true
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Millisecond)
	defer cancel()
	if err := f.cleanup.Cleanup(ctx); err == nil {
		t.Fatal("timeout reported a stopped VM")
	}
	if !f.running {
		t.Fatal("fixture did not exercise a still-running domain")
	}
}

func TestVMCleanupRejectsMissingOrMalformedDomainEvidence(t *testing.T) {
	for _, data := range []string{"", "<domain>", "<other/>", "<domain/><domain/>", "<domain/>extra", `<!DOCTYPE domain><domain/>`, `<domain id="1" id="2"/>`} {
		f := newCleanupFixture(t)
		f.exists, f.running = false, false
		f.foreign = "<domain/>"
		query := f.cleanup.query
		f.cleanup.query = func(ctx context.Context, args ...string) ([]byte, error) {
			if args[0] == "dumpxml" {
				return []byte(data), nil
			}
			return query(ctx, args...)
		}
		if err := f.cleanup.Cleanup(context.Background()); err == nil {
			t.Fatal("missing/ambiguous domain evidence allowed disk removal")
		}
		if _, err := os.Stat(filepath.Join(f.cleanup.store.root.Name(), f.run.Record.RunID, "root.qcow2")); err != nil {
			t.Fatal("disk removed without valid domain evidence")
		}
	}
}

func TestVirshQueryBoundsOutputAndDiscardsPrivateFailures(t *testing.T) {
	for _, scenario := range []string{"success", "exit", "oversize", "cancel"} {
		t.Run(scenario, func(t *testing.T) {
			root := t.TempDir()
			t.Setenv("PATH", root)
			t.Setenv("KELPIE_QUERY_PRIVATE_TEST", privateFixture)
			body := "#!/bin/sh\n"
			switch scenario {
			case "success":
				body += "printf 'ok:%s' \"$KELPIE_QUERY_PRIVATE_TEST\"\n"
			case "exit":
				body += "printf 'test-only-private' >&2\nexit 7\n"
			case "oversize":
				body += "i=0\nwhile [ $i -lt 30000 ]; do printf 'test-only-private'; i=$((i+1)); done\n"
			case "cancel":
				body += "/bin/sleep 30\n"
			}
			if err := os.WriteFile(filepath.Join(root, "virsh"), []byte(body), 0700); err != nil {
				t.Fatal(err)
			}
			ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
			defer cancel()
			if scenario == "cancel" {
				cancel()
			}
			data, err := queryVirsh(ctx, "list", "--all", "--uuid")
			if scenario == "success" {
				if err != nil || string(data) != "ok:" {
					t.Fatal("ambient private environment reached VM query")
				}
			} else if !errors.Is(err, errVMCleanup) || data != nil {
				t.Fatal("failed query retained output or returned success")
			}
		})
	}
}
