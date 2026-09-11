//go:build linux && libvirt_integration

package daemon

import (
	"context"
	"crypto/sha256"
	"io"
	"os"
	"strings"
	"syscall"
	"testing"
	"time"
)

// Test-only access preparation, not an image deployment API. Keep the existing
// inode open, grant only QEMU read (never write), and restore the exact original
// ACL after confirmed domain absence. Ancestor search ACLs must be separately
// prepared/restored by the disposable-host operator, never inferred here.
func packetBaseReadAccess(t *testing.T, path string) func() {
	t.Helper()
	file, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
	if err != nil {
		t.Fatal("could not anchor fixture base image")
	}
	info, err := file.Stat()
	uid, ok := hypervisorUID()
	if err != nil || !privateOwned(info, false) || !ok {
		file.Close()
		t.Fatal("fixture base image is not private and Worker-owned")
	}
	const original = "user::rw-\ngroup::---\nother::---"
	expected := "user::rw-\nuser:" + uid + ":r--\ngroup::---\nmask::r--\nother::---"
	acl, err := readDirectoryACL(file) // getfacl also supports this anchored file.
	if err != nil || strings.TrimSpace(string(acl)) != original {
		file.Close()
		t.Fatal("refuse to overwrite a pre-existing fixture image ACL")
	}
	hash := func() [32]byte {
		if _, err := file.Seek(0, io.SeekStart); err != nil {
			t.Fatal("fixture base image seek failed")
		}
		digest := sha256.New()
		if _, err := io.Copy(digest, file); err != nil {
			t.Fatal("fixture base image hash failed")
		}
		return [32]byte(digest.Sum(nil))
	}
	before := hash()
	identity := func() bool {
		current, err := os.Lstat(path)
		if err != nil || !os.SameFile(info, current) || current.Size() != info.Size() || !current.Mode().IsRegular() {
			return false
		}
		initial := info.Sys().(*syscall.Stat_t)
		stat, ok := current.Sys().(*syscall.Stat_t)
		return ok && stat.Uid == initial.Uid && stat.Gid == initial.Gid && stat.Nlink == 1
	}
	t.Cleanup(func() {
		defer file.Close()
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		domains, err := queryVirsh(ctx, "list", "--all", "--uuid")
		if err != nil || strings.TrimSpace(string(domains)) != "" || !identity() {
			t.Error("fixture image ACL restoration blocked: domain absence or original inode/owner unconfirmed")
			return
		}
		acl, err := readDirectoryACL(file)
		if err != nil || (strings.TrimSpace(string(acl)) != expected && strings.TrimSpace(string(acl)) != original) {
			t.Error("fixture image ACL changed unexpectedly; not overwritten")
			return
		}
		if _, err := aclCommand(file, "setfacl", "--set="+strings.ReplaceAll(original, "\n", ",")); err != nil || file.Sync() != nil {
			t.Error("fixture image ACL restoration failed")
			return
		}
		acl, err = readDirectoryACL(file)
		current, statErr := file.Stat()
		if err != nil || statErr != nil || strings.TrimSpace(string(acl)) != original || !privateOwned(current, false) || hash() != before {
			t.Error("fixture image bytes/permissions were not preserved")
			return
		}
		t.Logf("base image owner/private ACL restored; unchanged SHA256 %x", before)
	})
	if _, err := aclCommand(file, "setfacl", "--set="+strings.ReplaceAll(expected, "\n", ",")); err != nil || file.Sync() != nil {
		t.Fatal("could not grant exact QEMU read-only access")
	}
	verify := func() {
		t.Helper()
		acl, err := readDirectoryACL(file)
		if !identity() || err != nil || strings.TrimSpace(string(acl)) != expected || hash() != before {
			t.Fatal("fixture backing image owner/read-only ACL/bytes changed")
		}
	}
	verify()
	t.Logf("anchored fixture image: QEMU read-only ACL; original SHA256 %x", before)
	return verify
}
