//go:build linux && libvirt_integration

package daemon

import (
	"context"
	"os"
	"regexp"
	"strconv"
	"strings"
	"testing"
	"time"
)

// Public cloning requires both the disposable-host acknowledgement and an
// explicit public-egress mode, in addition to the control TLS test endpoint.
func TestDedicatedLibvirtInternetClone(t *testing.T) {
	if os.Getenv("KELPIE_LIBVIRT_TEST_ACK") != "disposable-host-only" || os.Getenv("KELPIE_GUEST_INTERNET") != "logged-public-ipv4" {
		t.Skip("requires disposable host, installed root hook and explicit public internet settings")
	}
	control, err := parseGuestControl(os.Getenv("KELPIE_LIBVIRT_CONTROL_ORIGIN"), os.Getenv("KELPIE_LIBVIRT_CONTROL_IPV4"))
	if err != nil {
		t.Fatal(err)
	}
	config := Config{GuestInternet: os.Getenv("KELPIE_GUEST_INTERNET"), GuestDNSIPv4: os.Getenv("KELPIE_GUEST_DNS_IPV4"), GuestDeniedIPv4: os.Getenv("KELPIE_GUEST_DENIED_IPV4")}
	internet, err := config.internetPolicy()
	if err != nil || internet == nil {
		t.Fatal("explicit internet policy missing")
	}
	dedicatedNetworkPackets(t, &control, true, internet)
}

func packetInternetClone(t *testing.T, ctx context.Context, record runRecord) string {
	t.Helper()
	output := packetGuestExecWithin(t, ctx, 45*time.Second, record.RunID, "/usr/bin/python3", "-c", `
import os, pathlib, re, socket, subprocess, tempfile
addresses = socket.getaddrinfo('github.com', 443, socket.AF_INET, socket.SOCK_STREAM)
assert addresses, 'DNS lookup failed'
destination = addresses[0][4][0]
# No ambient credentials, Git configuration, hooks or code execution from the
# public fixture. It is cloned only into a fresh directory in this owned VM.
with tempfile.TemporaryDirectory(prefix='kelpie-public-clone-') as directory:
    env = {'PATH': '/usr/bin:/bin', 'HOME': directory, 'LANG': 'C',
           'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
           'GIT_TERMINAL_PROMPT': '0'}
    target = pathlib.Path(directory) / 'repository'
    # Pin this DNS result for the fixture only, retaining TLS hostname checks.
    # The observer must find this exact destination, not some other HTTPS flow.
    result = subprocess.run(['/usr/bin/git', '-c', 'http.curloptResolve=github.com:443:' + destination,
        '-c', 'http.followRedirects=false', 'clone', '--depth=1', '--',
        'https://github.com/octocat/Hello-World.git', str(target)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    assert result.returncode == 0, 'public clone failed'
    sha = subprocess.check_output(['/usr/bin/git', '-C', str(target), 'rev-parse', 'HEAD'],
        env=env, stderr=subprocess.DEVNULL, timeout=3).decode().strip()
    assert re.fullmatch('[0-9a-f]{40}', sha)
    assert (target / 'README').is_file()
print('public-dns=verified repository-clone=verified clone-ip=' + destination + ' no-repository-code-executed')
`)
	destination, prefix := strings.CutPrefix(strings.TrimSpace(string(output)), "public-dns=verified repository-clone=verified clone-ip=")
	destination, suffix := strings.CutSuffix(destination, " no-repository-code-executed")
	if !prefix || !suffix || !publicGuestIPv4(destination) || destination == record.Network.ControlIPv4 {
		t.Fatal("actual DNS/clone checks incomplete")
	}
	t.Log("actual guest resolved public DNS and cloned the public HTTPS repository; no credentials or repository code executed")
	return destination
}

func packetInternetDropCounter(t *testing.T, ctx context.Context, chain string) uint64 {
	t.Helper()
	data := packetHostQuery(t, ctx, "/usr/bin/sudo", "-n", "/usr/sbin/ebtables", "-t", "nat", "-L", chain, "--Lc")
	if !strings.Contains(string(data), "Bridge chain: "+chain+", entries: ") {
		t.Fatal("exact TAP chain unavailable")
	}
	matches := regexp.MustCompile(`(?m)^.*-j DROP\s*,\s*pcnt\s*=\s*([0-9]+)\s*--\s*bcnt\s*=\s*[0-9]+\s*$`).FindAllSubmatch(data, -1)
	if len(matches) == 0 {
		t.Fatal("no deny counters in the exact TAP chain")
	}
	var total uint64
	for _, match := range matches {
		value, err := strconv.ParseUint(string(match[1]), 10, 64)
		if err != nil || total+value < total {
			t.Fatal("invalid deny counter")
		}
		total += value
	}
	return total
}

func packetInternetSpoofing(t *testing.T, ctx context.Context, record runRecord, tap string) {
	t.Helper()
	network := record.Network
	for _, direction := range []string{"out", "in"} {
		chain := "libvirt-I-" + tap
		device, srcMAC, dstMAC, srcIP, dstIP := "kelpie0", network.GuestMAC, network.GatewayMAC, network.Guest, network.DNSIPv4
		if direction == "in" {
			chain = "libvirt-O-" + tap
			device, srcMAC, dstMAC, srcIP, dstIP = network.Bridge, network.GatewayMAC, network.GuestMAC, network.DNSIPv4, network.Guest
		}
		before := packetInternetDropCounter(t, ctx, chain)
		args := []string{"-c", packetInternetSpoofFrames, device, srcMAC, dstMAC, srcIP, dstIP, direction}
		var result []byte
		if direction == "out" {
			result = packetGuestExec(t, ctx, record.RunID, "/usr/bin/python3", args...)
		} else {
			result = packetHostQuery(t, ctx, "/usr/bin/sudo", append([]string{"-n", "/usr/bin/python3"}, args...)...)
		}
		after := packetInternetDropCounter(t, ctx, chain)
		if strings.TrimSpace(string(result)) != "forbidden-frames=6" || after < before+6 {
			t.Fatalf("%s spoof/protocol frames not accounted for by TAP deny counters: %d -> %d", direction, before, after)
		}
		t.Logf("actual %s raw IP/MAC/ARP/VLAN/IPv6 boundary probes rejected; TAP DROP %d -> %d", direction, before, after)
	}
	// Unlike the six pre-conntrack rejects, this frame has correct L2 and
	// public L3 identities but no guest-initiated connection. Require the
	// NAT network's bridge-specific inbound rejection to account for it.
	// Routed ingress is rejected there before the bridged FO-TAP chain runs.
	before := packetInternetIngressReject(t, ctx, network.Bridge)
	packetHostQuery(t, ctx, "/usr/bin/sudo", "-n", "/usr/bin/python3", "-c", packetInternetUnsolicited,
		network.UUID, network.DNSIPv4, network.Guest)
	after := packetInternetIngressReject(t, ctx, network.Bridge)
	if after < before+1 {
		t.Fatalf("unsolicited inbound packet was not rejected by conntrack: %d -> %d", before, after)
	}
	t.Logf("actual unsolicited inbound UDP rejected by the exact owned NAT bridge rule; REJECT %d -> %d", before, after)
}

func packetInternetIngressReject(t *testing.T, ctx context.Context, bridge string) uint64 {
	t.Helper()
	data := packetHostQuery(t, ctx, "/usr/bin/sudo", "-n", "/usr/sbin/iptables", "-w", "2", "-t", "filter", "-L", "LIBVIRT_FWI", "-n", "-v", "-x")
	match := regexp.MustCompile(`(?m)^\s*([0-9]+)\s+[0-9]+\s+REJECT\s+0\s+--\s+\*\s+`+regexp.QuoteMeta(bridge)+`\s+0\.0\.0\.0/0\s+0\.0\.0\.0/0\s+reject-with icmp-port-unreachable\s*$`).FindAllSubmatch(data, -1)
	if len(match) != 1 || !strings.HasPrefix(string(data), "Chain LIBVIRT_FWI (") {
		t.Fatal("exact owned bridge inbound rejection counter unavailable")
	}
	value, err := strconv.ParseUint(string(match[0][1]), 10, 64)
	if err != nil {
		t.Fatal(err)
	}
	return value
}

// A frame injected directly on the Host bridge bypasses IPv4 FORWARD and is
// not a valid external-ingress fixture. Use a temporary veth from a fresh
// unshared namespace and a collision-checked address on its owned veth. No
// existing interface, route or firewall policy is changed. A real Host UDP
// receiver proves the ingress fixture works before testing VM rejection.
// Delete only the verified ifindex/MAC/alias we created; never adopt a device.
const packetInternetUnsolicited = `import ipaddress, json, os, re, socket, subprocess, sys
run, source_ip, destination_ip = sys.argv[1:]
assert os.geteuid() == 0 and re.fullmatch('[0-9a-f-]{36}', run)
env = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C', 'LC_ALL': 'C'}
host, peer = 'kh' + run.replace('-', '')[:10], 'kn' + run.replace('-', '')[:10]
suffix = ':'.join(run.replace('-', '')[index:index+2] for index in range(0, 10, 2))
host_mac, peer_mac = '02:' + suffix, '06:' + suffix
def command(*args):
    return subprocess.check_output(args, env=env, stderr=subprocess.DEVNULL, timeout=3)
inventory = json.loads(command('/usr/sbin/ip', '-j', 'link', 'show'))
assert not any(row['ifname'] in (host, peer) or row.get('address') in (host_mac, peer_mac) for row in inventory)
probe_ip = '198.18.255.254'
for route in json.loads(command('/usr/sbin/ip', '-j', '-4', 'route', 'show', 'table', 'all')):
    if route.get('dst') not in (None, 'default', '0.0.0.0/0'):
        assert ipaddress.ip_address(probe_ip) not in ipaddress.ip_network(route['dst'], strict=False)
for link in json.loads(command('/usr/sbin/ip', '-j', '-4', 'address', 'show')):
    assert not any(address.get('local') == probe_ip for address in link.get('addr_info', []))
parent, child = socket.socketpair()
parent.settimeout(3)
program = r'''
import json, os, socket, struct, subprocess, sys
channel = socket.socket(fileno=int(sys.argv[1]))
channel.settimeout(3)
channel.sendall(b'ready')
device, destination, source_ip, destination_ip, probe_ip, probe_port = json.loads(channel.recv(4096))
subprocess.run(['/usr/sbin/ip', 'link', 'set', device, 'up'], check=True, timeout=2)
link = json.loads(subprocess.check_output(['/usr/sbin/ip', '-j', 'link', 'show', 'dev', device], timeout=2))[0]
src, dst = bytes.fromhex(link['address'].replace(':', '')), bytes.fromhex(destination.replace(':', ''))
def frame(target, port):
    header = struct.pack('!BBHHHBBH4s4s', 0x45, 0, 28, 1, 0, 64, 17, 0, socket.inet_aton(source_ip), socket.inet_aton(target))
    value = sum(struct.unpack('!10H', header))
    while value >> 16: value = (value & 65535) + (value >> 16)
    header = header[:10] + struct.pack('!H', (~value) & 65535) + header[12:]
    return dst + src + b'\x08\x00' + header + struct.pack('!HHHH', 53, port, 8, 0)
with socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3)) as wire:
    wire.bind((device, 0))
    packet = frame(probe_ip, probe_port)
    assert wire.send(packet) == len(packet)
    channel.sendall(b'control')
    assert channel.recv(16) == b'continue'
    packet = frame(destination_ip, 41992)
    assert wire.send(packet) == len(packet)
channel.sendall(b'sent')
assert channel.recv(16) == b'finish'
'''
process = subprocess.Popen(['/usr/bin/unshare', '--net', '/usr/bin/python3', '-c', program, str(child.fileno())], pass_fds=(child.fileno(),), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
child.close()
owned = receiver = None
try:
    assert parent.recv(16) == b'ready'
    assert os.stat('/proc/self/ns/net').st_ino != os.stat(f'/proc/{process.pid}/ns/net').st_ino
    # Explicit MACs prevent udev's automatic stable-MAC assignment racing the
    # ownership snapshot. They are scoped to this run and collision-checked.
    command('/usr/sbin/ip', 'link', 'add', 'name', host, 'address', host_mac,
            'type', 'veth', 'peer', 'name', peer, 'address', peer_mac)
    owned = json.loads(command('/usr/sbin/ip', '-j', 'link', 'show', 'dev', host))[0]
    command('/usr/sbin/ip', 'link', 'set', host, 'alias', 'kelpie-ingress-test:' + run)
    # An unnumbered test veth did not enter the Host IPv4 receive path. Assign
    # only this temporary /32; deleting the owned device removes its local route.
    command('/usr/sbin/ip', 'address', 'add', probe_ip + '/32', 'dev', host, 'noprefixroute')
    command('/usr/sbin/ip', 'link', 'set', peer, 'netns', str(process.pid))
    command('/usr/sbin/ip', 'link', 'set', host, 'up')
    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    receiver.bind((probe_ip, 0))
    receiver.settimeout(2)
    parent.sendall(json.dumps([peer, owned['address'], source_ip, destination_ip, probe_ip, receiver.getsockname()[1]]).encode())
    assert parent.recv(16) == b'control'
    payload, sender = receiver.recvfrom(32)
    assert payload == b'' and sender == (source_ip, 53)
    parent.sendall(b'continue')
    assert parent.recv(16) == b'sent'
finally:
    if receiver is not None:
        receiver.close()
    if owned is not None:
        current = json.loads(command('/usr/sbin/ip', '-j', 'link', 'show', 'dev', host))[0]
        assert current['ifindex'] == owned['ifindex'] and current['address'] == owned['address']
        assert current.get('ifalias') == 'kelpie-ingress-test:' + run
        command('/usr/sbin/ip', 'link', 'delete', host)
        assert not any(row['ifname'] in (host, peer) for row in json.loads(command('/usr/sbin/ip', '-j', 'link', 'show')))
    try:
        parent.sendall(b'finish')
        assert process.wait(timeout=2) == 0
    finally:
        parent.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
print('ingress-positive-control=verified external-unsolicited-frame=1 owned-veth-and-netns-removed')
`

// All six frames must fail before connection tracking. Public destinations
// make a spoofed source distinguishable from a private-destination block.
const packetInternetSpoofFrames = `import socket, struct, sys
device, source, destination, source_ip, destination_ip, direction = sys.argv[1:]
src, dst = bytes.fromhex(source.replace(':', '')), bytes.fromhex(destination.replace(':', ''))
def checksum(data):
    if len(data) % 2: data += b'\0'
    value = sum(struct.unpack('!' + 'H' * (len(data)//2), data))
    while value >> 16: value = (value & 65535) + (value >> 16)
    return (~value) & 65535
def ipv4(sender, target):
    udp = struct.pack('!HHHH', 41991, 53, 8, 0)
    header = struct.pack('!BBHHHBBH4s4s', 0x45, 0, 28, 1, 0, 64, 17, 0, socket.inet_aton(sender), socket.inet_aton(target))
    return header[:10] + struct.pack('!H', checksum(header)) + header[12:] + udp
normal = ipv4(source_ip, destination_ip)
wrong_ip = ipv4('10.99.0.2', destination_ip) if direction == 'out' else ipv4(source_ip, '10.99.0.2')
private = ipv4(source_ip, '169.254.169.254') if direction == 'out' else ipv4('169.254.169.254', destination_ip)
ipv6 = struct.pack('!IHBB', 6 << 28, 8, 17, 64) + socket.inet_pton(socket.AF_INET6, 'fe80::2') + socket.inet_pton(socket.AF_INET6, 'fe80::1') + struct.pack('!HHHH', 41991, 53, 8, 1)
arp = struct.pack('!HHBBH6s4s6s4s', 1, 0x800, 6, 4, 2, src, socket.inet_aton('10.99.0.2'), dst, socket.inet_aton(destination_ip))
frames = [dst + src + b'\x08\x00' + wrong_ip, dst + bytes.fromhex('06aaaaaaaaaa') + b'\x08\x00' + normal,
          dst + src + b'\x08\x00' + private, dst + src + b'\x81\x00\x00\x07\x08\x00' + normal,
          dst + src + b'\x86\xdd' + ipv6, dst + src + b'\x08\x06' + arp]
with socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3)) as wire:
    wire.bind((device, 0))
    for frame in frames: assert wire.send(frame) == len(frame)
print('forbidden-frames=6')
`
