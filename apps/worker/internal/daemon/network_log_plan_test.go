package daemon

import (
	"strings"
	"testing"
)

func logTestNetwork(t *testing.T) runNetwork {
	t.Helper()
	network, err := allocateRunNetwork(networkTestRun, "10.240.0.0/24", nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	return network
}

func TestNetworkLogIdentityRequiresEntireOwnedNetwork(t *testing.T) {
	network := logTestNetwork(t)
	body, _ := network.definitionXML()
	for _, data := range []string{string(body), strings.Replace(string(body), "<network ", "<network connections='1' ", 1)} {
		parsed, err := networkLogIdentity(network.Name, []byte("<hookData>"+data+"</hookData>"))
		if err != nil || parsed != network {
			t.Fatal("exact managed network rejected")
		}
	}
	for _, bad := range []string{
		strings.Replace(string(body), network.Bridge, "eth0", 1),
		strings.Replace(string(body), "prefix='30'", "prefix='24'", 1),
		strings.Replace(string(body), "<network ", "<network connections='2' ", 1),
		strings.Replace(string(body), "mode='nat'", "mode='open'", 1),
		strings.Replace(string(body), "</network>", "<route address='0.0.0.0' prefix='0'/></network>", 1),
		strings.Replace(string(body), "</network>", "<uuid>duplicate</uuid></network>", 1),
		strings.Replace(string(body), "prefix='30'", "prefix='30' prefix='30'", 1),
		string(body) + string(body),
	} {
		if _, err := networkLogIdentity(network.Name, []byte("<hookData>"+bad+"</hookData>")); err == nil {
			t.Fatal("non-owned or widened network accepted by privileged log planner")
		}
	}
}

func TestNetworkLoggingDoesNotGrantTrafficOrLogPayload(t *testing.T) {
	network := logTestNetwork(t)
	rules, err := networkLogRules(network)
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"create table inet kelpie_log_", "iifname \"" + network.Bridge + "\"", "ip saddr " + network.Guest,
		"ct state new counter log prefix \"kelpie-egress:" + network.UUID, "hook forward priority -10"} {
		if !strings.Contains(string(rules), want) {
			t.Fatal("logging lost its exact network or flow scope")
		}
	}
	for _, bad := range []string{"flush", " flags ", "snaplen", "payload", " jump ", "ct mark", "eth0"} {
		if strings.Contains(string(rules), bad) {
			t.Fatal("log rules touch other policy or sensitive packet contents")
		}
	}
	network.Bridge = "foreign"
	if _, err := networkLogRules(network); err == nil {
		t.Fatal("foreign bridge accepted")
	}
}

const logFingerprintFixture = `{"nftables":[
 {"metainfo":{"version":"1.0.9"}},
 {"table":{"family":"inet","name":"owned","handle":1}},
 {"chain":{"family":"inet","table":"owned","name":"egress","handle":2,"policy":"accept"}},
 {"rule":{"family":"inet","table":"owned","chain":"egress","handle":3,"expr":[
 {"counter":{"packets":7,"bytes":420}}, {"log":{"prefix":"owned "}}]}}
]}`

func TestNetworkLogFingerprintIgnoresOnlyCountersAndFormatting(t *testing.T) {
	want, err := networkLogFingerprint([]byte(logFingerprintFixture))
	if err != nil {
		t.Fatal(err)
	}
	for _, equivalent := range []string{
		strings.Replace(logFingerprintFixture, `"packets":7,"bytes":420`, `"bytes":9007199254740993,"packets":10`, 1),
		strings.Replace(logFingerprintFixture, `"family":"inet","name":"owned","handle":1`, `"handle":1, "name":"owned", "family":"inet"`, 1),
		strings.Replace(logFingerprintFixture, `"version":"1.0.9"`, `"version":"different"`, 1),
	} {
		got, err := networkLogFingerprint([]byte(equivalent))
		if err != nil || got != want {
			t.Fatal("counter or JSON formatting changed ownership fingerprint")
		}
	}
	for _, changed := range []string{
		strings.Replace(logFingerprintFixture, `"handle":1`, `"handle":4`, 1),
		strings.Replace(logFingerprintFixture, `"owned "`, `"foreign "`, 1),
		strings.Replace(logFingerprintFixture, `"policy":"accept"`, `"policy":"drop"`, 1),
		strings.Replace(logFingerprintFixture, `"handle":1`, `"handle":9007199254740992`, 1),
	} {
		got, err := networkLogFingerprint([]byte(changed))
		if err != nil || got == want {
			t.Fatal("modified table accepted as the original table")
		}
	}
}

func TestNetworkLogFingerprintRejectsAmbiguousJSON(t *testing.T) {
	for _, bad := range []string{
		strings.Replace(logFingerprintFixture, `"handle":1`, `"handle":2,"handle":1`, 1),
		strings.Replace(logFingerprintFixture, `"packets":7`, `"packets":5,"packets":7`, 1),
		strings.Replace(logFingerprintFixture, `"packets":7`, `"packets":-1`, 1),
		strings.Replace(logFingerprintFixture, `"packets":7`, `"packets":0.5`, 1),
		strings.Replace(logFingerprintFixture, `"packets":7`, `"packets":null`, 1),
		logFingerprintFixture + `{}`,
		`{"nftables":null}`, `{}`, strings.Repeat(" ", 256<<10+1),
	} {
		if _, err := networkLogFingerprint([]byte(bad)); err == nil {
			t.Fatal("ambiguous or invalid nft output accepted")
		}
	}
}
