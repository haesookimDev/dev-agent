package daemon

import (
	"bytes"
	"encoding/xml"
	"errors"
	"strings"
	"testing"
)

func TestRunNetworkXMLPreservesCanonicalBoundary(t *testing.T) {
	network, err := allocateRunNetwork(networkTestRun, "10.240.0.0/24", nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	body, err := network.definitionXML()
	if err != nil {
		t.Fatal(err)
	}
	var definition struct {
		XMLName xml.Name
		IPv6    string `xml:"ipv6,attr"`
		Trust   string `xml:"trustGuestRxFilters,attr"`
		Name    string `xml:"name"`
		UUID    string `xml:"uuid"`
		Owner   struct {
			XMLName xml.Name
			Run     string `xml:"run,attr"`
			Version string `xml:"version,attr"`
		} `xml:"metadata>owner"`
		Bridge struct {
			Name string `xml:"name,attr"`
		} `xml:"bridge"`
		MAC struct {
			Address string `xml:"address,attr"`
		} `xml:"mac"`
		Port struct {
			Isolated string `xml:"isolated,attr"`
		} `xml:"port"`
		Forward struct {
			Mode string `xml:"mode,attr"`
		} `xml:"forward"`
		DNS struct {
			Enable            string `xml:"enable,attr"`
			ForwardPlainNames string `xml:"forwardPlainNames,attr"`
		} `xml:"dns"`
		IP []struct {
			Family  string `xml:"family,attr"`
			Address string `xml:"address,attr"`
			Prefix  int    `xml:"prefix,attr"`
			Host    []struct {
				MAC string `xml:"mac,attr"`
				IP  string `xml:"ip,attr"`
			} `xml:"dhcp>host"`
			Ranges []struct{} `xml:"dhcp>range"`
		} `xml:"ip"`
	}
	if xml.Unmarshal(body, &definition) != nil || definition.XMLName.Local != "network" ||
		definition.IPv6 != "no" || definition.Trust != "no" || definition.Name != network.Name ||
		definition.UUID != network.UUID || definition.Owner.Run != network.UUID || definition.Owner.Version != "1" ||
		definition.Owner.XMLName.Space != "urn:kelpie:network:v1" || definition.Bridge.Name != network.Bridge ||
		definition.MAC.Address != network.GatewayMAC || definition.Port.Isolated != "yes" || definition.Forward.Mode != "nat" ||
		definition.DNS.Enable != "yes" || definition.DNS.ForwardPlainNames != "no" {
		t.Fatal("network boundary or ownership metadata changed")
	}
	if len(definition.IP) != 1 || definition.IP[0].Family != "ipv4" || definition.IP[0].Address != network.Gateway ||
		definition.IP[0].Prefix != 30 || len(definition.IP[0].Host) != 1 || len(definition.IP[0].Ranges) != 0 ||
		definition.IP[0].Host[0].MAC != network.GuestMAC || definition.IP[0].Host[0].IP != network.Guest {
		t.Fatal("DHCP allocation is not limited to the exact guest")
	}
	for _, forbidden := range []string{"<tftp", "<route", "<virtualport", "<interface", "<autostart", "dnsmasq:", "network='default'"} {
		if strings.Contains(string(body), forbidden) {
			t.Fatalf("unexpected connectivity configuration: %s", forbidden)
		}
	}
	again, _ := network.definitionXML()
	if !bytes.Equal(body, again) {
		t.Fatal("network XML changed for the same identity")
	}
}

func TestRunNetworkQuarantineHasNoExceptions(t *testing.T) {
	network, _ := allocateRunNetwork(networkTestRun, "10.240.0.0/24", nil, nil)
	body, err := network.quarantineXML()
	if err != nil {
		t.Fatal(err)
	}
	var filter struct {
		XMLName xml.Name
		Name    string     `xml:"name,attr"`
		Chain   string     `xml:"chain,attr"`
		UUID    string     `xml:"uuid"`
		Refs    []struct{} `xml:"filterref"`
		Rules   []struct {
			Action    string `xml:"action,attr"`
			Direction string `xml:"direction,attr"`
			Priority  int    `xml:"priority,attr"`
			MAC       *struct {
				Attributes []xml.Attr `xml:",any,attr"`
			} `xml:"mac"`
		} `xml:"rule"`
	}
	if xml.Unmarshal(body, &filter) != nil || filter.XMLName.Local != "filter" || filter.Name != network.Filter ||
		filter.Chain != "root" || filter.UUID != network.UUID || len(filter.Refs) != 0 || len(filter.Rules) != 1 {
		t.Fatal("quarantine has unexpected identity or rule composition")
	}
	rule := filter.Rules[0]
	if rule.Action != "drop" || rule.Direction != "inout" || rule.Priority != -1000 || rule.MAC == nil || len(rule.MAC.Attributes) != 0 {
		t.Fatal("quarantine does not unconditionally drop both Ethernet directions")
	}
}

func TestRunNetworkXMLRejectsTampering(t *testing.T) {
	network, _ := allocateRunNetwork(networkTestRun, "10.240.0.0/24", nil, nil)
	network.Name = "injected'><forward mode='bridge'/><name>"
	for _, generate := range []func() ([]byte, error){network.definitionXML, network.quarantineXML, network.interfaceXML} {
		body, err := generate()
		if !errors.Is(err, errRunNetwork) || body != nil {
			t.Fatal("generated XML from a noncanonical identity")
		}
	}
}

func TestRunNetworkInterfacePinsQuarantineAndAddress(t *testing.T) {
	network, _ := allocateRunNetwork(networkTestRun, "10.240.0.0/24", nil, nil)
	body, err := network.interfaceXML()
	if err != nil {
		t.Fatal(err)
	}
	var nic struct {
		Type  string `xml:"type,attr"`
		Trust string `xml:"trustGuestRxFilters,attr"`
		MAC   struct {
			Address string `xml:"address,attr"`
		} `xml:"mac"`
		Source struct {
			Network string `xml:"network,attr"`
		} `xml:"source"`
		Model struct {
			Type string `xml:"type,attr"`
		} `xml:"model"`
		Filter struct {
			Name       string `xml:"filter,attr"`
			Parameters []struct {
				Name  string `xml:"name,attr"`
				Value string `xml:"value,attr"`
			} `xml:"parameter"`
		} `xml:"filterref"`
	}
	if xml.Unmarshal(body, &nic) != nil || nic.Type != "network" || nic.Trust != "no" ||
		nic.MAC.Address != network.GuestMAC || nic.Source.Network != network.Name ||
		nic.Model.Type != "virtio" || nic.Filter.Name != network.Filter || len(nic.Filter.Parameters) != 2 {
		t.Fatal("NIC escaped the recorded network/quarantine boundary")
	}
	parameters := map[string]string{}
	for _, parameter := range nic.Filter.Parameters {
		parameters[parameter.Name] = parameter.Value
	}
	if parameters["IP"] != network.Guest || parameters["CTRL_IP_LEARNING"] != "none" {
		t.Fatal("NIC permits implicit guest IP learning")
	}
	again, _ := network.interfaceXML()
	if !bytes.Equal(body, again) {
		t.Fatal("NIC XML changed for the same recorded identity")
	}
}
