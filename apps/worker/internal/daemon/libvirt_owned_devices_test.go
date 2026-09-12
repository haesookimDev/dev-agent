package daemon

import (
	"strings"
	"testing"
)

func TestOwnedDevicesDoNotFallBackToSharedNetwork(t *testing.T) {
	network := controlNetworkFixture(t)
	args, err := libvirtOwnedDeviceArguments(network, "/images/base.qcow2")
	if err != nil || len(args) != 22 {
		t.Fatal("missing exact device settings")
	}
	text := strings.Join(args, "\n")
	for _, expected := range []string{network.Name, network.Filter, network.GuestMAC, "parameter[1]/@value=" + network.Guest,
		"parameter[2]/@value=none", "backingStore/source/@file=/images/base.qcow2", "backingStore/source/seclabel/@relabel=no"} {
		if !strings.Contains(text, expected) {
			t.Fatal("missing owned device setting", expected)
		}
	}
	for _, base := range []string{"relative.qcow2", "/images/../base", "/images/base,readonly=no", "/images/base\nother", "/images/a=b"} {
		if _, err := libvirtOwnedDeviceArguments(network, base); err == nil {
			t.Fatal("unsafe image option accepted")
		}
	}
	network.Version, network.ControlOrigin, network.ControlIPv4 = 1, "", ""
	if _, err := libvirtOwnedDeviceArguments(network, "/images/base.qcow2"); err == nil {
		t.Fatal("legacy quarantine used as controlled launch")
	}
}
