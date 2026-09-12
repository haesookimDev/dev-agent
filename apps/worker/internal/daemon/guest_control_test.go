package daemon

import (
	"errors"
	"strings"
	"testing"
)

func TestGuestControlRequiresPinnedHTTPSOrigin(t *testing.T) {
	for _, test := range []struct {
		origin, address, host string
		port                  uint16
	}{
		{"https://control.example.test", "192.0.2.7", "control.example.test", 443},
		{"https://control.example.test:8443", "10.40.0.2", "control.example.test", 8443},
		{"https://192.0.2.7:443", "192.0.2.7", "192.0.2.7", 443},
	} {
		control, err := parseGuestControl(test.origin, test.address)
		if err != nil || control.Origin != test.origin || control.IPv4 != test.address || control.Host != test.host || control.Port != test.port {
			t.Fatalf("valid explicit guest endpoint rejected: %v", err)
		}
	}
}

func TestGuestControlRejectsUnsafeOrAmbiguousOrigins(t *testing.T) {
	for _, origin := range []string{
		"", "http://control.example.test", "https://localhost", "https://api.localhost", "https://api.local", "https://api",
		"https://127.0.0.1", "https://0.1.2.3", "https://169.254.169.254", "https://[::1]", "https://[::ffff:192.0.2.7]",
		"https://user:private@control.example.test", "https://control.example.test/", "https://control.example.test/api", "https://control.example.test?",
		"https://control.example.test?token=private", "https://control.example.test#private", "https://CONTROL.example.test", "https://control.example.test.",
		"https://control..example.test", "https://-control.example.test", "https://control-.example.test", "https://control_.example.test", "https://한글.example.test",
		"https://control.example.test:", "https://control.example.test:0", "https://control.example.test:0443", "https://control.example.test:65536",
		"https://control.example.test:port", "https://control.example.test\nINJECT=1", " https://control.example.test", "https://control.example.test%2fprivate",
		"https://control.example.test\\private", "https://" + strings.Repeat("a", 64) + ".test", "https://192.0.2.8",
		"https://127.1", "https://0177.0.0.1", "https://0x7f.0x0.0x0.0x1",
	} {
		if value, err := parseGuestControl(origin, "192.0.2.7"); !errors.Is(err, errGuestControl) || value != (guestControl{}) {
			t.Errorf("unsafe origin accepted: %q", origin)
		}
	}
	for _, address := range []string{"", "192.0.2.7/32", "192.000.2.7", "192.0.2.7 ", "::ffff:192.0.2.7", "::1", "0.0.0.0", "0.2.3.4", "127.0.0.1", "169.254.169.254", "224.0.0.1", "255.255.255.255"} {
		if value, err := parseGuestControl("https://control.example.test", address); !errors.Is(err, errGuestControl) || value != (guestControl{}) {
			t.Errorf("unsafe pin accepted: %q", address)
		}
	}
}
