package daemon

import (
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
)

type artifactTestInfo struct {
	os.FileInfo
	stat syscall.Stat_t
	mode os.FileMode
}

func (i artifactTestInfo) Sys() any          { return &i.stat }
func (i artifactTestInfo) Mode() os.FileMode { return i.mode }

func TestHypervisorArtifactRequiresExactPrivateSingleLinkOwner(t *testing.T) {
	path := filepath.Join(t.TempDir(), "fixture")
	if err := os.WriteFile(path, []byte("synthetic"), 0600); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	valid := artifactTestInfo{FileInfo: info, stat: syscall.Stat_t{Uid: 64055, Nlink: 1}, mode: 0600}
	if !privateArtifactUID(valid, "64055") {
		t.Fatal("private single-link QEMU artifact rejected")
	}
	for _, change := range []func(*artifactTestInfo){
		func(i *artifactTestInfo) { i.stat.Uid++ },
		func(i *artifactTestInfo) { i.stat.Uid = 0 },
		func(i *artifactTestInfo) { i.stat.Nlink = 2 },
		func(i *artifactTestInfo) { i.mode = 0644 },
		func(i *artifactTestInfo) { i.mode = 0600 | os.ModeSymlink },
		func(i *artifactTestInfo) { i.mode = 0600 | os.ModeNamedPipe },
		func(i *artifactTestInfo) { i.mode = 0600 | os.ModeDir },
		func(i *artifactTestInfo) { i.mode = 0600 | os.ModeSetuid },
		func(i *artifactTestInfo) { i.mode = 0600 | os.ModeSetgid },
		func(i *artifactTestInfo) { i.mode = 0600 | os.ModeSticky },
	} {
		candidate := valid
		change(&candidate)
		if privateArtifactUID(candidate, "64055") {
			t.Fatal("unowned or unsafe QEMU artifact accepted")
		}
	}
}

func TestACLToolLookupIgnoresCallerPATH(t *testing.T) {
	dir := t.TempDir()
	for _, name := range []string{"getfacl", "setfacl"} {
		if err := os.WriteFile(filepath.Join(dir, name), []byte("#!/bin/sh\necho unexpected-path-lookup\n"), 0700); err != nil {
			t.Fatal(err)
		}
	}
	t.Setenv("PATH", dir)
	file, err := os.Open(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	for _, name := range []string{"getfacl", "setfacl"} {
		data, _ := aclCommand(file, name, "--version")
		if strings.Contains(string(data), "unexpected-path-lookup") {
			t.Fatal("caller PATH replaced a trusted ACL tool")
		}
	}
}

func TestHypervisorSearchACLRejectsBroaderOrAmbiguousPermissions(t *testing.T) {
	const uid = "64055"
	const exact = "user::rwx\nuser:64055:--x\ngroup::---\nmask::--x\nother::---\n"
	for _, changed := range []string{
		strings.Replace(exact, "user:64055:--x", "user:64055:r-x", 1),
		strings.Replace(exact, "user:64055:--x", "user:64055:-wx", 1),
		strings.Replace(exact, "64055", "64056", 1),
		strings.Replace(exact, "group::---", "group::--x", 1),
		strings.Replace(exact, "other::---", "other::--x", 1),
		strings.Replace(exact, "mask::--x", "mask::rwx", 1),
		exact + "default:user::rwx\n",
		exact + "user:64056:rwx\n",
		exact + "user:64055:--x\n",
		strings.Repeat("x", 1025),
		"",
	} {
		if exactSearchACL([]byte(changed), uid, true) {
			t.Fatal("unexpected/masked/default ACL was accepted")
		}
	}
	if !exactSearchACL([]byte(exact), uid, false) {
		t.Fatal("exact search-only ACL was rejected")
	}
	basic := []byte("user::rwx\ngroup::---\nother::---\n")
	if exactSearchACL(basic, uid, false) || !exactSearchACL(basic, uid, true) {
		t.Fatal("private pre-grant ACL differs")
	}
}
