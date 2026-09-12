package main

import (
	"context"
	"fmt"
	"os"

	"github.com/haesookimdev/kelpie/apps/worker/internal/daemon"
)

func main() {
	if daemon.RunNetworkLogHook(context.Background(), os.Args[1:], os.Stdin) != nil {
		// libvirt records stderr. Do not include hook XML, paths or command
		// output, which could have originated in an untrusted workload.
		fmt.Fprintln(os.Stderr, "kelpie network logging is unconfirmed")
		os.Exit(1)
	}
}
