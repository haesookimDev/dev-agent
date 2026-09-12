package daemon

import "fmt"

// Version 2 permits only outbound TCP to the recorded control endpoint and
// replies to those connections. No DNS, DHCP, internet, IPv6 or host-service
// exception is implicit. Static addressing avoids DHCP's broadcast exceptions.
// MAC/IP enforcement happens before connection tracking. Do not replace this
// with a shared filter whose later edits could broaden a recorded boundary.
// This is an IP:port boundary, not SNI/HTTP Host validation: the configured
// listener must serve only the control API, not a shared CDN or generic proxy.
func (network runNetwork) policyXML() ([]byte, error) {
	if !network.valid(network.UUID) {
		return nil, errRunNetwork
	}
	if network.Version == 1 {
		return network.quarantineXML()
	}
	control, err := parseGuestControl(network.ControlOrigin, network.ControlIPv4)
	if err != nil {
		return nil, err
	}
	return []byte(fmt.Sprintf(`<filter name='%s' chain='root'>
  <uuid>%s</uuid>
  <rule action='drop' direction='out' priority='-1000'><mac match='no' srcmacaddr='%s'/></rule>
  <rule action='drop' direction='in' priority='-1000'><mac match='no' srcmacaddr='%s'/></rule>
  <rule action='accept' direction='out' priority='-900'><arp hwtype='1' protocoltype='0x800' opcode='Request' arpsrcmacaddr='%s' arpsrcipaddr='%s' arpdstipaddr='%s'/></rule>
  <rule action='accept' direction='out' priority='-890'><arp hwtype='1' protocoltype='0x800' opcode='Reply' arpsrcmacaddr='%s' arpsrcipaddr='%s' arpdstmacaddr='%s' arpdstipaddr='%s'/></rule>
  <rule action='accept' direction='in' priority='-900'><arp hwtype='1' protocoltype='0x800' opcode='Request' arpsrcmacaddr='%s' arpsrcipaddr='%s' arpdstipaddr='%s'/></rule>
  <rule action='accept' direction='in' priority='-890'><arp hwtype='1' protocoltype='0x800' opcode='Reply' arpsrcmacaddr='%s' arpsrcipaddr='%s' arpdstmacaddr='%s' arpdstipaddr='%s'/></rule>
  <rule action='accept' direction='out' priority='-800'><ip srcipaddr='%s' dstipaddr='%s' dstmacaddr='%s' protocol='tcp' dstportstart='%d' dstportend='%d'/></rule>
  <rule action='accept' direction='in' priority='-800'><ip srcipaddr='%s' dstipaddr='%s' dstmacaddr='%s' protocol='tcp' srcportstart='%d' srcportend='%d'/></rule>
  <rule action='drop' direction='inout' priority='-700'><mac/></rule>
  <rule action='accept' direction='out' priority='0'><tcp srcipaddr='%s' dstipaddr='%s' dstportstart='%d' dstportend='%d'/></rule>
  <rule action='drop' direction='inout' priority='1000' statematch='false'><all/></rule>
</filter>
`, network.Filter, network.UUID, network.GuestMAC, network.GatewayMAC,
		network.GuestMAC, network.Guest, network.Gateway,
		network.GuestMAC, network.Guest, network.GatewayMAC, network.Gateway,
		network.GatewayMAC, network.Gateway, network.Guest,
		network.GatewayMAC, network.Gateway, network.GuestMAC, network.Guest,
		network.Guest, control.IPv4, network.GatewayMAC, control.Port, control.Port,
		control.IPv4, network.Guest, network.GuestMAC, control.Port, control.Port,
		network.Guest, control.IPv4, control.Port, control.Port)), nil
}

func (network runNetwork) guestNetworkConfig() ([]byte, error) {
	if !network.valid(network.UUID) || network.Version != 2 {
		return nil, errRunNetwork
	}
	return []byte(fmt.Sprintf("version: 2\nethernets:\n  run:\n    match:\n      macaddress: '%s'\n    set-name: kelpie0\n    dhcp4: false\n    dhcp6: false\n    accept-ra: false\n    link-local: []\n    addresses: ['%s/30']\n    routes:\n      - to: default\n        via: '%s'\n", network.GuestMAC, network.Guest, network.Gateway)), nil
}
