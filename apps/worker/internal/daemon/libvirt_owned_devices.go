package daemon

import (
	"path/filepath"
	"strings"
)

// virt-install 4.1's XML setters escape values and create only these exact
// device children. The base source alone opts out of dynamic DAC relabeling;
// the VM and its writable overlay retain the normal DAC/AppArmor boundary.
// Operators must provision QEMU read-only access to the immutable image.
func libvirtOwnedDeviceArguments(network runNetwork, base string) ([]string, error) {
	if !network.valid(network.UUID) || (network.Version != 2 && network.Version != 3) || !filepath.IsAbs(base) || filepath.Clean(base) != base || strings.ContainsAny(base, ",=\r\n\x00") {
		return nil, errRunNetwork
	}
	args := []string{"--network", "network=" + network.Name + ",model=virtio,mac=" + network.GuestMAC + ",filterref.filter=" + network.Filter + ",trustGuestRxFilters=no"}
	for _, value := range []string{
		"./devices/interface[1]/filterref/parameter[1]/@name=IP",
		"./devices/interface[1]/filterref/parameter[1]/@value=" + network.Guest,
		"./devices/interface[1]/filterref/parameter[2]/@name=CTRL_IP_LEARNING",
		"./devices/interface[1]/filterref/parameter[2]/@value=none",
		"./devices/disk[1]/backingStore/@type=file",
		"./devices/disk[1]/backingStore/format/@type=qcow2",
		"./devices/disk[1]/backingStore/source/@file=" + base,
		"./devices/disk[1]/backingStore/source/seclabel/@model=dac",
		"./devices/disk[1]/backingStore/source/seclabel/@relabel=no",
		"xpath.create=./devices/disk[1]/backingStore/backingStore",
	} {
		args = append(args, "--xml", value)
	}
	return args, nil
}
