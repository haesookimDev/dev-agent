package daemon

import (
	"context"
	"os"
	"os/exec"
	"os/user"
	"strconv"
	"strings"
	"syscall"
	"time"
)

// The supported Ubuntu host runs QEMU as libvirt-qemu. Only that named UID may
// search run directories: no listing, writing, default ACL or metadata access.
// Do not disable libvirt DAC/AppArmor or change QEMU to the Worker/root user.
func hypervisorUID() (string, bool) {
	account, err := user.Lookup("libvirt-qemu")
	if err != nil {
		return "", false
	}
	uid, err := strconv.ParseUint(account.Uid, 10, 32)
	return account.Uid, err == nil && uid != 0 && uid != uint64(os.Geteuid()) && strconv.FormatUint(uid, 10) == account.Uid
}

func exactSearchACL(data []byte, uid string, basicAllowed bool) bool {
	if len(data) > 1024 {
		return false
	}
	value := strings.TrimSpace(string(data))
	basic := "user::rwx\ngroup::---\nother::---"
	search := "user::rwx\nuser:" + uid + ":--x\ngroup::---\nmask::--x\nother::---"
	return value == search || basicAllowed && value == basic
}

func aclCommand(file *os.File, name string, args ...string) ([]byte, error) {
	if name != "getfacl" && name != "setfacl" {
		return nil, errRunStore
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	command := exec.CommandContext(ctx, "/usr/bin/"+name, append(args, "/proc/self/fd/3")...)
	command.Env = []string{"PATH=/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL=C", "LANG=C"}
	command.ExtraFiles = []*os.File{file} // Anchored inode, not a mutable pathname.
	command.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	command.Cancel = func() error { return syscall.Kill(-command.Process.Pid, syscall.SIGKILL) }
	command.WaitDelay = time.Second
	var output boundedVMOutput
	command.Stdout = &output
	if command.Run() != nil {
		return nil, errRunStore
	}
	return output.buffer.Bytes(), nil
}

func readDirectoryACL(file *os.File) ([]byte, error) {
	return aclCommand(file, "getfacl", "--omit-header", "--numeric", "--no-effective", "--absolute-names")
}

func validHypervisorSearch(file *os.File) bool {
	uid, ok := hypervisorUID()
	if !ok {
		return false
	}
	data, err := readDirectoryACL(file)
	return err == nil && exactSearchACL(data, uid, false)
}

func grantHypervisorSearch(store *runStore, runID string) error {
	if _, err := store.Load(runID); err != nil {
		return err
	}
	uid, ok := hypervisorUID()
	if !ok {
		return errRunStore
	}
	for _, name := range []string{".", runID} {
		if err := grantDirectorySearch(store.root, name, uid); err != nil {
			return err
		}
	}
	return nil
}

func grantDirectorySearch(root *os.Root, name, uid string) error {
	file, err := root.OpenFile(name, os.O_RDONLY|syscall.O_DIRECTORY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
	if err != nil {
		return errRunStore
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil || !ownedDirectoryMode(info) {
		return errRunStore
	}
	data, err := readDirectoryACL(file)
	if err != nil || !exactSearchACL(data, uid, true) {
		return errRunStore // Never overwrite unexpected/default/masked ACL grants.
	}
	if !exactSearchACL(data, uid, false) {
		acl := "user::rwx,user:" + uid + ":--x,group::---,mask::--x,other::---"
		if _, err := aclCommand(file, "setfacl", "--set="+acl); err != nil {
			return err
		}
	}
	if file.Sync() != nil || !validHypervisorSearch(file) {
		return errRunStore
	}
	return nil
}

func privateHypervisorArtifact(info os.FileInfo) bool {
	uid, ok := hypervisorUID()
	return ok && privateArtifactUID(info, uid)
}

func privateArtifactUID(info os.FileInfo, uid string) bool {
	stat, statOK := info.Sys().(*syscall.Stat_t)
	return statOK && strconv.FormatUint(uint64(stat.Uid), 10) == uid &&
		stat.Nlink == 1 && info.Mode().IsRegular() && info.Mode().Perm() == 0600 &&
		info.Mode()&(os.ModeSymlink|os.ModeSetuid|os.ModeSetgid|os.ModeSticky) == 0
}
