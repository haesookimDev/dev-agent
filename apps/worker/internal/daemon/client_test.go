package daemon

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
)

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
