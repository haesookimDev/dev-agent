package daemon

import (
	"context"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"testing"
)

func TestInvalidGuestClaimIsRejectedBeforeHostEffectsAndSeed(t *testing.T) {
	control, err := parseGuestControl("https://control.example.test", "192.0.2.7")
	if err != nil {
		t.Fatal(err)
	}
	for _, work := range []WorkItem{
		{Status: "provisioning", Version: 0},
		{Status: "provisioning", Version: -1},
		{Status: "provisioning", Version: math.MaxInt},
		{Status: "analyzing", Version: 2},
		{Status: "cancelled", Version: 2},
		{Status: "", Version: 2},
	} {
		t.Run(fmt.Sprintf("%s/%d", work.Status, work.Version), func(t *testing.T) {
			claim := resourceClaim()
			claim.WorkItem = work
			claim.WorkItem.ID = storeTestWork
			root := t.TempDir()
			marker := filepath.Join(root, "host-command-ran")
			base := filepath.Join(root, "base.qcow2")
			if err := os.WriteFile(base, []byte("synthetic image"), 0600); err != nil {
				t.Fatal(err)
			}
			script := "#!/bin/sh\n: > '" + marker + "'\nexit 9\n"
			if err := os.WriteFile(filepath.Join(root, "qemu-img"), []byte(script), 0700); err != nil {
				t.Fatal(err)
			}
			t.Setenv("PATH", root)
			store := newTestRunStore(t)
			executor := LibvirtExecutor{store: store, config: Config{
				BaseImage: base, GuestControlURL: control.Origin, GuestControlIPv4: control.IPv4,
			}}
			if err := executor.Execute(context.Background(), nil, claim); err == nil || safeDiagnostic(err) != "VM Runner control bootstrap was not confirmed" {
				t.Error("invalid claim was not rejected at the initial bootstrap gate")
			}
			if _, err := os.Stat(marker); !os.IsNotExist(err) {
				t.Error("invalid claim caused a host command")
			}
			if data, err := guestUserData(control, claim, nil); err == nil || data != nil {
				t.Error("invalid claim generated a Runner assignment")
			}
		})
	}
}
