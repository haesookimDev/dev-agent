package daemon

import (
	"path/filepath"
	"slices"
	"testing"
)

func TestLibvirtFirmwareMatchesNativeImageArchitecture(t *testing.T) {
	runDir := filepath.Join(t.TempDir(), "55555555-5555-4555-8555-555555555555")
	for _, architecture := range []string{"arm64", "amd64", "unsupported"} {
		t.Run(architecture, func(t *testing.T) {
			args, err := libvirtBootArguments(architecture, runDir)
			switch architecture {
			case "arm64":
				if err != nil || !slices.Equal(args, []string{"--boot", "uefi,nvram=" + filepath.Join(runDir, "nvram.fd")}) {
					t.Fatal("ARM firmware variables escape the recorded run")
				}
			case "amd64":
				if err != nil || len(args) != 0 {
					t.Fatal("ARM UEFI selection changes the existing amd64 image boot contract")
				}
			default:
				if err == nil || len(args) != 0 {
					t.Fatal("unsupported architecture silently selected firmware")
				}
			}
		})
	}
}
