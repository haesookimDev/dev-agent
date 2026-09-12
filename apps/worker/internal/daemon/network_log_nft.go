package daemon

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os/exec"
	"strconv"
	"syscall"
	"time"
)

type networkLogCommand func(context.Context, []byte, ...string) ([]byte, error)

func queryNetworkLog(ctx context.Context, input []byte, args ...string) ([]byte, error) {
	ctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	command := exec.CommandContext(ctx, "/usr/sbin/nft", args...)
	command.Env = []string{"PATH=/usr/sbin:/usr/bin:/sbin:/bin", "LANG=C", "LC_ALL=C"}
	command.Stdin = bytes.NewReader(input)
	command.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	command.Cancel = func() error { return syscall.Kill(-command.Process.Pid, syscall.SIGKILL) }
	command.WaitDelay = time.Second
	var output boundedVMOutput
	command.Stdout = &output
	// Never log process output, hook XML or arguments on error.
	if command.Run() != nil {
		return nil, errRunNetwork
	}
	return output.buffer.Bytes(), nil
}

func networkLogObjects(data []byte) ([]map[string]json.RawMessage, error) {
	if len(data) == 0 || len(data) > 256<<10 || !json.Valid(data) || validateWorkerRecoveryJSON(data) != nil {
		return nil, errRunNetwork
	}
	var document map[string]json.RawMessage
	var rows []map[string]json.RawMessage
	if json.Unmarshal(data, &document) != nil || len(document) != 1 ||
		json.Unmarshal(document["nftables"], &rows) != nil || rows == nil {
		return nil, errRunNetwork
	}
	var result []map[string]json.RawMessage
	for _, row := range rows {
		if len(row) != 1 {
			return nil, errRunNetwork
		}
		if _, metadata := row["metainfo"]; !metadata {
			result = append(result, row)
		}
	}
	return result, nil
}

// Absence is established by a successful bounded inventory, never by treating
// an arbitrary nft error (permissions, timeout, unavailable kernel) as ENOENT.
func networkLogPresent(ctx context.Context, query networkLogCommand, network runNetwork) (bool, error) {
	data, err := query(ctx, nil, "--json", "list", "tables", "inet")
	if err != nil {
		return false, errRunNetwork
	}
	rows, err := networkLogObjects(data)
	if err != nil {
		return false, err
	}
	seen := map[string]bool{}
	for _, row := range rows {
		var table struct {
			Family string `json:"family"`
			Name   string `json:"name"`
		}
		if json.Unmarshal(row["table"], &table) != nil || table.Family != "inet" || table.Name == "" || seen[table.Name] {
			return false, errRunNetwork
		}
		seen[table.Name] = true
	}
	return seen[networkLogTable(network)], nil
}

func networkLogSnapshot(ctx context.Context, query networkLogCommand, network runNetwork) (string, uint64, error) {
	if !network.valid(network.UUID) {
		return "", 0, errRunNetwork
	}
	data, err := query(ctx, nil, "--json", "--handle", "--numeric", "list", "table", "inet", networkLogTable(network))
	if err != nil {
		return "", 0, errRunNetwork
	}
	rows, err := networkLogObjects(data)
	if err != nil || len(rows) != 3 {
		return "", 0, errRunNetwork
	}
	var table struct {
		Family  string `json:"family"`
		Name    string `json:"name"`
		Handle  uint64 `json:"handle"`
		Comment string `json:"comment"`
	}
	if json.Unmarshal(rows[0]["table"], &table) != nil || table.Family != "inet" ||
		table.Name != networkLogTable(network) || table.Handle == 0 || table.Comment != "kelpie-egress-v1:"+network.UUID {
		return "", 0, errRunNetwork
	}
	var chain, rule struct {
		Handle uint64 `json:"handle"`
	}
	if json.Unmarshal(rows[1]["chain"], &chain) != nil || chain.Handle == 0 ||
		json.Unmarshal(rows[2]["rule"], &rule) != nil || rule.Handle == 0 {
		return "", 0, errRunNetwork
	}
	want, err := networkLogFingerprint(networkLogExpected(network, table.Handle, chain.Handle, rule.Handle))
	fingerprint, actualErr := networkLogFingerprint(data)
	if err != nil || actualErr != nil || fingerprint != want {
		return "", 0, errRunNetwork
	}
	return fingerprint, table.Handle, nil
}

// Exact nft 1.0.9 numeric JSON, verified against the supported Ubuntu host.
// Handles come from the kernel; all policy fields must match the generated
// plan. No dormant flag, extra expression or logging-payload option is ignored.
func networkLogExpected(network runNetwork, table, chain, rule uint64) []byte {
	return []byte(fmt.Sprintf(`{"nftables":[
{"table":{"family":"inet","name":"%[1]s","handle":%[5]d,"comment":"kelpie-egress-v1:%[2]s"}},
{"chain":{"family":"inet","table":"%[1]s","name":"egress","handle":%[6]d,"type":"filter","hook":"forward","prio":-10,"policy":"accept"}},
{"rule":{"family":"inet","table":"%[1]s","chain":"egress","handle":%[7]d,"expr":[
{"match":{"op":"==","left":{"meta":{"key":"iifname"}},"right":"%[3]s"}},
{"match":{"op":"==","left":{"payload":{"protocol":"ip","field":"saddr"}},"right":"%[4]s"}},
{"match":{"op":"in","left":{"ct":{"key":"state"}},"right":8}},
{"counter":{"packets":0,"bytes":0}},
{"log":{"prefix":"kelpie-egress:%[2]s ","level":"info"}}
]}}]}`, networkLogTable(network), network.UUID, network.Bridge, network.Guest, table, chain, rule))
}

func deleteNetworkLog(ctx context.Context, query networkLogCommand, handle uint64) error {
	if handle == 0 {
		return errRunNetwork
	}
	// Delete the recorded kernel handle, not a name that could have been
	// replaced between inspection and deletion by another administrator.
	_, err := query(ctx, nil, "delete", "table", "inet", "handle", strconv.FormatUint(handle, 10))
	return err
}
