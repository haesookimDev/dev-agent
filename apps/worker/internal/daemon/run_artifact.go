package daemon

import (
	"os"
	"syscall"
)

// External image/seed tools may honor a permissive umask. Protect their exact
// newly created file before it can be handed to a VM; never chmod a link target.
func privateRunArtifact(store *runStore, name string) error {
	file, err := store.root.OpenFile(name, os.O_RDWR|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
	if err != nil {
		return diagnosticError{kind: vmSeedData}
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil || !info.Mode().IsRegular() {
		return diagnosticError{kind: vmSeedData}
	}
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || stat.Uid != uint32(os.Geteuid()) || stat.Nlink != 1 ||
		info.Mode()&(os.ModeSetuid|os.ModeSetgid|os.ModeSticky) != 0 {
		return diagnosticError{kind: vmSeedData}
	}
	if file.Chmod(0600) != nil || file.Sync() != nil {
		return diagnosticError{kind: vmSeedData}
	}
	return nil
}
