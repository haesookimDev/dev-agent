package daemon

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

const (
	leaseClientWorkerID = "11111111-1111-4111-8111-111111111111"
	leaseClientLeaseID  = "22222222-2222-4222-8222-222222222222"
	leaseClientWorkID   = "33333333-3333-4333-8333-333333333333"
)

func leaseClientSnapshot() LeaseSnapshot {
	return LeaseSnapshot{
		LeaseID: leaseClientLeaseID, WorkerID: leaseClientWorkerID, WorkItemID: leaseClientWorkID,
		State: "active", WorkStatus: "failed", WorkVersion: 3,
		CPU: 2, MemoryMB: 4096, DiskGB: 30,
	}
}

func TestLeaseClientInspectAndReconcileUseWorkerCredential(t *testing.T) {
	firstCredential := strings.Repeat("first-credential-", 3)
	secondCredential := strings.Repeat("second-credential-", 3)
	credentialPath := t.TempDir() + "/worker-token"
	if err := os.WriteFile(credentialPath, []byte(firstCredential+"\n"), 0600); err != nil {
		t.Fatal(err)
	}

	var requests atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		requestNumber := requests.Add(1)
		if request.Header.Get("X-Kelpie-Lease") != "" {
			t.Error("lease credential was sent on a Worker-authenticated request")
		}
		if request.Header.Get("Content-Type") != "application/json" {
			t.Error("JSON content type was not sent")
		}
		switch requestNumber {
		case 1:
			if request.Method != http.MethodGet ||
				request.URL.Path != "/api/workers/"+leaseClientWorkerID+"/leases/"+leaseClientLeaseID ||
				request.Header.Get("Authorization") != "Bearer "+firstCredential {
				t.Error("inspect request did not match its method, path, or credential")
			}
			_ = json.NewEncoder(writer).Encode(leaseClientSnapshot())
		case 2:
			if request.Method != http.MethodPost ||
				request.URL.Path != "/api/workers/"+leaseClientWorkerID+"/leases/"+leaseClientLeaseID+"/reconcile" ||
				request.Header.Get("Authorization") != "Bearer "+secondCredential {
				t.Error("reconcile request did not match its method, path, or reloaded credential")
			}
			data, err := io.ReadAll(request.Body)
			if err != nil || string(data) != `{"work_item_id":"`+leaseClientWorkID+`","expected_version":3,"cleanup_confirmed":true}` {
				t.Errorf("unexpected reconciliation body %q", data)
			}
			writer.WriteHeader(http.StatusNoContent)
		default:
			t.Error("unexpected extra request")
		}
	}))
	defer server.Close()

	client := NewClient(server.URL, strings.Repeat("unused-fallback-", 3))
	client.tokenFile = credentialPath
	snapshot, err := client.InspectLease(context.Background(), leaseClientWorkerID, leaseClientLeaseID)
	if err != nil || snapshot != leaseClientSnapshot() {
		t.Fatalf("inspect failed: snapshot=%+v err=%v", snapshot, err)
	}
	if err := os.WriteFile(credentialPath, []byte(secondCredential+"\n"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := client.ReconcileLease(context.Background(), leaseClientWorkerID, snapshot); err != nil {
		t.Fatal(err)
	}
	if requests.Load() != 2 {
		t.Fatal("expected one inspect and one reconcile request")
	}
}

func TestLeaseClientRejectsInvalidInputsBeforeRequest(t *testing.T) {
	var requests atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		requests.Add(1)
	}))
	defer server.Close()
	client := NewClient(server.URL, strings.Repeat("worker-credential-", 3))

	for _, test := range []struct {
		name     string
		workerID string
		snapshot LeaseSnapshot
		inspect  bool
		leaseID  string
	}{
		{name: "inspect worker path", inspect: true, workerID: "../worker", leaseID: leaseClientLeaseID},
		{name: "inspect lease path", inspect: true, workerID: leaseClientWorkerID, leaseID: "not-a-uuid"},
		{name: "reconcile worker path", workerID: "not-a-uuid", snapshot: leaseClientSnapshot()},
		{name: "worker mismatch", workerID: "44444444-4444-4444-8444-444444444444", snapshot: leaseClientSnapshot()},
		{name: "lease identity", workerID: leaseClientWorkerID, snapshot: func() LeaseSnapshot { value := leaseClientSnapshot(); value.LeaseID = "bad"; return value }()},
		{name: "work identity", workerID: leaseClientWorkerID, snapshot: func() LeaseSnapshot { value := leaseClientSnapshot(); value.WorkItemID = "bad"; return value }()},
		{name: "version", workerID: leaseClientWorkerID, snapshot: func() LeaseSnapshot { value := leaseClientSnapshot(); value.WorkVersion = 0; return value }()},
		{name: "cpu", workerID: leaseClientWorkerID, snapshot: func() LeaseSnapshot { value := leaseClientSnapshot(); value.CPU = 0; return value }()},
		{name: "memory", workerID: leaseClientWorkerID, snapshot: func() LeaseSnapshot { value := leaseClientSnapshot(); value.MemoryMB = 0; return value }()},
		{name: "disk", workerID: leaseClientWorkerID, snapshot: func() LeaseSnapshot { value := leaseClientSnapshot(); value.DiskGB = 0; return value }()},
		{name: "lease state", workerID: leaseClientWorkerID, snapshot: func() LeaseSnapshot { value := leaseClientSnapshot(); value.State = "expired"; return value }()},
		{name: "work state", workerID: leaseClientWorkerID, snapshot: func() LeaseSnapshot { value := leaseClientSnapshot(); value.WorkStatus = "running"; return value }()},
	} {
		t.Run(test.name, func(t *testing.T) {
			var err error
			if test.inspect {
				_, err = client.InspectLease(context.Background(), test.workerID, test.leaseID)
			} else {
				err = client.ReconcileLease(context.Background(), test.workerID, test.snapshot)
			}
			if err == nil || err.Error() != "control request configuration failed" {
				t.Fatalf("invalid input was not safely rejected: %v", err)
			}
		})
	}
	if requests.Load() != 0 {
		t.Fatal("an invalid input reached the server")
	}
}

func TestLeaseClientRejectsInvalidInspectResponses(t *testing.T) {
	valid, err := json.Marshal(leaseClientSnapshot())
	if err != nil {
		t.Fatal(err)
	}
	for _, test := range []struct {
		name   string
		status int
		body   string
	}{
		{name: "empty", status: http.StatusOK},
		{name: "null", status: http.StatusOK, body: "null"},
		{name: "duplicate status", status: http.StatusOK, body: strings.TrimSuffix(string(valid), "}") + `,"work_status":"failed"}`},
		{name: "null duplicate", status: http.StatusOK, body: strings.TrimSuffix(string(valid), "}") + `,"work_version":null}`},
		{name: "case alias", status: http.StatusOK, body: strings.Replace(string(valid), `"work_status"`, `"WORK_STATUS"`, 1)},
		{name: "malformed", status: http.StatusOK, body: "{"},
		{name: "trailing value", status: http.StatusOK, body: string(valid) + `{}`},
		{name: "unknown field", status: http.StatusOK, body: strings.TrimSuffix(string(valid), "}") + `,"secret":"private"}`},
		{name: "oversized", status: http.StatusOK, body: string(valid) + strings.Repeat(" ", maxLeaseSnapshotBytes)},
		{name: "wrong status", status: http.StatusCreated, body: string(valid)},
		{name: "no content", status: http.StatusNoContent},
		{name: "lease mismatch", status: http.StatusOK, body: strings.Replace(string(valid), leaseClientLeaseID, "44444444-4444-4444-8444-444444444444", 1)},
		{name: "worker mismatch", status: http.StatusOK, body: strings.Replace(string(valid), leaseClientWorkerID, "44444444-4444-4444-8444-444444444444", 1)},
		{name: "work id", status: http.StatusOK, body: strings.Replace(string(valid), leaseClientWorkID, "invalid", 1)},
		{name: "version", status: http.StatusOK, body: strings.Replace(string(valid), `"work_version":3`, `"work_version":0`, 1)},
		{name: "cpu", status: http.StatusOK, body: strings.Replace(string(valid), `"cpu":2`, `"cpu":0`, 1)},
		{name: "memory", status: http.StatusOK, body: strings.Replace(string(valid), `"memory_mb":4096`, `"memory_mb":0`, 1)},
		{name: "disk", status: http.StatusOK, body: strings.Replace(string(valid), `"disk_gb":30`, `"disk_gb":0`, 1)},
		{name: "lease state", status: http.StatusOK, body: strings.Replace(string(valid), `"state":"active"`, `"state":"expired"`, 1)},
		{name: "work state", status: http.StatusOK, body: strings.Replace(string(valid), `"work_status":"failed"`, `"work_status":"running"`, 1)},
		{name: "wrong type", status: http.StatusOK, body: strings.Replace(string(valid), `"cpu":2`, `"cpu":"2"`, 1)},
	} {
		t.Run(test.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
				writer.WriteHeader(test.status)
				_, _ = io.WriteString(writer, test.body)
			}))
			defer server.Close()
			client := NewClient(server.URL, strings.Repeat("worker-credential-", 3))
			_, err := client.InspectLease(context.Background(), leaseClientWorkerID, leaseClientLeaseID)
			if err == nil || strings.Contains(err.Error(), "private") || strings.Contains(err.Error(), server.URL) {
				t.Fatalf("invalid response was not safely rejected: %v", err)
			}
		})
	}
}

func TestLeaseClientDoesNotFollowRedirects(t *testing.T) {
	var redirected atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.URL.Path == "/redirected" {
			redirected.Add(1)
			_ = json.NewEncoder(writer).Encode(leaseClientSnapshot())
			return
		}
		http.Redirect(writer, request, "/redirected", http.StatusTemporaryRedirect)
	}))
	defer server.Close()
	client := NewClient(server.URL, strings.Repeat("worker-credential-", 3))
	if _, err := client.InspectLease(context.Background(), leaseClientWorkerID, leaseClientLeaseID); err == nil {
		t.Fatal("redirect was accepted")
	}
	if redirected.Load() != 0 {
		t.Fatal("redirect target received the Worker credential")
	}
}

func TestLeaseClientReconcileRequiresExactNoContent(t *testing.T) {
	for _, status := range []int{http.StatusOK, http.StatusAccepted, http.StatusResetContent, http.StatusBadRequest, http.StatusInternalServerError} {
		t.Run(http.StatusText(status), func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) {
				writer.WriteHeader(status)
				_, _ = io.WriteString(writer, "private server response")
			}))
			defer server.Close()
			client := NewClient(server.URL, strings.Repeat("worker-credential-", 3))
			err := client.ReconcileLease(context.Background(), leaseClientWorkerID, leaseClientSnapshot())
			if err == nil || strings.Contains(err.Error(), "private") || !strings.Contains(err.Error(), http.StatusText(status)) {
				t.Fatalf("status %d was not safely rejected: %v", status, err)
			}
		})
	}
}

func TestLeaseClientPreservesCancellationAndTimeout(t *testing.T) {
	for _, test := range []struct {
		name    string
		timeout bool
	}{
		{name: "context cancellation"},
		{name: "HTTP client timeout", timeout: true},
	} {
		t.Run(test.name, func(t *testing.T) {
			started := make(chan struct{})
			server := httptest.NewServer(http.HandlerFunc(func(_ http.ResponseWriter, request *http.Request) {
				close(started)
				select {
				case <-request.Context().Done():
				case <-time.After(2 * time.Second):
					t.Error("request context was not cancelled")
				}
			}))
			defer server.Close()
			client := NewClient(server.URL, strings.Repeat("worker-credential-", 3))
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			if test.timeout {
				client.http.Timeout = 20 * time.Millisecond
			}
			done := make(chan error, 1)
			go func() {
				_, err := client.InspectLease(ctx, leaseClientWorkerID, leaseClientLeaseID)
				done <- err
			}()
			var err error
			select {
			case <-started:
				if !test.timeout {
					cancel()
				}
				err = <-done
			case err = <-done:
				if !test.timeout {
					t.Fatal("request ended before its context was cancelled")
				}
			}
			expected := context.Canceled
			if test.timeout {
				expected = context.DeadlineExceeded
			}
			if !errors.Is(err, expected) || err.Error() != expected.Error() {
				t.Fatalf("cancellation boundary was lost: %v", err)
			}
		})
	}
}

func TestLeaseClientRequiresCredential(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) {
		t.Error("request was sent without an individual Worker credential")
	}))
	defer server.Close()
	client := NewClient(server.URL, "")
	_, inspectErr := client.InspectLease(context.Background(), leaseClientWorkerID, leaseClientLeaseID)
	reconcileErr := client.ReconcileLease(context.Background(), leaseClientWorkerID, leaseClientSnapshot())
	if !errors.Is(inspectErr, errCredentialUnavailable) || !errors.Is(reconcileErr, errCredentialUnavailable) {
		t.Fatal("missing credential was not rejected")
	}
}
