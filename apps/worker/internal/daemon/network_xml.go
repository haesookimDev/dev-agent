package daemon

import "fmt"

// Templates accept only a fully reconstructed identity. They are not a launch
// API: ownership persistence, host preflight and cleanup must precede adoption.
func (network runNetwork) definitionXML() ([]byte, error) {
	if !network.valid(network.UUID) {
		return nil, errRunNetwork
	}
	return []byte(fmt.Sprintf(`<network ipv6='no' trustGuestRxFilters='no'>
  <name>%s</name>
  <uuid>%s</uuid>
  <metadata>
    <kelpie:owner xmlns:kelpie='urn:kelpie:network:v1' run='%s' version='1'/>
  </metadata>
  <forward mode='nat'/>
  <bridge name='%s' stp='off' delay='0'/>
  <mac address='%s'/>
  <port isolated='yes'/>
  <dns enable='yes' forwardPlainNames='no'/>
  <ip family='ipv4' address='%s' prefix='30'>
    <dhcp>
      <host mac='%s' name='kelpie-run' ip='%s'/>
    </dhcp>
  </ip>
</network>
`, network.Name, network.UUID, network.UUID, network.Bridge, network.GatewayMAC,
		network.Gateway, network.GuestMAC, network.Guest)), nil
}

// Quarantine starts at Ethernet, not merely IPv4: no ARP, DHCP, IPv6, VLAN,
// established traffic or referenced host filter can grant an exception here.
// A real NIC must attach this filter before a reviewed egress policy replaces
// it; generating/defining the XML alone is not proof of packet enforcement.
func (network runNetwork) quarantineXML() ([]byte, error) {
	if !network.valid(network.UUID) {
		return nil, errRunNetwork
	}
	return []byte(fmt.Sprintf(`<filter name='%s' chain='root'>
  <uuid>%s</uuid>
  <rule action='drop' direction='inout' priority='-1000'>
    <mac/>
  </rule>
</filter>
`, network.Filter, network.UUID)), nil
}
