//go:build linux && libvirt_integration

package daemon

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"
)

// Pure schema validation on a matching Linux libvirt installation. This does
// not define/start a network or VM, install firewall rules, or prove filtering.
func TestDedicatedLibvirtNetworkSchemas(t *testing.T) {
	if os.Getenv("KELPIE_LIBVIRT_TEST_ACK") != "disposable-host-only" {
		t.Skip("requires libvirt_integration tag and explicit disposable-host acknowledgement")
	}
	if os.Geteuid() == 0 {
		t.Fatal("run as the unprivileged Worker user")
	}
	for _, tool := range []string{"/usr/bin/virt-xml-validate", "/usr/bin/xmllint"} {
		if _, err := os.Stat(tool); err != nil {
			t.Fatal("requires the distribution's libvirt XML schemas and libxml2-utils")
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	network, err := allocateRunNetwork(networkTestRun, "10.240.0.0/24", nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	for schema, generate := range map[string]func() ([]byte, error){"network": network.definitionXML, "nwfilter": network.quarantineXML} {
		t.Run(schema, func(t *testing.T) {
			body, err := generate()
			if err != nil {
				t.Fatal(err)
			}
			path := filepath.Join(t.TempDir(), schema+".xml")
			if err := os.WriteFile(path, body, 0600); err != nil {
				t.Fatal(err)
			}
			command := exec.CommandContext(ctx, "/usr/bin/virt-xml-validate", path, schema)
			command.Env = []string{"PATH=/usr/bin:/bin", "LANG=C", "LC_ALL=C"}
			command.WaitDelay = time.Second
			// Only generated synthetic XML and a temporary fixture path can be
			// diagnosed by this tool. No workload or credential enters the file.
			if output, err := command.CombinedOutput(); err != nil {
				t.Fatalf("libvirt %s schema rejected generated XML: %v\n%s", schema, err, output)
			}
			t.Logf("libvirt %s schema accepted generated XML; no network activated", schema)
		})
	}
}
