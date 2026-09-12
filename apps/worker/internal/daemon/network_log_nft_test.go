package daemon

import (
	"context"
	"errors"
	"reflect"
	"strings"
	"testing"
)

func TestNetworkLogSnapshotRequiresExactLoggingPolicy(t *testing.T) {
	network := logTestNetwork(t)
	body := string(networkLogExpected(network, 123, 1, 2))
	check := func(data string) error {
		query := func(_ context.Context, input []byte, args ...string) ([]byte, error) {
			if len(input) != 0 || !reflect.DeepEqual(args, []string{"--json", "--handle", "--numeric", "list", "table", "inet", networkLogTable(network)}) {
				t.Fatal("snapshot command escaped exact table")
			}
			return []byte(data), nil
		}
		_, _, err := networkLogSnapshot(context.Background(), query, network)
		return err
	}
	if err := check(body); err != nil {
		t.Fatal("expected table rejected")
	}
	for _, changed := range []string{
		strings.Replace(body, `"handle":123`, `"handle":123,"flags":["dormant"]`, 1),
		strings.Replace(body, `"prio":-10`, `"prio":10`, 1),
		strings.Replace(body, `"right":8`, `"right":2`, 1),
		strings.Replace(body, `"level":"info"`, `"level":"info","flags":["all"]`, 1),
		strings.Replace(body, network.Bridge, "eth0", 1),
		strings.Replace(body, `"handle":123`, `"handle":0`, 1),
		strings.Replace(body, `"handle":123`, `"handle":123,"handle":123`, 1),
	} {
		if check(changed) == nil {
			t.Fatal("modified or ambiguous logging policy accepted")
		}
	}
}

func TestNetworkLogAbsenceRequiresSuccessfulInventory(t *testing.T) {
	network := logTestNetwork(t)
	for _, data := range []string{"", `{}`, `{"nftables":null}`, `{"nftables":[{"table":{"family":"inet","name":"x"}},{"table":{"family":"inet","name":"x"}}]}`} {
		query := func(context.Context, []byte, ...string) ([]byte, error) { return []byte(data), nil }
		if _, err := networkLogPresent(context.Background(), query, network); err == nil {
			t.Fatal("invalid inventory certified absence")
		}
	}
	query := func(context.Context, []byte, ...string) ([]byte, error) { return nil, errors.New("failure") }
	if _, err := networkLogPresent(context.Background(), query, network); err == nil {
		t.Fatal("query failure certified absence")
	}
	query = func(context.Context, []byte, ...string) ([]byte, error) { return []byte(`{"nftables":[]}`), nil }
	if present, err := networkLogPresent(context.Background(), query, network); err != nil || present {
		t.Fatal("empty successful inventory rejected")
	}
}

func TestNetworkLogDeletionUsesRecordedHandle(t *testing.T) {
	query := func(_ context.Context, input []byte, args ...string) ([]byte, error) {
		if len(input) != 0 || !reflect.DeepEqual(args, []string{"delete", "table", "inet", "handle", "9007199254740993"}) {
			t.Fatal("deletion did not use the exact kernel handle")
		}
		return nil, nil
	}
	if err := deleteNetworkLog(context.Background(), query, 9007199254740993); err != nil {
		t.Fatal(err)
	}
	if err := deleteNetworkLog(context.Background(), query, 0); err == nil {
		t.Fatal("invalid handle accepted")
	}
}
