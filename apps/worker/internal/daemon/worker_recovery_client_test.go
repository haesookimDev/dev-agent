package daemon

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"reflect"
	"strings"
	"sync/atomic"
	"testing"
)

const workerRecoveryClientID = "aaaaaaaa-1111-4111-8111-111111111111"

func workerRecoveryClientConfig() Config {
	return Config{
		WorkerName: "recovery-worker", Executor: "libvirt",
		CPUTotal: 4, MemoryMBTotal: 8192, DiskGBTotal: 60,
	}
}

func workerRecoveryClientResponse(active int) map[string]any {
	return map[string]any{
		"id": workerRecoveryClientID, "name": "recovery-worker", "state": "online",
		"cpu_total": 4, "cpu_available": 4 - 2*active,
		"memory_mb_total": 8192, "memory_mb_available": 8192 - 4096*active,
		"disk_gb_available": 60 - 30*active, "active_runs": active,
		"labels":       map[string]string{"virtualization": "libvirt"},
		"last_seen_at": "2026-09-11T00:00:00Z",
		"extension": map[string]any{
			"enabled": true,
			"details": map[string]any{"source": "test"},
		},
	}
}

func workerRecoveryClientJSON(t *testing.T, value any) string {
	t.Helper()
	data, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	return string(data)
}

func TestWorkerRecoveryClientRegistersActiveAndInactiveWorkers(t *testing.T) {
	firstCredential := strings.Repeat("first-recovery-credential-", 2)
	secondCredential := strings.Repeat("second-recovery-credential-", 2)
	credentialPath := t.TempDir() + "/worker-token"
	if err := os.WriteFile(credentialPath, []byte(firstCredential+"\n"), 0600); err != nil {
		t.Fatal(err)
	}

	var requests atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		requestNumber := requests.Add(1)
		if request.Method != http.MethodPost || request.URL.Path != "/api/workers/register" || request.URL.RawQuery != "" {
			t.Error("recovery registration used the wrong endpoint")
		}
		if request.Header.Get("X-Kelpie-Lease") != "" || request.Header.Get("Content-Type") != "application/json" {
			t.Error("recovery registration crossed the credential boundary")
		}
		expectedCredential := firstCredential
		active := 0
		if requestNumber == 2 {
			expectedCredential = secondCredential
			active = 1
		}
		if request.Header.Get("Authorization") != "Bearer "+expectedCredential {
			t.Error("recovery registration did not reload the Worker credential")
		}
		var body map[string]any
		if err := json.NewDecoder(request.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		expectedBody := map[string]any{
			"name": "recovery-worker", "cpu_total": float64(4),
			"memory_mb_total": float64(8192), "disk_gb_available": float64(60),
			"labels": map[string]any{"virtualization": "libvirt"},
		}
		if !reflect.DeepEqual(body, expectedBody) {
			t.Errorf("unexpected recovery registration body: %#v", body)
		}
		_ = json.NewEncoder(writer).Encode(workerRecoveryClientResponse(active))
	}))
	defer server.Close()

	client := NewClient(server.URL, strings.Repeat("unused-fallback-", 3))
	client.tokenFile = credentialPath
	for active := 0; active <= 1; active++ {
		worker, err := client.RegisterRecovery(context.Background(), workerRecoveryClientConfig())
		if err != nil {
			t.Fatalf("active=%d: %v", active, err)
		}
		if worker.ID != workerRecoveryClientID || worker.Name != "recovery-worker" ||
			worker.CPUTotal != 4 || worker.CPUAvailable != 4-2*active ||
			worker.MemoryMBTotal != 8192 || worker.MemoryMBAvailable != 8192-4096*active ||
			worker.DiskGBAvailable != 60-30*active || worker.ActiveRuns != active ||
			worker.Labels["virtualization"] != "libvirt" {
			t.Fatalf("active=%d: unexpected Worker: %+v", active, worker)
		}
		if active == 0 {
			if err := os.WriteFile(credentialPath, []byte(secondCredential+"\n"), 0600); err != nil {
				t.Fatal(err)
			}
		}
	}
	if requests.Load() != 2 {
		t.Fatal("expected one inactive and one active recovery registration")
	}
}

func TestWorkerRecoveryClientRejectsInvalidConfiguration(t *testing.T) {
	for _, test := range []struct {
		name   string
		mutate func(*Config)
	}{
		{name: "executor", mutate: func(config *Config) { config.Executor = "mock" }},
		{name: "name", mutate: func(config *Config) { config.WorkerName = "" }},
		{name: "cpu", mutate: func(config *Config) { config.CPUTotal = 0 }},
		{name: "memory", mutate: func(config *Config) { config.MemoryMBTotal = 1023 }},
		{name: "disk", mutate: func(config *Config) { config.DiskGBTotal = 0 }},
	} {
		t.Run(test.name, func(t *testing.T) {
			var requests atomic.Int32
			server := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
				requests.Add(1)
			}))
			defer server.Close()
			config := workerRecoveryClientConfig()
			test.mutate(&config)
			_, err := NewClient(server.URL, strings.Repeat("worker-credential-", 3)).RegisterRecovery(context.Background(), config)
			if err == nil || err.Error() != "control request configuration failed" {
				t.Fatalf("invalid configuration was not safely rejected: %v", err)
			}
			if requests.Load() != 0 {
				t.Fatal("invalid configuration reached the control plane")
			}
		})
	}
}

func TestWorkerRecoveryClientRequiresExactOKStatus(t *testing.T) {
	for _, status := range []int{
		http.StatusCreated, http.StatusAccepted, http.StatusNoContent,
		http.StatusBadRequest, http.StatusInternalServerError,
	} {
		t.Run(http.StatusText(status), func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
				writer.WriteHeader(status)
				_, _ = io.WriteString(writer, "private-status-response")
			}))
			defer server.Close()
			_, err := NewClient(server.URL, strings.Repeat("worker-credential-", 3)).RegisterRecovery(context.Background(), workerRecoveryClientConfig())
			expected := (diagnosticError{kind: controlStatus, code: status}).Error()
			if err == nil || err.Error() != expected || strings.Contains(err.Error(), "private-status-response") {
				t.Fatalf("status %d was not safely rejected: %v", status, err)
			}
		})
	}
}

func TestWorkerRecoveryClientRejectsMalformedResponses(t *testing.T) {
	valid := workerRecoveryClientJSON(t, workerRecoveryClientResponse(0))
	withoutID := workerRecoveryClientResponse(0)
	delete(withoutID, "id")
	nullID := workerRecoveryClientResponse(0)
	nullID["id"] = nil
	for _, test := range []struct {
		name string
		body string
	}{
		{name: "empty"},
		{name: "truncated", body: `{"id":"` + workerRecoveryClientID + `"`},
		{name: "oversized", body: valid + strings.Repeat(" ", maxWorkerRecoveryBytes)},
		{name: "duplicate required", body: strings.TrimSuffix(valid, "}") + `,"id":"` + workerRecoveryClientID + `"}`},
		{name: "duplicate nested", body: strings.Replace(valid, `"details":{"source":"test"}`, `"details":{"source":"test","source":"other"}`, 1)},
		{name: "case alias substitution", body: strings.Replace(valid, `"id"`, `"ID"`, 1)},
		{name: "missing required", body: workerRecoveryClientJSON(t, withoutID)},
		{name: "null required", body: workerRecoveryClientJSON(t, nullID)},
		{name: "trailing object", body: valid + `{}`},
		{name: "top-level array", body: `[]`},
		{name: "top-level null", body: `null`},
	} {
		t.Run(test.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
				_, _ = io.WriteString(writer, test.body)
			}))
			defer server.Close()
			_, err := NewClient(server.URL, strings.Repeat("worker-credential-", 3)).RegisterRecovery(context.Background(), workerRecoveryClientConfig())
			if err == nil || err.Error() != "control response decoding failed" ||
				strings.Contains(err.Error(), workerRecoveryClientID) || strings.Contains(err.Error(), server.URL) {
				t.Fatalf("invalid response was not safely rejected: %v", err)
			}
		})
	}
}

func TestWorkerRecoveryClientRejectsMissingNullAndAliasedRequiredFields(t *testing.T) {
	for _, required := range workerRecoveryRequiredFields {
		for _, variant := range []string{"missing", "null", "case alias"} {
			t.Run(required+" "+variant, func(t *testing.T) {
				response := workerRecoveryClientResponse(0)
				switch variant {
				case "missing":
					delete(response, required)
				case "null":
					response[required] = nil
				case "case alias":
					response[strings.ToUpper(required)] = response[required]
					delete(response, required)
				}
				server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
					_ = json.NewEncoder(writer).Encode(response)
				}))
				defer server.Close()
				_, err := NewClient(server.URL, strings.Repeat("worker-credential-", 3)).RegisterRecovery(context.Background(), workerRecoveryClientConfig())
				if err == nil || err.Error() != "control response decoding failed" {
					t.Fatalf("%s %s was accepted: %v", required, variant, err)
				}
			})
		}
	}
}

func TestWorkerRecoveryClientRejectsInvalidWorkerIdentityResourcesAndState(t *testing.T) {
	for _, test := range []struct {
		name   string
		mutate func(map[string]any)
	}{
		{name: "wrong name", mutate: func(value map[string]any) { value["name"] = "other-worker" }},
		{name: "invalid id", mutate: func(value map[string]any) { value["id"] = "not-a-uuid" }},
		{name: "noncanonical id", mutate: func(value map[string]any) { value["id"] = strings.ToUpper(workerRecoveryClientID) }},
		{name: "cpu total mismatch", mutate: func(value map[string]any) { value["cpu_total"] = 8 }},
		{name: "memory total mismatch", mutate: func(value map[string]any) { value["memory_mb_total"] = 16384 }},
		{name: "cpu over total", mutate: func(value map[string]any) { value["cpu_available"] = 5 }},
		{name: "memory over total", mutate: func(value map[string]any) { value["memory_mb_available"] = 8193 }},
		{name: "disk over configured", mutate: func(value map[string]any) { value["disk_gb_available"] = 61 }},
		{name: "negative cpu", mutate: func(value map[string]any) { value["cpu_available"] = -1 }},
		{name: "negative memory", mutate: func(value map[string]any) { value["memory_mb_available"] = -1 }},
		{name: "negative disk", mutate: func(value map[string]any) { value["disk_gb_available"] = -1 }},
		{name: "negative active runs", mutate: func(value map[string]any) { value["active_runs"] = -1 }},
		{name: "string count", mutate: func(value map[string]any) { value["active_runs"] = "0" }},
		{name: "fractional count", mutate: func(value map[string]any) { value["cpu_available"] = 1.5 }},
		{name: "offline", mutate: func(value map[string]any) { value["state"] = "offline" }},
		{name: "draining", mutate: func(value map[string]any) { value["state"] = "draining" }},
		{name: "quarantined", mutate: func(value map[string]any) { value["state"] = "quarantined" }},
	} {
		t.Run(test.name, func(t *testing.T) {
			response := workerRecoveryClientResponse(0)
			test.mutate(response)
			server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
				_ = json.NewEncoder(writer).Encode(response)
			}))
			defer server.Close()
			_, err := NewClient(server.URL, strings.Repeat("worker-credential-", 3)).RegisterRecovery(context.Background(), workerRecoveryClientConfig())
			if err == nil || err.Error() != "control response decoding failed" {
				t.Fatalf("invalid Worker was accepted: %v", err)
			}
		})
	}
}

func TestWorkerRecoveryClientRequiresCredentialAndPreservesCancellation(t *testing.T) {
	var requests atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		requests.Add(1)
		_ = json.NewEncoder(writer).Encode(workerRecoveryClientResponse(0))
	}))
	defer server.Close()

	client := NewClient(server.URL, "")
	if _, err := client.RegisterRecovery(context.Background(), workerRecoveryClientConfig()); !errors.Is(err, errCredentialUnavailable) {
		t.Fatalf("missing credential was not rejected: %v", err)
	}
	client = NewClient(server.URL, strings.Repeat("worker-credential-", 3))
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := client.RegisterRecovery(ctx, workerRecoveryClientConfig()); !errors.Is(err, context.Canceled) || err.Error() != context.Canceled.Error() {
		t.Fatalf("cancellation boundary was lost: %v", err)
	}
	if requests.Load() != 0 {
		t.Fatal("a request was sent without a credential or after cancellation")
	}
}

func TestWorkerRecoveryClientDoesNotFollowRedirects(t *testing.T) {
	var redirected atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.URL.Path == "/redirected" {
			redirected.Add(1)
			_ = json.NewEncoder(writer).Encode(workerRecoveryClientResponse(0))
			return
		}
		http.Redirect(writer, request, "/redirected", http.StatusTemporaryRedirect)
	}))
	defer server.Close()
	_, err := NewClient(server.URL, strings.Repeat("worker-credential-", 3)).RegisterRecovery(context.Background(), workerRecoveryClientConfig())
	if err == nil || !strings.Contains(err.Error(), "HTTP 307") {
		t.Fatalf("redirect was accepted: %v", err)
	}
	if redirected.Load() != 0 {
		t.Fatal("redirect target received the Worker credential")
	}
}

func TestWorkerRecoveryClientKeepsDiagnosticsPrivate(t *testing.T) {
	const privateValue = "private-response-payload"
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
		_, _ = io.WriteString(writer, `{"id":"`+privateValue+`","id":"duplicate"}`)
	}))
	defer server.Close()
	_, err := NewClient(server.URL, strings.Repeat("worker-credential-", 3)).RegisterRecovery(context.Background(), workerRecoveryClientConfig())
	if err == nil || err.Error() != "control response decoding failed" ||
		strings.Contains(err.Error(), privateValue) || strings.Contains(err.Error(), server.URL) {
		t.Fatalf("private response details crossed the diagnostic boundary: %v", err)
	}
}
