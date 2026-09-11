package daemon

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestClaimKeepsLeaseIdentitySeparateFromCredential(t *testing.T) {
	const leaseID = "22222222-2222-4222-8222-222222222222"
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/workers/test-worker/claim" || r.Header.Get("X-Kelpie-Lease") != "" {
			t.Error("claim crossed the Worker/lease credential boundary")
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"work_item": map[string]string{"id": "33333333-3333-4333-8333-333333333333"},
			"lease_id":  leaseID, "lease_token": "synthetic-only-lease",
		})
	}))
	defer server.Close()
	claim, err := NewClient(server.URL, strings.Repeat("test-only", 4)).Claim(context.Background(), "test-worker", Resources{CPU: 1, MemoryMB: 1024, DiskGB: 5})
	if err != nil || claim == nil || claim.LeaseID != leaseID || claim.LeaseToken != "synthetic-only-lease" || claim.WorkItem.ID == claim.LeaseID {
		t.Fatal("claim lost or confused the persisted lease identity")
	}
}

func TestClientPropagatesCorrelationID(t *testing.T) {
	correlationID := "33333333-3333-4333-8333-333333333333"
	server := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if got := request.Header.Get("X-Kelpie-Correlation-ID"); got != correlationID {
			t.Fatalf("unexpected correlation id %q", got)
		}
		writer.WriteHeader(http.StatusNoContent)
	}))
	defer server.Close()

	client := NewClient(server.URL, "worker-token")
	ctx := ContextWithCorrelationID(context.Background(), correlationID)
	if err := client.call(ctx, http.MethodGet, "/test", nil, nil, nil); err != nil {
		t.Fatal(err)
	}
}
