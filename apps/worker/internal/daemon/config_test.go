package daemon

import "testing"

func TestConfigRejectsShortWorkerCredential(t *testing.T) {
	t.Setenv("KELPIE_WORKER_TOKEN", "short")
	if _, err := ConfigFromEnv(); err == nil {
		t.Fatal("expected short token to be rejected")
	}
}

func TestConfigAcceptsMockExecutor(t *testing.T) {
	t.Setenv("KELPIE_WORKER_TOKEN", "12345678901234567890123456789012")
	t.Setenv("KELPIE_EXECUTOR", "mock")
	config, err := ConfigFromEnv()
	if err != nil {
		t.Fatal(err)
	}
	if config.Executor != "mock" {
		t.Fatalf("unexpected executor %q", config.Executor)
	}
}

func TestLibvirtConfigDoesNotReuseHostLoopbackURL(t *testing.T) {
	t.Setenv("KELPIE_WORKER_TOKEN_FILE", "")
	t.Setenv("KELPIE_WORKER_TOKEN", "12345678901234567890123456789012")
	t.Setenv("KELPIE_EXECUTOR", "libvirt")
	t.Setenv("KELPIE_CONTROL_URL", "http://localhost:8000")
	t.Setenv("KELPIE_GUEST_CONTROL_URL", "")
	t.Setenv("KELPIE_GUEST_CONTROL_IPV4", "")
	if _, err := ConfigFromEnv(); err == nil {
		t.Fatal("libvirt silently reused the host-only control URL")
	}
	t.Setenv("KELPIE_GUEST_CONTROL_URL", "https://control.example.test:8443")
	t.Setenv("KELPIE_GUEST_CONTROL_IPV4", "192.0.2.7")
	config, err := ConfigFromEnv()
	if err != nil || config.ControlURL != "http://localhost:8000" || config.GuestControlURL != "https://control.example.test:8443" || config.GuestControlIPv4 != "192.0.2.7" {
		t.Fatal("guest control configuration was not kept separate from host transport")
	}
}

func TestLibvirtConfigRejectsUnsafeNetworkPool(t *testing.T) {
	t.Setenv("KELPIE_WORKER_TOKEN_FILE", "")
	t.Setenv("KELPIE_WORKER_TOKEN", "12345678901234567890123456789012")
	t.Setenv("KELPIE_EXECUTOR", "libvirt")
	t.Setenv("KELPIE_GUEST_CONTROL_URL", "https://control.example.test")
	t.Setenv("KELPIE_GUEST_CONTROL_IPV4", "192.0.2.7")
	for _, value := range []string{"0.0.0.0/0", "10.240.0.1/24", "192.0.2.0/24", "10.0.0.0/8", "::/64"} {
		t.Setenv("KELPIE_NETWORK_POOL", value)
		if _, err := ConfigFromEnv(); err == nil {
			t.Fatal("unsafe allocation pool accepted")
		}
	}
}

func TestLibvirtInternetEnvironmentRequiresCompleteExplicitOptIn(t *testing.T) {
	t.Setenv("KELPIE_WORKER_TOKEN_FILE", "")
	t.Setenv("KELPIE_WORKER_TOKEN", "12345678901234567890123456789012")
	t.Setenv("KELPIE_EXECUTOR", "libvirt")
	t.Setenv("KELPIE_GUEST_CONTROL_URL", "https://control.example.test")
	t.Setenv("KELPIE_GUEST_CONTROL_IPV4", "192.0.2.7")
	t.Setenv("KELPIE_GUEST_CONTROL_CA_FILE", "")
	t.Setenv("KELPIE_NETWORK_POOL", "10.240.0.0/16")
	for _, scenario := range []struct {
		mode, dns, denied string
		valid             bool
	}{
		{"", "", "", true}, {"disabled", "", "", true},
		{"logged-public-ipv4", "1.1.1.1", "203.0.113.0/24", true},
		{"", "1.1.1.1", "", false}, {"disabled", "", "203.0.113.0/24", false},
		{"logged-public-ipv4", "1.1.1.1", "", false},
		{"logged-public-ipv4", "192.168.1.1", "203.0.113.0/24", false},
		{"public", "1.1.1.1", "203.0.113.0/24", false},
	} {
		t.Setenv("KELPIE_GUEST_INTERNET", scenario.mode)
		t.Setenv("KELPIE_GUEST_DNS_IPV4", scenario.dns)
		t.Setenv("KELPIE_GUEST_DENIED_IPV4", scenario.denied)
		config, err := ConfigFromEnv()
		if (err == nil) != scenario.valid {
			t.Fatalf("unexpected opt-in validity for mode %q", scenario.mode)
		}
		if err == nil && (config.GuestInternet != scenario.mode || config.GuestDNSIPv4 != scenario.dns || config.GuestDeniedIPv4 != scenario.denied) {
			t.Fatal("explicit settings changed while loading environment")
		}
	}
}
