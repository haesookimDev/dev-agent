package daemon

import (
	"os"
	"path/filepath"
	"testing"
)

func TestRunArtifactPrivateModeWithoutFollowingLinks(t *testing.T) {
	for _, scenario := range []string{"regular", "symlink", "hardlink", "directory"} {
		t.Run(scenario, func(t *testing.T) {
			store := newTestRunStore(t)
			run := createTestRun(t, store)
			name := run.Record.RunID + "/root.qcow2"
			path := filepath.Join(store.root.Name(), name)
			outside := filepath.Join(t.TempDir(), "unowned")
			if err := os.WriteFile(outside, []byte("preserve"), 0644); err != nil {
				t.Fatal(err)
			}
			if err := os.Chmod(outside, 0644); err != nil {
				t.Fatal(err)
			}
			var err error
			switch scenario {
			case "regular":
				err = os.WriteFile(path, []byte("owned"), 0644)
			case "symlink":
				err = os.Symlink(outside, path)
			case "hardlink":
				err = os.Link(outside, path)
			case "directory":
				err = os.Mkdir(path, 0700)
			}
			if err != nil {
				t.Fatal(err)
			}
			err = privateRunArtifact(store, name)
			if (err == nil) != (scenario == "regular") {
				t.Fatal("artifact type/ownership guard differs")
			}
			if scenario == "regular" {
				info, err := os.Stat(path)
				if err != nil || info.Mode().Perm() != 0600 {
					t.Fatal("artifact remains public")
				}
			}
			info, err := os.Stat(outside)
			if err != nil || info.Mode().Perm() != 0644 {
				t.Fatal("foreign link target permissions changed")
			}
		})
	}
}
