//go:build !linux

package daemon

import "os"

func validHypervisorSearch(*os.File) bool { return false }

// Actual libvirt execution is supported on the Linux host, not macOS itself.
func grantHypervisorSearch(*runStore, string) error { return errRunStore }

func privateHypervisorArtifact(os.FileInfo) bool { return false }
