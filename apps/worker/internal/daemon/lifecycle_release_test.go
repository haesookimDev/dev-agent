package daemon

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

func TestPhysicalCleanupGuardsEveryLeaseRelease(t *testing.T) {
	for _, scenario := range []string{"terminal", "executor-failed", "cleanup-failed", "cancelled"} {
		t.Run(scenario, func(t *testing.T) {
			fixture := newCleanupFixture(t)
			if scenario == "cleanup-failed" {
				fixture.refuse = "list"
			}
			releases, premature := 0, false
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				switch {
				case strings.HasSuffix(r.URL.Path, "/release"):
					releases++
					premature = premature || fixture.exists || fixture.running
					w.WriteHeader(http.StatusNoContent)
				case r.Method == http.MethodGet:
					_ = json.NewEncoder(w).Encode(WorkItem{ID: storeTestWork, Status: "failed", Version: 3})
				default:
					w.WriteHeader(http.StatusNoContent)
				}
			}))
			defer server.Close()
			daemon := resourceDaemon(server)
			daemon.tracker.Reserve(testResources)
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			daemon.executor = executorFunc(func(ctx context.Context, client RunClient, claim Claim) error {
				// The old client lacks binding and demonstrates the unsafe physical
				// release, rather than treating a missing method as the regression.
				if binder, ok := client.(interface{ BindResources(ResourceLifecycle) error }); ok {
					if err := binder.BindResources(fixture.cleanup); err != nil {
						return err
					}
				}
				if scenario == "cancelled" {
					cancel()
					return ctx.Err()
				}
				if scenario != "terminal" {
					return errors.New("test execution failed")
				}
				return client.Release(ctx, claim.WorkItem.ID, claim.LeaseToken)
			})
			claim := resourceClaim()
			claim.WorkItem.ID = storeTestWork
			daemon.execute(ctx, claim)
			if premature {
				t.Fatal("lease was released while the VM still existed")
			}
			if scenario == "cleanup-failed" {
				if releases != 0 {
					t.Fatal("failed cleanup reached remote release")
				}
				assertResources(t, daemon, Resources{}, 1)
			} else {
				if releases != 1 || fixture.exists || fixture.running {
					t.Fatal("successful cleanup did not precede exactly one release")
				}
				assertResources(t, daemon, testResources, 0)
				run, err := fixture.cleanup.store.Load(fixture.run.Record.RunID)
				if err != nil || run.Phase != "released" {
					t.Fatal("acknowledged release was not persisted")
				}
			}
		})
	}
}

func TestLibvirtAttemptUsesRecordedIdentityBeforeAnyLaunch(t *testing.T) {
	emptyDomainInventory(t)
	store := newTestRunStore(t)
	commandDir := os.Getenv("PATH")
	arguments := filepath.Join(commandDir, "launch-arguments")
	for name, body := range map[string]string{
		"qemu-img":      "#!/bin/sh\nwhile [ \"$#\" -gt 2 ]; do shift; done\n: > \"$1\"\n",
		"cloud-localds": "#!/bin/sh\n: > \"$1\"\n",
		"virt-install":  "#!/bin/sh\nprintf '%s\\n' \"$@\" > '" + arguments + "'\nexit 7\n",
	} {
		if err := os.WriteFile(filepath.Join(commandDir, name), []byte(body), 0700); err != nil {
			t.Fatal(err)
		}
	}
	image := filepath.Join(commandDir, "synthetic-base.qcow2")
	if err := os.WriteFile(image, []byte("synthetic input"), 0600); err != nil {
		t.Fatal(err)
	}
	claim := resourceClaim()
	claim.WorkItem.ID = storeTestWork
	claim.LeaseID = "55555555-5555-4555-8555-555555555555"
	var releasePhase string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case strings.HasSuffix(r.URL.Path, "/release"):
			runs, err := store.List()
			if err != nil || len(runs) != 1 {
				t.Error("release had no durable ownership")
				w.WriteHeader(500)
				return
			}
			releasePhase = runs[0].Phase
			w.WriteHeader(http.StatusNoContent)
		case r.Method == http.MethodGet:
			_ = json.NewEncoder(w).Encode(WorkItem{ID: storeTestWork, Status: "failed", Version: 3})
		default:
			w.WriteHeader(http.StatusNoContent)
		}
	}))
	defer server.Close()
	d := resourceDaemon(server)
	d.config.BaseImage, d.config.WorkRoot = image, store.root.Name()
	d.config.GuestControlURL, d.config.GuestControlIPv4 = "https://control.example.test", "192.0.2.7"
	// Launch commands and the Linux permission boundary are synthetic in this
	// cross-platform identity test; the opt-in Linux suite covers real libvirt.
	d.executor = LibvirtExecutor{config: d.config, logger: d.logger, store: store,
		prepareAccess: func(*runStore, string) error { return nil }}
	d.tracker.Reserve(testResources)
	d.execute(context.Background(), claim)
	runs, err := store.List()
	if err != nil || len(runs) != 1 || runs[0].Phase != "released" || releasePhase != "cleaned" {
		t.Fatal("launch failure did not clean and durably release its exact run")
	}
	if runs[0].Record.Schema != 2 || runs[0].Record.RunID != claim.LeaseID {
		t.Fatal("durable run identity is not bound to the claimed API lease")
	}
	data, err := os.ReadFile(arguments)
	if err != nil {
		t.Fatal("launch command was not reached")
	}
	args := string(data)
	if !strings.Contains(args, "--uuid\n"+runs[0].Record.RunID+"\n") ||
		!strings.Contains(args, "--name\n"+runs[0].Record.Domain+"\n") ||
		!strings.Contains(args, "description="+domainOwner(runs[0].Record)+"\n") || strings.Contains(args, claim.LeaseToken) {
		t.Fatal("launch lost ownership metadata or leaked a lease argument")
	}
	nvram := filepath.Join(store.root.Name(), runs[0].Record.RunID, "nvram.fd")
	if runtime.GOARCH == "arm64" && !strings.Contains(args, "--boot\nuefi,nvram="+nvram+"\n") ||
		runtime.GOARCH == "amd64" && strings.Contains(args, "--boot\n") ||
		!strings.Contains(args, "--virt-type\nkvm\n") ||
		runtime.GOARCH == "arm64" && !strings.Contains(args, "/seed.iso,device=cdrom,bus=scsi\n") ||
		runtime.GOARCH == "amd64" && !strings.Contains(args, "/seed.iso,device=cdrom\n") {
		t.Fatal("launch did not require KVM, owned UEFI variables and an ARM-compatible seed bus")
	}
	for _, artifact := range runArtifacts {
		if _, err := os.Stat(filepath.Join(store.root.Name(), runs[0].Record.RunID, artifact)); !errors.Is(err, os.ErrNotExist) {
			t.Fatal("failed launch left its owned disk or credential seed")
		}
	}
	assertResources(t, d, testResources, 0)
}
