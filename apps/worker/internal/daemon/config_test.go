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
