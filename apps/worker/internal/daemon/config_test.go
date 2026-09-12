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
