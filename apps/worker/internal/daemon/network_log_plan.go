package daemon

import (
	"bytes"
	"crypto/sha256"
	"encoding/json"
	"encoding/xml"
	"fmt"
	"net/netip"
	"reflect"
	"slices"
	"strconv"
	"strings"
)

// The privileged libvirt hook receives XML, not a Worker credential or a shell
// command. Reconstruct the entire known network before authorizing any effect.
// This logging plan grants no traffic: the existing per-VM filter still decides
// what may leave. Never call libvirt from its synchronous hook (deadlock).
func networkLogIdentity(name string, data []byte) (runNetwork, error) {
	root, err := parseNetworkXML(data, "hookData")
	if err != nil || len(root.Attrs) != 0 || root.Text != "" || len(root.Children) < 1 || len(root.Children) > 2 {
		return runNetwork{}, errRunNetwork
	}
	if len(root.Children) == 2 && root.Children[1].Name != (xml.Name{Local: "networkport"}) {
		return runNetwork{}, errRunNetwork
	}
	node := root.Children[0]
	if node.Name != (xml.Name{Local: "network"}) || node.child("uuid") == nil || node.child("ip") == nil {
		return runNetwork{}, errRunNetwork
	}
	ip := node.child("ip")
	address, err := netip.ParseAddr(ip.attr("address"))
	if err != nil || !address.Is4() || ip.attr("prefix") != "30" {
		return runNetwork{}, errRunNetwork
	}
	network, err := networkAt(node.child("uuid").Text, netip.PrefixFrom(address, 30).Masked())
	if err != nil || network.Name != name {
		return runNetwork{}, errRunNetwork
	}
	// A single-guest network has at most one live port. Connection counts are
	// dynamic hook data, not an excuse to ignore unknown XML or shared networks.
	if count := node.attr("connections"); count != "" && count != "0" && count != "1" {
		return runNetwork{}, errRunNetwork
	}
	node.Attrs = slices.DeleteFunc(node.Attrs, func(a xml.Attr) bool {
		return a.Name == (xml.Name{Local: "connections"})
	})
	expected, err := network.definitionXML()
	if err != nil {
		return runNetwork{}, err
	}
	want, err := parseNetworkXML(expected, "network")
	if err != nil {
		return runNetwork{}, err
	}
	normalizeNetworkXML(node)
	normalizeNetworkXML(want)
	if !reflect.DeepEqual(node, want) {
		return runNetwork{}, errRunNetwork
	}
	return network, nil
}

func networkLogTable(network runNetwork) string {
	return "kelpie_log_" + strings.ReplaceAll(network.UUID, "-", "")
}

func networkLogRules(network runNetwork) ([]byte, error) {
	if !network.valid(network.UUID) {
		return nil, errRunNetwork
	}
	// No global flush, accept/bypass verdict, payload, DNS query, HTTP URL,
	// token or TCP options. Retransmitted NEW packets may yield duplicate flow
	// records; counters and logging records are different pieces of evidence.
	// Keep explicit commands in one nft transaction. nft 1.0.9 accepts a
	// nested create-table block but creates only the table, silently omitting
	// its chain/rule. A successful process alone is therefore not readiness.
	return []byte(fmt.Sprintf(`create table inet %[1]s { comment "kelpie-egress-v1:%[2]s"; }
add chain inet %[1]s egress { type filter hook forward priority -10; policy accept; }
add rule inet %[1]s egress iifname "%[3]s" ip saddr %[4]s ct state new counter log prefix "kelpie-egress:%[2]s " level info
`, networkLogTable(network), network.UUID, network.Bridge, network.Guest)), nil
}

// Only counters vary during a table's lifetime. Preserve handles and every
// other field: a changed/recreated table is not adopted or deleted merely
// because it has the same name. nft's version metadata is not table identity.
func networkLogFingerprint(data []byte) (string, error) {
	if len(data) == 0 || len(data) > 256<<10 {
		return "", errRunNetwork
	}
	// UseNumber preserves handles larger than 2^53; recursively decoding also
	// canonicalizes object key order. Reject duplicate keys before decoding.
	var document map[string]any
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.UseNumber()
	if decoder.Decode(&document) != nil || len(document) != 1 || validateWorkerRecoveryJSON(data) != nil {
		return "", errRunNetwork
	}
	rows, ok := document["nftables"].([]any)
	if !ok {
		return "", errRunNetwork
	}
	var objects []map[string]any
	for _, value := range rows {
		row, ok := value.(map[string]any)
		if !ok || len(row) != 1 {
			return "", errRunNetwork
		}
		if _, metadata := row["metainfo"]; metadata {
			continue
		}
		if raw, rule := row["rule"]; rule {
			body, ok := raw.(map[string]any)
			if !ok {
				return "", errRunNetwork
			}
			expressions, ok := body["expr"].([]any)
			if !ok {
				return "", errRunNetwork
			}
			for _, value := range expressions {
				expr, ok := value.(map[string]any)
				if !ok || len(expr) != 1 {
					return "", errRunNetwork
				}
				if counter, exists := expr["counter"]; exists {
					values, ok := counter.(map[string]any)
					if !ok || len(values) != 2 {
						return "", errRunNetwork
					}
					for _, key := range []string{"packets", "bytes"} {
						number, ok := values[key].(json.Number)
						if !ok {
							return "", errRunNetwork
						}
						if _, err := strconv.ParseUint(string(number), 10, 64); err != nil {
							return "", errRunNetwork
						}
						values[key] = json.Number("0")
					}
				}
			}
		}
		objects = append(objects, row)
	}
	if len(objects) != 3 || objects[0]["table"] == nil || objects[1]["chain"] == nil || objects[2]["rule"] == nil {
		return "", errRunNetwork
	}
	canonical, err := json.Marshal(objects)
	if err != nil {
		return "", errRunNetwork
	}
	return fmt.Sprintf("%x", sha256.Sum256(canonical)), nil
}
