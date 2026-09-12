//go:build !linux

package daemon

import "context"

func queryHostIPv4Routes(context.Context) ([]byte, error) {
	return nil, errRunNetwork // Actual libvirt host inventory requires Linux.
}
