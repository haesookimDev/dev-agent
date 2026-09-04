package main

import (
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
)

func TestGatewayRequiresIdentity(t *testing.T) {
	handler := &gateway{config: config{authMode: "trusted_headers"}, logger: slog.New(slog.NewTextHandler(io.Discard, nil))}
	request := httptest.NewRequest(http.MethodGet, "http://run.preview.localhost/", nil)
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401, got %d", response.Code)
	}
}

func TestGatewayResolvesAndProxies(t *testing.T) {
	target := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		fmt.Fprint(response, request.Header.Get("X-Kelpie-Work-Item"))
	}))
	defer target.Close()
	control := httptest.NewServer(http.HandlerFunc(func(response http.ResponseWriter, request *http.Request) {
		if request.Header.Get("Authorization") != "Bearer "+strings.Repeat("x", 32) {
			t.Fatal("missing control credential")
		}
		response.Header().Set("content-type", "application/json")
		fmt.Fprintf(response, `{"target_url":%q,"work_item_id":"work-1","read_only":true}`, target.URL)
	}))
	defer control.Close()
	token := strings.Repeat("x", 32)
	handler := &gateway{
		config: config{controlURL: control.URL, workerToken: token, authMode: "development"},
		client: control.Client(), logger: slog.New(slog.NewTextHandler(os.Stderr, nil)),
	}
	request := httptest.NewRequest(http.MethodGet, "http://run.preview.localhost/", nil)
	request.Host = "run.preview.localhost"
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusOK || response.Body.String() != "work-1" {
		t.Fatalf("unexpected response: %d %q", response.Code, response.Body.String())
	}
}
