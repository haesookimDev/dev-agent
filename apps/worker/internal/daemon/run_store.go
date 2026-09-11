package daemon

import (
	"bytes"
	"crypto/rand"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"slices"
	"syscall"
	"time"
)

var errRunStore = errors.New("VM ownership record unavailable or invalid")
var runUUID = regexp.MustCompile(`^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$`)
var workUUID = regexp.MustCompile(`^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$`)

const maxRunRecord = 4096

// Identity is immutable and durable before any VM command. No claim token,
// assignment, environment, repository or user-supplied path belongs here.
type runRecord struct {
	Schema    int       `json:"schema"`
	RunID     string    `json:"run_id"`
	WorkID    string    `json:"work_id"`
	Domain    string    `json:"domain"`
	Resources Resources `json:"resources"`
	CreatedAt time.Time `json:"created_at"`
}

type runPhase struct {
	RunID string    `json:"run_id"`
	Phase string    `json:"phase"`
	At    time.Time `json:"at"`
}

type ownedRun struct {
	Record runRecord
	Phase  string
	At     time.Time
}

// A single daemon owns the private root for its entire lifetime. Root-relative
// operations cannot escape it through a replaced path component. The lock is
// never unlinked: removing a locked file would permit two owners on two inodes.
type runStore struct {
	root *os.Root
	lock *os.File
}

func openRunStore(path string) (*runStore, error) {
	if !filepath.IsAbs(path) || filepath.Clean(path) != path || path == string(filepath.Separator) {
		return nil, errRunStore
	}
	if err := os.Mkdir(path, 0700); err != nil && !errors.Is(err, os.ErrExist) {
		return nil, errRunStore
	}
	before, err := os.Lstat(path)
	if err != nil || !ownedDirectoryMode(before) {
		return nil, errRunStore
	}
	root, err := os.OpenRoot(path)
	if err != nil {
		return nil, errRunStore
	}
	fail := func() (*runStore, error) { _ = root.Close(); return nil, errRunStore }
	after, err := root.Stat(".")
	if err != nil || !os.SameFile(before, after) {
		return fail()
	}
	if !validStoreDirectory(root, ".") {
		return fail()
	}
	lock, err := root.OpenFile(".worker.lock", os.O_CREATE|os.O_RDWR|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0600)
	if err != nil {
		return fail()
	}
	info, err := lock.Stat()
	if err != nil || !privateOwned(info, false) || info.Size() != 0 ||
		syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB) != nil {
		_ = lock.Close()
		return fail()
	}
	return &runStore{root: root, lock: lock}, nil
}

func (s *runStore) Close() {
	_ = s.lock.Close()
	_ = s.root.Close()
}

func privateOwned(info os.FileInfo, directory bool) bool {
	stat, ok := info.Sys().(*syscall.Stat_t)
	if !ok || stat.Uid != uint32(os.Geteuid()) || info.Mode().Perm()&0077 != 0 ||
		info.Mode()&(os.ModeSymlink|os.ModeSetuid|os.ModeSetgid|os.ModeSticky) != 0 {
		return false
	}
	if directory {
		return info.IsDir() && info.Mode().Perm()&0700 == 0700
	}
	return info.Mode().IsRegular() && stat.Nlink == 1 && info.Mode().Perm() == 0600
}

func ownedDirectoryMode(info os.FileInfo) bool {
	stat, ok := info.Sys().(*syscall.Stat_t)
	return ok && stat.Uid == uint32(os.Geteuid()) && info.IsDir() &&
		info.Mode()&(os.ModeSymlink|os.ModeSetuid|os.ModeSetgid|os.ModeSticky) == 0 &&
		(info.Mode().Perm() == 0700 || info.Mode().Perm() == 0710)
}

func validStoreDirectory(root *os.Root, name string) bool {
	file, err := root.OpenFile(name, os.O_RDONLY|syscall.O_DIRECTORY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
	if err != nil {
		return false
	}
	defer file.Close()
	info, err := file.Stat()
	return err == nil && ownedDirectoryMode(info) && (privateOwned(info, true) || validHypervisorSearch(file))
}

func (s *runStore) Create(workID string, resources Resources) (ownedRun, error) {
	if !workUUID.MatchString(workID) || resources.CPU < 1 || resources.MemoryMB < 1 || resources.DiskGB < 1 {
		return ownedRun{}, errRunStore
	}
	var id [16]byte
	if _, err := rand.Read(id[:]); err != nil {
		return ownedRun{}, errRunStore
	}
	id[6], id[8] = id[6]&0x0f|0x40, id[8]&0x3f|0x80
	uuid := fmt.Sprintf("%x-%x-%x-%x-%x", id[:4], id[4:6], id[6:8], id[8:10], id[10:])
	if err := s.root.Mkdir(uuid, 0700); err != nil {
		return ownedRun{}, errRunStore
	}
	record := runRecord{Schema: 1, RunID: uuid, WorkID: workID, Domain: "kelpie-" + uuid,
		Resources: resources, CreatedAt: time.Now().UTC()}
	if err := s.writeExclusive(uuid+"/run.json", record); err != nil {
		// A partial record is preserved and fails recovery closed, never reused.
		return ownedRun{}, err
	}
	if err := s.syncDirectory("."); err != nil {
		return ownedRun{}, err
	}
	return ownedRun{Record: record, Phase: "prepared", At: record.CreatedAt}, nil
}

func (s *runStore) Load(uuid string) (ownedRun, error) {
	if !runUUID.MatchString(uuid) {
		return ownedRun{}, errRunStore
	}
	if !validStoreDirectory(s.root, uuid) {
		return ownedRun{}, errRunStore
	}
	var record runRecord
	if err := s.readRecord(uuid+"/run.json", &record); err != nil {
		return ownedRun{}, err
	}
	if record.Schema != 1 || record.RunID != uuid || !workUUID.MatchString(record.WorkID) ||
		record.Domain != "kelpie-"+uuid || record.Resources.CPU < 1 || record.Resources.MemoryMB < 1 ||
		record.Resources.DiskGB < 1 || record.CreatedAt.IsZero() || record.CreatedAt.Location() != time.UTC {
		return ownedRun{}, errRunStore
	}
	result := ownedRun{Record: record, Phase: "prepared", At: record.CreatedAt}
	for _, phase := range []string{"running", "cleanup-pending", "cleaned", "released"} {
		var marker runPhase
		if err := s.readRecord(uuid+"/"+phase+".json", &marker); errors.Is(err, os.ErrNotExist) {
			continue
		} else if err != nil {
			return ownedRun{}, err
		}
		if marker.RunID != uuid || marker.Phase != phase || !nextRunPhase(result.Phase, phase) ||
			marker.At.Before(result.At) || marker.At.Location() != time.UTC {
			return ownedRun{}, errRunStore
		}
		result.Phase, result.At = phase, marker.At
	}
	return result, nil
}

func nextRunPhase(current, next string) bool {
	return current == "prepared" && (next == "running" || next == "cleanup-pending") ||
		current == "running" && next == "cleanup-pending" ||
		current == "cleanup-pending" && next == "cleaned" || current == "cleaned" && next == "released"
}

func (s *runStore) Advance(uuid, phase string) (ownedRun, error) {
	run, err := s.Load(uuid)
	if err != nil {
		return ownedRun{}, err
	}
	if run.Phase == phase {
		return run, nil
	}
	if !nextRunPhase(run.Phase, phase) {
		return ownedRun{}, errRunStore
	}
	at := time.Now().UTC()
	if at.Before(run.At) {
		at = run.At // A backwards wall-clock adjustment cannot regress the journal.
	}
	if err := s.writeExclusive(uuid+"/"+phase+".json", runPhase{RunID: uuid, Phase: phase, At: at}); err != nil {
		return ownedRun{}, err
	}
	return s.Load(uuid)
}

func (s *runStore) List() ([]ownedRun, error) {
	dir, err := s.root.Open(".")
	if err != nil {
		return nil, errRunStore
	}
	defer dir.Close()
	entries, err := dir.ReadDir(4097)
	if err != nil && !errors.Is(err, io.EOF) || len(entries) > 4096 {
		return nil, errRunStore
	}
	slices.SortFunc(entries, func(a, b os.DirEntry) int { return bytes.Compare([]byte(a.Name()), []byte(b.Name())) })
	var result []ownedRun
	for _, entry := range entries {
		if entry.Name() == ".worker.lock" {
			continue
		}
		run, err := s.Load(entry.Name())
		if err != nil {
			// Unknown/legacy directories are not evidence of ownership.
			return nil, err
		}
		result = append(result, run)
	}
	return result, nil
}

func (s *runStore) readRecord(name string, value any) error {
	file, err := s.root.OpenFile(name, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
	if errors.Is(err, os.ErrNotExist) {
		return os.ErrNotExist
	}
	if err != nil {
		return errRunStore
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil || !privateOwned(info, false) || info.Size() > maxRunRecord {
		return errRunStore
	}
	data, err := io.ReadAll(io.LimitReader(file, maxRunRecord+1))
	if err != nil || len(data) > maxRunRecord || json.Unmarshal(data, value) != nil {
		return errRunStore
	}
	canonical, err := json.Marshal(value)
	if err != nil || !bytes.Equal(data, append(canonical, '\n')) {
		// Reject duplicate/unknown fields, trailing data and manual record edits.
		return errRunStore
	}
	return nil
}

func (s *runStore) writeExclusive(name string, value any) error {
	data, err := json.Marshal(value)
	if err != nil || len(data)+1 > maxRunRecord {
		return errRunStore
	}
	file, err := s.root.OpenFile(name, os.O_WRONLY|os.O_CREATE|os.O_EXCL|syscall.O_NOFOLLOW, 0600)
	if err != nil {
		return errRunStore
	}
	_, writeErr := file.Write(append(data, '\n'))
	syncErr := file.Sync()
	closeErr := file.Close()
	if writeErr != nil || syncErr != nil || closeErr != nil {
		return errRunStore
	}
	return s.syncDirectory(filepath.Dir(name))
}

func (s *runStore) syncDirectory(name string) error {
	dir, err := s.root.Open(name)
	if err != nil {
		return errRunStore
	}
	defer dir.Close()
	if err := dir.Sync(); err != nil {
		return errRunStore
	}
	return nil
}
