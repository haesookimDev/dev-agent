//go:build linux && libvirt_integration

package daemon

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"encoding/xml"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"testing"
	"time"
)

// An explicit, credential-free Golden Image fixture, not the production
// Executor/Runner path. It uses the production journal/provisioner/NIC template
// and cleanup. Root is used ONLY by this opt-in test for read-only firewall
// counters and scoped packet probes (public ingress uses a fresh owned veth
// and namespace). Worker privileges, host firewall policy and pre-existing
// resources are never changed by this test.
func TestDedicatedLibvirtQuarantinePackets(t *testing.T) {
	dedicatedNetworkPackets(t, nil, false, nil)
}

func TestDedicatedLibvirtControlPackets(t *testing.T) {
	if os.Getenv("KELPIE_LIBVIRT_TEST_ACK") != "disposable-host-only" || os.Getenv("KELPIE_LIBVIRT_CONTROL_ORIGIN") == "" {
		t.Skip("requires a dedicated host and explicit credential-free HTTPS test endpoint")
	}
	control, err := parseGuestControl(os.Getenv("KELPIE_LIBVIRT_CONTROL_ORIGIN"), os.Getenv("KELPIE_LIBVIRT_CONTROL_IPV4"))
	if err != nil {
		t.Fatal(err)
	}
	dedicatedNetworkPackets(t, &control, false, nil)
}

func dedicatedNetworkPackets(t *testing.T, control *guestControl, requireLogs bool, internet *guestInternet) {
	t.Helper()
	base := os.Getenv("KELPIE_LIBVIRT_PACKET_IMAGE")
	if os.Getenv("KELPIE_LIBVIRT_TEST_ACK") != "disposable-host-only" || base == "" {
		t.Skip("requires dedicated-host acknowledgement and an explicitly prepared Golden Image")
	}
	if os.Geteuid() == 0 || runtime.GOARCH != "arm64" || !filepath.IsAbs(base) || filepath.Clean(base) != base || strings.ContainsAny(base, "<>&\"'\n\r,") {
		t.Fatal("requires unprivileged ARM64 Worker and a canonical fixture image path")
	}
	info, err := os.Lstat(base)
	if err != nil || !privateOwned(info, false) {
		t.Fatal("fixture image must initially be private, Worker-owned and not linked")
	}
	var space syscall.Statfs_t
	if syscall.Statfs(filepath.Dir(base), &space) != nil || space.Bavail*uint64(space.Bsize) < 1<<30 {
		t.Fatal("fixture needs at least 1 GiB free; never copy or overwrite the base")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 4*time.Minute)
	defer cancel()
	domains, err := queryVirsh(ctx, "list", "--all", "--uuid")
	if err != nil || strings.TrimSpace(string(domains)) != "" {
		t.Fatal("requires an empty dedicated-host domain inventory")
	}
	verifyBase := packetBaseReadAccess(t, base)
	before, err := queryVirsh(ctx, "net-list", "--all", "--uuid")
	if err != nil {
		t.Fatal(err)
	}
	root, err := os.MkdirTemp("/var/tmp", "kelpie-network-")
	if err != nil {
		t.Fatal(err)
	}
	store, err := openRunStore(root)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	reader := hostNetworkReader()
	inventory, err := reader.read(ctx)
	if err != nil {
		t.Fatal(err)
	}
	lease, err := os.ReadFile("/proc/sys/kernel/random/uuid")
	if err != nil {
		t.Fatal(err)
	}
	var run ownedRun
	if internet != nil && control != nil && requireLogs {
		run, err = store.CreateInternet(storeTestWork, strings.TrimSpace(string(lease)), testResources, "10.240.0.0/24", inventory, *control, *internet)
	} else if control == nil {
		run, err = store.CreateNetworked(storeTestWork, strings.TrimSpace(string(lease)), testResources, "10.240.0.0/24", inventory.excluded)
	} else {
		run, err = store.CreateControlled(storeTestWork, strings.TrimSpace(string(lease)), testResources, "10.240.0.0/24", inventory.excluded, *control)
	}
	if err != nil {
		t.Fatal(err)
	}
	cleanup := newVMCleanup(store, run.Record.RunID)
	t.Logf("preserved packet-test journal: %s/%s", root, run.Record.RunID)
	defer func() {
		finish, stop := context.WithTimeout(context.Background(), time.Minute)
		defer stop()
		if err := cleanup.Cleanup(finish); err != nil {
			t.Errorf("owned packet fixture cleanup unconfirmed; journal retained: %v", err)
		}
	}()
	p := networkProvisioner{store: store, reader: reader}
	if err := p.create(ctx, run.Record.RunID); err != nil {
		t.Fatal(err)
	}
	network := *run.Record.Network
	runDir := filepath.Join(root, run.Record.RunID)
	overlay, seed := filepath.Join(runDir, "root.qcow2"), filepath.Join(runDir, "seed.iso")
	labCommand(t, ctx, "qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b", base, overlay)
	for name, body := range map[string]string{
		"meta-data": "instance-id: " + run.Record.Domain + "\nlocal-hostname: kelpie-packet-fixture\n",
		"user-data": "#cloud-config\nssh_pwauth: false\ndisable_root: true\n",
	} {
		if err := os.WriteFile(filepath.Join(runDir, name), []byte(body), 0600); err != nil {
			t.Fatal(err)
		}
	}
	// Static, offline configuration avoids DHCP waiting under deny-all. The
	// packet probes use raw Ethernet and do not depend on routing/ARP success.
	tools := t.TempDir() // Contains no disk, socket or other live VM source.
	netConfig := filepath.Join(tools, "network-config")
	netBody := fmt.Sprintf("version: 2\nethernets:\n  fixture:\n    match:\n      macaddress: '%s'\n    set-name: fixture0\n    dhcp4: false\n    dhcp6: false\n    accept-ra: false\n    optional: true\n    addresses: ['%s/30']\n", network.GuestMAC, network.Guest)
	if control != nil {
		body, err := network.guestNetworkConfig()
		if err != nil {
			t.Fatal(err)
		}
		netBody = string(body)
	}
	if err := os.WriteFile(netConfig, []byte(netBody), 0600); err != nil {
		t.Fatal(err)
	}
	labCommand(t, ctx, "cloud-localds", "--network-config", netConfig, seed, filepath.Join(runDir, "user-data"), filepath.Join(runDir, "meta-data"))
	for _, name := range []string{"root.qcow2", "seed.iso"} {
		if err := privateRunArtifact(store, run.Record.RunID+"/"+name); err != nil {
			t.Fatal(err)
		}
	}
	if err := grantHypervisorSearch(store, run.Record.RunID); err != nil {
		t.Fatal(err)
	}
	nic, err := network.interfaceXML()
	if err != nil {
		t.Fatal(err)
	}
	domain := fmt.Sprintf(`<domain type='kvm'>
  <name>%s</name><uuid>%s</uuid><description>%s</description>
  <memory unit='MiB'>1536</memory><vcpu>2</vcpu>
  <os><type arch='aarch64' machine='virt'>hvm</type>
    <loader readonly='yes' type='pflash'>/usr/share/AAVMF/AAVMF_CODE.fd</loader>
    <nvram template='/usr/share/AAVMF/AAVMF_VARS.fd'>%s/nvram.fd</nvram>
    <boot dev='hd'/></os>
  <features><acpi/><gic version='host'/></features><cpu mode='host-passthrough'/>
  <devices>
    <disk type='file' device='disk'><driver name='qemu' type='qcow2'/><source file='%s'/>
      <backingStore type='file'><format type='qcow2'/><source file='%s'><seclabel model='dac' relabel='no'/></source><backingStore/></backingStore>
      <target dev='vda' bus='virtio'/></disk>
    <controller type='scsi' model='virtio-scsi'/>
    <disk type='file' device='cdrom'><driver name='qemu' type='raw'/><source file='%s'/><target dev='sda' bus='scsi'/><readonly/></disk>
    %s
    <channel type='unix'><target type='virtio' name='org.qemu.guest_agent.0'/></channel>
    <console type='pty'/>
  </devices>
</domain>`, run.Record.Domain, run.Record.RunID, domainOwner(run.Record), runDir, overlay, base, seed, nic)
	domainPath := filepath.Join(tools, "domain.xml")
	if err := os.WriteFile(domainPath, []byte(domain), 0600); err != nil {
		t.Fatal(err)
	}
	labCommand(t, ctx, "virsh", "-c", "qemu:///system", "define", domainPath, "--validate")
	labCommand(t, ctx, "virsh", "-c", "qemu:///system", "start", run.Record.RunID)
	verifyBase() // Regression: default dynamic DAC used to chown the backing image.
	if _, err := store.Advance(run.Record.RunID, "running"); err != nil {
		t.Fatal(err)
	}
	if nvram, err := cleanup.verifyDomain(ctx, run.Record); err != nil || !nvram {
		t.Fatal("running fixture has invalid disk/firmware ownership")
	}
	tap := packetFixtureNIC(t, ctx, run.Record)
	deadline := time.Now().Add(90 * time.Second)
	for {
		if _, err := queryVirsh(ctx, "qemu-agent-command", run.Record.RunID, `{"execute":"guest-ping"}`); err == nil {
			break
		}
		if ctx.Err() != nil || !time.Now().Before(deadline) {
			t.Fatal("Golden Image guest agent did not become ready")
		}
		time.Sleep(time.Second)
	}
	// A successful in-guest loopback packet is a positive transport control,
	// independent of the intentionally denied external NIC.
	packetGuestExec(t, ctx, run.Record.RunID, "/usr/bin/python3", "-c", packetLoopbackControl)
	cloneIPv4 := ""
	if internet != nil {
		cloneIPv4 = packetInternetClone(t, ctx, run.Record)
		packetInternetSpoofing(t, ctx, run.Record, tap)
	}
	if control != nil {
		var initial uint64
		if internet != nil {
			initial = packetInternetDropCounter(t, ctx, "libvirt-I-"+tap)
		} else {
			initial = packetReadDropCounter(t, ctx, "libvirt-I-"+tap, false)
		}
		result := packetGuestExecWithin(t, ctx, 30*time.Second, run.Record.RunID, "/usr/bin/python3", "-c", packetControlProbe,
			control.Host, control.IPv4, fmt.Sprint(control.Port), network.Gateway)
		if strings.TrimSpace(string(result)) != "tls-control=verified forbidden-tcp=8" {
			t.Fatal("control transport checks incomplete")
		}
		var final uint64
		if internet != nil {
			final = packetInternetDropCounter(t, ctx, "libvirt-I-"+tap)
		} else {
			final = packetReadDropCounter(t, ctx, "libvirt-I-"+tap, false)
		}
		if final < initial+8 {
			t.Fatal("forbidden connection attempts were not accounted for by the TAP firewall")
		}
		t.Logf("actual guest verified TLS to the explicit control endpoint; 8 forbidden TCP destinations/ports rejected; TAP DROP %d -> %d", initial, final)
		if requireLogs {
			packetVerifyNetworkLog(t, ctx, network, control, cloneIPv4, false)
		}
	} else {
		for _, direction := range []string{"out", "in"} {
			chain := "libvirt-I-" + tap
			srcMAC, dstMAC, srcIP, dstIP, device := network.GuestMAC, network.GatewayMAC, network.Guest, network.Gateway, "fixture0"
			if direction == "in" {
				chain = "libvirt-O-" + tap
				srcMAC, dstMAC, srcIP, dstIP, device = network.GatewayMAC, network.GuestMAC, network.Gateway, network.Guest, network.Bridge
			}
			initial := packetDropCounter(t, ctx, chain)
			args := []string{"-c", packetFrameProbe, device, srcMAC, dstMAC, srcIP, dstIP}
			var sent []byte
			if direction == "out" {
				sent = packetGuestExec(t, ctx, run.Record.RunID, "/usr/bin/python3", args...)
			} else {
				sent = packetHostQuery(t, ctx, "/usr/bin/sudo", append([]string{"-n", "/usr/bin/python3"}, args...)...)
			}
			if strings.TrimSpace(string(sent)) != "frames-sent=12" {
				t.Fatal("frame generator did not confirm every packet transmission")
			}
			final := packetDropCounter(t, ctx, chain)
			if final < initial+12 {
				t.Fatalf("%s quarantine did not account for all 12 transmitted frames: %d -> %d", direction, initial, final)
			}
			t.Logf("%s: 12 guest/bridge frames sent; exact TAP DROP counter %d -> %d", direction, initial, final)
		}
	}
	if err := cleanup.Cleanup(ctx); err != nil {
		t.Fatal(err)
	}
	if requireLogs {
		packetVerifyNetworkLog(t, ctx, network, control, cloneIPv4, true)
	}
	if err := cleanup.Released(); err != nil {
		t.Fatal(err)
	}
	verifyBase()
	after, err := queryVirsh(ctx, "net-list", "--all", "--uuid")
	if err != nil || string(before) != string(after) {
		t.Fatal("packet fixture changed pre-existing network inventory")
	}
	t.Log("actual Golden Image transport checks completed; owned VM/network/filter/bridge/disks absent; synthetic local release only, not Runner/API acceptance")
}

const packetControlProbe = `
import socket, ssl, sys
host, address, port, gateway = sys.argv[1:]
port = int(port)
with socket.create_connection((address, port), timeout=12) as raw:
    with ssl.create_default_context().wrap_socket(raw, server_hostname=host) as conn:
        conn.sendall(('HEAD / HTTP/1.1\r\nHost: ' + host + '\r\nConnection: close\r\n\r\n').encode())
        assert conn.recv(4096).startswith(b'HTTP/'), 'TLS endpoint did not return HTTP'
for target, blocked_port in [(address, 22), (address, 80), (gateway, port),
        ('192.168.5.15', port), ('169.254.169.254', 80), ('10.0.0.1', port),
        ('172.16.0.1', port), ('192.168.1.1', port)]:
    try:
        conn = socket.create_connection((target, blocked_port), timeout=0.5)
    except (TimeoutError, OSError):
        pass
    else:
        conn.close()
        raise AssertionError('forbidden TCP connection succeeded')
print('tls-control=verified forbidden-tcp=8')
`

func packetFixtureNIC(t *testing.T, ctx context.Context, record runRecord) string {
	t.Helper()
	data, err := queryVirsh(ctx, "dumpxml", record.RunID)
	var domain struct {
		Interfaces []struct {
			Type string `xml:"type,attr"`
			MAC  struct {
				Address string `xml:"address,attr"`
			} `xml:"mac"`
			Source struct {
				Network string `xml:"network,attr"`
				Bridge  string `xml:"bridge,attr"`
			} `xml:"source"`
			Target struct {
				Dev string `xml:"dev,attr"`
			} `xml:"target"`
			Filter struct {
				Name string `xml:"filter,attr"`
			} `xml:"filterref"`
		} `xml:"devices>interface"`
	}
	if err != nil || !validDomainXML(data) || xml.Unmarshal(data, &domain) != nil || len(domain.Interfaces) != 1 {
		t.Fatal("fixture does not have exactly one network interface")
	}
	nic, network := domain.Interfaces[0], record.Network
	if nic.Type != "network" || nic.MAC.Address != network.GuestMAC || nic.Source.Network != network.Name || nic.Source.Bridge != network.Bridge ||
		nic.Filter.Name != network.Filter || !regexp.MustCompile(`^vnet[0-9]{1,8}$`).MatchString(nic.Target.Dev) {
		t.Fatal("actual NIC identity does not match the recorded quarantine")
	}
	return nic.Target.Dev
}

func packetHostQuery(t *testing.T, ctx context.Context, name string, args ...string) []byte {
	t.Helper()
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	command := exec.CommandContext(ctx, name, args...)
	command.Env = []string{"PATH=/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL=C", "LANG=C"}
	command.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	command.Cancel = func() error { return syscall.Kill(-command.Process.Pid, syscall.SIGKILL) }
	command.WaitDelay = time.Second
	var output boundedVMOutput
	command.Stdout = &output
	if command.Run() != nil {
		t.Fatal("dedicated packet-test helper failed")
	}
	return output.buffer.Bytes()
}

func packetDropCounter(t *testing.T, ctx context.Context, chain string) uint64 {
	return packetReadDropCounter(t, ctx, chain, true)
}

func packetReadDropCounter(t *testing.T, ctx context.Context, chain string, quarantine bool) uint64 {
	t.Helper()
	data := packetHostQuery(t, ctx, "/usr/bin/sudo", "-n", "/usr/sbin/ebtables", "-t", "nat", "-L", chain, "--Lc")
	matches := regexp.MustCompile(`(?m)^-j DROP\s*,\s*pcnt\s*=\s*([0-9]+)\s*--\s*bcnt\s*=\s*[0-9]+\s*$`).FindAllSubmatch(data, -1)
	if len(matches) != 1 || !strings.Contains(string(data), "Bridge chain: "+chain+", entries: ") ||
		quarantine && !strings.Contains(string(data), "Bridge chain: "+chain+", entries: 1,") {
		t.Fatalf("expected one unconditional drop in the exact TAP chain; observed:\n%s", data)
	}
	value, err := strconv.ParseUint(string(matches[0][1]), 10, 64)
	if err != nil {
		t.Fatal(err)
	}
	return value
}

func packetGuestExec(t *testing.T, ctx context.Context, uuid, path string, args ...string) []byte {
	t.Helper()
	return packetGuestExecWithin(t, ctx, 15*time.Second, uuid, path, args...)
}

func packetGuestExecWithin(t *testing.T, ctx context.Context, limit time.Duration, uuid, path string, args ...string) []byte {
	t.Helper()
	ctx, cancel := context.WithTimeout(ctx, limit)
	defer cancel()
	request, _ := json.Marshal(map[string]any{"execute": "guest-exec", "arguments": map[string]any{"path": path, "arg": args, "capture-output": true}})
	data, err := queryVirsh(ctx, "qemu-agent-command", uuid, string(request))
	var started struct {
		Return struct {
			PID int `json:"pid"`
		} `json:"return"`
	}
	if err != nil || json.Unmarshal(data, &started) != nil || started.Return.PID <= 0 {
		t.Fatal("fixture guest command did not start")
	}
	for {
		request, _ := json.Marshal(map[string]any{"execute": "guest-exec-status", "arguments": map[string]any{"pid": started.Return.PID}})
		data, err := queryVirsh(ctx, "qemu-agent-command", uuid, string(request))
		var status struct {
			Return struct {
				Exited    bool   `json:"exited"`
				ExitCode  *int   `json:"exitcode"`
				Output    string `json:"out-data"`
				Truncated bool   `json:"out-truncated"`
			} `json:"return"`
		}
		if err != nil || json.Unmarshal(data, &status) != nil {
			t.Fatal("fixture guest command status unavailable")
		}
		if status.Return.Exited {
			output, decodeErr := base64.StdEncoding.DecodeString(status.Return.Output)
			if status.Return.ExitCode != nil && *status.Return.ExitCode != 0 {
				t.Fatalf("fixture guest command failed with exit code %d; guest output withheld", *status.Return.ExitCode)
			}
			if status.Return.ExitCode == nil || *status.Return.ExitCode != 0 || status.Return.Truncated || decodeErr != nil {
				t.Fatal("fixture guest command failed or output was incomplete")
			}
			return output
		}
		if ctx.Err() != nil {
			t.Fatal("fixture guest command timed out; VM cleanup will stop it")
		}
		time.Sleep(100 * time.Millisecond)
	}
}

const packetLoopbackControl = `import socket
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
    receiver.bind(('127.0.0.1', 0))
    receiver.settimeout(1)
    sender.sendto(b'kelpie-packet-positive-control', receiver.getsockname())
    assert receiver.recv(64) == b'kelpie-packet-positive-control'
print('loopback-control=passed')
`

// Deliberately no DNS, external connection or dependency install. Raw frames
// bypass ARP/route failures so a positive DROP delta proves actual enforcement.
const packetFrameProbe = `import socket, struct, sys
device, source, destination, source_ip, destination_ip = sys.argv[1:]
src = bytes.fromhex(source.replace(':', ''))
dst = bytes.fromhex(destination.replace(':', ''))
payload = b'kelpie-quarantine-frame'
def checksum(data):
    if len(data) % 2: data += b'\0'
    value = sum(struct.unpack('!' + 'H' * (len(data)//2), data))
    while value >> 16: value = (value & 65535) + (value >> 16)
    return (~value) & 65535
def ipv4(target, source=source_ip):
    udp = struct.pack('!HHHH', 41000, 9, 8 + len(payload), 0) + payload
    header = struct.pack('!BBHHHBBH4s4s', 0x45, 0, 20 + len(udp), 1, 0, 64, 17, 0, socket.inet_aton(source), socket.inet_aton(target))
    return header[:10] + struct.pack('!H', checksum(header)) + header[12:] + udp
def ipv6(target):
    source = socket.inet_pton(socket.AF_INET6, 'fe80::2')
    dest = socket.inet_pton(socket.AF_INET6, target)
    length = 8 + len(payload)
    udp = struct.pack('!HHHH', 41000, 9, length, 0) + payload
    pseudo = source + dest + struct.pack('!I3xB', length, 17)
    udp = udp[:6] + struct.pack('!H', checksum(pseudo + udp) or 65535) + udp[8:]
    return struct.pack('!IHBB', 6 << 28, length, 17, 64) + source + dest + udp
frames = [dst + src + b'\x08\x00' + ipv4(target) for target in
          (destination_ip, '169.254.169.254', '10.0.0.1', '172.16.0.1', '192.168.0.1', '198.51.100.1')]
frames += [dst + src + b'\x86\xdd' + ipv6(target) for target in ('fe80::1', 'fd00::1', '2001:db8::1')]
arp = struct.pack('!HHBBH6s4s6s4s', 1, 0x800, 6, 4, 1, src, socket.inet_aton(source_ip), b'\0'*6, socket.inet_aton(destination_ip))
frames += [b'\xff'*6 + src + b'\x08\x06' + arp,
           dst + src + b'\x81\x00\x00\x07\x08\x00' + ipv4(destination_ip),
           dst + bytes.fromhex('06aaaaaaaaaa') + b'\x08\x00' + ipv4(destination_ip, '10.99.0.2')]
assert len(frames) == 12
with socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3)) as wire:
    wire.bind((device, 0))
    for frame in frames:
        assert wire.send(frame) == len(frame)
print('frames-sent=12')
`
