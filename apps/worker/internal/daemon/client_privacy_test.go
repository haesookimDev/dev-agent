package daemon

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

const privateFixture = "synthetic-private-diagnostic-value"

type privateJSON struct{}

func (privateJSON) MarshalJSON() ([]byte, error) { return nil, errors.New(privateFixture) }

type transportFunc func(*http.Request) (*http.Response, error)

func (f transportFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func assertPrivateError(t *testing.T, err error, expected string) {
	t.Helper()
	if err == nil || strings.Contains(err.Error(), privateFixture) || err.Error() != expected {
		t.Fatal("diagnostic did not match the safe, fixed error contract")
	}
}

func TestControlFailureOmitsBodyAndStatusText(t *testing.T) {
	for _, status := range []int{401, 403, 409, 422, 500, 503} {
		client := NewClient("http://control.invalid", privateFixture)
		client.http.Transport = transportFunc(func(*http.Request) (*http.Response, error) {
			return &http.Response{StatusCode: status, Status: privateFixture,
				Body: io.NopCloser(strings.NewReader(privateFixture))}, nil
		})
		err := client.call(context.Background(), http.MethodGet, "/test", nil, nil, nil)
		if err == nil || strings.Contains(err.Error(), privateFixture) ||
			!strings.Contains(err.Error(), http.StatusText(status)) {
			t.Fatal("HTTP failure retained private text or lost its status classification")
		}
	}
}

func TestControlClientPrivateFailureBoundaries(t *testing.T) {
	for _, scenario := range []struct{ name, expected string }{
		{"encode", "control request encoding failed"},
		{"request", "control request configuration failed"},
		{"transport", "control request failed"},
		{"decode", "control response decoding failed"},
		{"claim", "control response decoding failed"},
	} {
		t.Run(scenario.name, func(t *testing.T) {
			client := NewClient("http://control.invalid", strings.Repeat("test-only-", 4))
			client.http.Transport = transportFunc(func(*http.Request) (*http.Response, error) {
				if scenario.name == "transport" {
					return nil, errors.New(privateFixture)
				}
				body := `{"version":"` + privateFixture + `"}`
				if scenario.name == "claim" {
					body = `{"work_item":{"version":"` + privateFixture + `"}}`
				}
				return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(body))}, nil
			})
			var body any
			if scenario.name == "encode" {
				body = privateJSON{}
			}
			if scenario.name == "request" {
				client.baseURL = "http://" + privateFixture + "/\n"
			}
			var result WorkItem
			err := client.call(context.Background(), http.MethodPost, "/test", body, nil, &result)
			if scenario.name == "claim" {
				_, err = client.Claim(context.Background(), "test-worker", Resources{})
			}
			assertPrivateError(t, err, scenario.expected)
		})
	}
}

func TestControlClientRealHTTPKeepsLeaseAndSafeStatus(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-Kelpie-Lease") != privateFixture || r.Header.Get("Authorization") != "" {
			t.Error("request credential boundary changed")
		}
		w.WriteHeader(http.StatusForbidden)
		_, _ = io.WriteString(w, privateFixture)
	}))
	defer server.Close()
	client := NewClient(server.URL, "")
	_, err := client.ReadRun(context.Background(), "test-work", privateFixture)
	assertPrivateError(t, err, "control plane returned HTTP 403 Forbidden")
}

func TestControlClientPreservesCancellation(t *testing.T) {
	for _, cause := range []error{context.Canceled, context.DeadlineExceeded} {
		client := NewClient("http://control.invalid", "")
		client.http.Transport = transportFunc(func(*http.Request) (*http.Response, error) { return nil, cause })
		err := client.call(context.Background(), http.MethodGet, "/test", nil, nil, nil)
		if !errors.Is(err, cause) || err.Error() != cause.Error() {
			t.Fatal("cancellation was lost or retained a private request URL")
		}
	}
}
