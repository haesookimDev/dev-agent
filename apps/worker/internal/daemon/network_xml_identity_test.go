package daemon

import (
	"strings"
	"testing"
)

func TestRunNetworkXMLIdentityRejectsAmbiguousEvidence(t *testing.T) {
	network, _ := allocateRunNetwork(networkTestRun, "10.240.0.0/24", nil, nil)
	expected, _ := network.definitionXML()
	for _, body := range []string{
		string(expected) + "<network/>", string(expected) + "trailing", "<!DOCTYPE network>" + string(expected),
		strings.Replace(string(expected), "<network ", "<network connections='0' connections='0' ", 1),
		strings.Replace(string(expected), "<network ", "<network connections='1' ", 1),
		strings.Replace(string(expected), "<network ", "<network unknown='yes' ", 1),
		strings.Replace(string(expected), "<name>", "<name>foreign</name><name>", 1),
		strings.Replace(string(expected), "<forward mode='nat'/>", "<forward mode='route'/>", 1),
		strings.Replace(string(expected), "version='1'", "version='2'", 1),
		strings.Replace(string(expected), "</network>", "<ip address='10.0.0.1' prefix='8'/></network>", 1),
		strings.Repeat(" ", (256<<10)+1),
	} {
		if matchesNetworkXML([]byte(body), expected, "network") {
			t.Fatal("accepted incomplete or modified ownership evidence")
		}
	}
	normalized := strings.Replace(string(expected), " ipv6='no'", "", 1)
	normalized = strings.Replace(normalized, " trustGuestRxFilters='no'", " connections='0'", 1)
	normalized = strings.Replace(normalized, " enable='yes'", "", 1)
	normalized = strings.Replace(normalized, " family='ipv4'", "", 1)
	if !matchesNetworkXML([]byte(normalized), expected, "network") {
		t.Fatal("rejected equivalent libvirt defaults")
	}
	active := strings.Replace(string(expected), "<forward mode='nat'/>", "<forward mode='nat'><nat><port start='1024' end='65535'/></nat></forward>", 1)
	if !matchesNetworkXML([]byte(active), expected, "network") {
		t.Fatal("rejected libvirt's exact active NAT port default")
	}
	for _, changed := range []string{
		strings.Replace(active, "start='1024'", "start='1023'", 1),
		strings.Replace(active, "<nat>", "<nat ipv6='yes'>", 1),
		strings.Replace(active, "</nat>", "<address start='10.0.0.1'/></nat>", 1),
	} {
		if matchesNetworkXML([]byte(changed), expected, "network") {
			t.Fatal("discarded a nondefault NAT change")
		}
	}
}
