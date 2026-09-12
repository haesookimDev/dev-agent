package daemon

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

type logHookFixture struct {
	hook       *networkLogHook
	network    runNetwork
	present    bool
	body       []byte
	ready      bool
	createFail bool
	deletes    int
}

func newLogHookFixture(t *testing.T) *logHookFixture {
	t.Helper()
	path := t.TempDir()
	if err := os.Chmod(path, 0755); err != nil {
		t.Fatal(err)
	}
	hook, err := openNetworkLogHook(path, uint32(os.Geteuid()), networkTestRun)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(hook.close)
	f := &logHookFixture{hook: hook, network: logTestNetwork(t), ready: true}
	f.body = networkLogExpected(f.network, 123, 1, 2)
	hook.ready = func(context.Context) error {
		if !f.ready {
			return errRunNetwork
		}
		return nil
	}
	hook.query = func(_ context.Context, input []byte, args ...string) ([]byte, error) {
		switch {
		case reflect.DeepEqual(args, []string{"--json", "list", "tables", "inet"}):
			if !f.present {
				return []byte(`{"nftables":[]}`), nil
			}
			return []byte(`{"nftables":[{"table":{"family":"inet","name":"` + networkLogTable(f.network) + `"}}]}`), nil
		case reflect.DeepEqual(args, []string{"--file", "-"}):
			if _, exists, err := hook.read(f.network, "pending"); err != nil || !exists {
				t.Fatal("effect preceded durable ownership intent")
			}
			want, _ := networkLogRules(f.network)
			if string(input) != string(want) || f.present || f.createFail {
				return nil, errRunNetwork
			}
			f.present = true
			return nil, nil
		case reflect.DeepEqual(args, []string{"--json", "--handle", "--numeric", "list", "table", "inet", networkLogTable(f.network)}):
			if !f.present {
				return nil, errRunNetwork
			}
			return f.body, nil
		case reflect.DeepEqual(args, []string{"delete", "table", "inet", "handle", "123"}):
			f.deletes++
			f.present = false
			return nil, nil
		default:
			t.Fatal("unexpected privileged command")
			return nil, errRunNetwork
		}
	}
	return f
}

func (f *logHookFixture) apply(operation, phase string) error {
	return f.hook.apply(context.Background(), f.network, operation, phase)
}

func TestNetworkLogHookLifecycle(t *testing.T) {
	f := newLogHookFixture(t)
	if err := f.apply("start", "begin"); err != nil {
		t.Fatal(err)
	}
	for _, operation := range []string{"started", "port-created", "updated"} {
		if err := f.apply(operation, "begin"); err != nil {
			t.Fatal(err)
		}
	}
	if err := f.apply("start", "begin"); err == nil {
		t.Fatal("reused a network lifecycle")
	}
	f.body = []byte(strings.Replace(string(f.body), `"packets":0`, `"packets":9`, 1))
	if err := f.apply("port-created", "begin"); err != nil {
		t.Fatal("packet counters invalidated active ownership")
	}
	f.ready = false // teardown still works when journald is unavailable
	if err := f.apply("stopped", "end"); err != nil || f.present || f.deletes != 1 {
		t.Fatal("owned table not removed")
	}
	if err := f.apply("stopped", "end"); err != nil || f.deletes != 1 {
		t.Fatal("repeat stop was not idempotent")
	}
	if err := f.apply("port-created", "begin"); err == nil {
		t.Fatal("attached to stopped logging lifecycle")
	}
	f.present = true
	if err := f.apply("stopped", "end"); err == nil || f.deletes != 1 {
		t.Fatal("deleted a table that reappeared after cleanup")
	}
}

func TestNetworkLogHookRefusesMissingOrChangedLogging(t *testing.T) {
	for _, change := range []string{"journal", "table", "policy", "handle", "boot", "receipt"} {
		t.Run(change, func(t *testing.T) {
			f := newLogHookFixture(t)
			if err := f.apply("start", "begin"); err != nil {
				t.Fatal(err)
			}
			switch change {
			case "journal":
				f.ready = false
			case "table":
				f.present = false
			case "policy":
				f.body = []byte(strings.Replace(string(f.body), `"prio":-10`, `"prio":10`, 1))
			case "handle":
				f.body = []byte(strings.Replace(string(f.body), `"handle":123`, `"handle":124`, 1))
			case "boot":
				f.hook.bootID = "ea065d7b-6720-4c3e-9557-cf5bdb6995d0"
			case "receipt":
				if err := f.hook.root.Remove(f.network.UUID + ".active.json"); err != nil {
					t.Fatal(err)
				}
			}
			if err := f.apply("port-created", "begin"); err == nil {
				t.Fatal("unconfirmed logger allowed NIC attachment")
			}
			if change != "journal" && change != "table" {
				if err := f.apply("stopped", "end"); err == nil || f.deletes != 0 {
					t.Fatal("ambiguous ownership authorized deletion")
				}
			}
		})
	}
}

func TestNetworkLogHookStartFailsClosed(t *testing.T) {
	for _, change := range []string{"journal", "collision", "creation", "partial"} {
		t.Run(change, func(t *testing.T) {
			f := newLogHookFixture(t)
			switch change {
			case "journal":
				f.ready = false
			case "collision":
				f.present = true
			case "creation":
				f.createFail = true
			case "partial":
				f.body = []byte(`{"nftables":[]}`)
			}
			if err := f.apply("start", "begin"); err == nil {
				t.Fatal("failed logging setup accepted")
			}
			if _, exists, err := f.hook.read(f.network, "active"); err != nil || exists {
				t.Fatal("failed start wrote active receipt")
			}
			if f.deletes != 0 {
				t.Fatal("failure blindly deleted a collided/unknown resource")
			}
		})
	}
}

func TestNetworkLogHookStoreRejectsUnsafeRecords(t *testing.T) {
	for _, change := range []string{"symlink", "hardlink", "writable", "partial", "alias"} {
		t.Run(change, func(t *testing.T) {
			f := newLogHookFixture(t)
			if err := f.apply("start", "begin"); err != nil {
				t.Fatal(err)
			}
			path := filepath.Join(f.hook.root.Name(), f.network.UUID+".active.json")
			switch change {
			case "symlink":
				if err := os.Rename(path, path+".original"); err != nil {
					t.Fatal(err)
				}
				if err := os.Symlink(path+".original", path); err != nil {
					t.Fatal(err)
				}
			case "hardlink":
				if err := os.Link(path, path+".link"); err != nil {
					t.Fatal(err)
				}
			case "writable":
				if err := os.Chmod(path, 0666); err != nil {
					t.Fatal(err)
				}
			case "partial", "alias":
				data := []byte(`{`)
				if change == "alias" {
					body, err := os.ReadFile(path)
					if err != nil {
						t.Fatal(err)
					}
					data = []byte(strings.Replace(string(body), `"version":1`, `"Version":1`, 1))
				}
				if err := os.WriteFile(path, data, 0644); err != nil {
					t.Fatal(err)
				}
			}
			if err := f.apply("port-created", "begin"); err == nil {
				t.Fatal("unsafe record granted attachment")
			}
			if err := f.apply("stopped", "end"); err == nil || f.deletes != 0 {
				t.Fatal("unsafe record granted deletion")
			}
		})
	}
}

func TestNetworkLogHookStoreSerializesOwners(t *testing.T) {
	f := newLogHookFixture(t)
	other, err := openNetworkLogHook(f.hook.root.Name(), f.hook.owner, f.hook.bootID)
	if err == nil {
		other.close()
		t.Fatal("two hooks acquired the same store")
	}
}

func TestNetworkLogHookRejectsInvalidOperationsWithoutEffects(t *testing.T) {
	f := newLogHookFixture(t)
	for _, args := range [][2]string{{"start", "end"}, {"stopped", "begin"}, {"unknown", "begin"}} {
		if err := f.apply(args[0], args[1]); err == nil || f.present || f.deletes != 0 {
			t.Fatal("invalid hook phase caused effects")
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := f.hook.apply(ctx, f.network, "start", "begin"); err == nil {
		t.Fatal("cancelled hook applied effects")
	}
	if err := RunNetworkLogHook(context.Background(), []string{"default", "start", "begin", "-"}, strings.NewReader("foreign")); err != nil {
		t.Fatal("unrelated network hook was intercepted")
	}
	if err := RunNetworkLogHook(context.Background(), []string{}, strings.NewReader("")); !errors.Is(err, errRunNetwork) {
		t.Fatal("invalid arguments accepted")
	}
}
