package main

import (
	"bufio"
	"crypto/tls"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

const testPreviewHost = "work.preview.example.net"

func oidcRequest(method, path string, body io.Reader) *http.Request {
	request := httptest.NewRequest(method, "https://"+testPreviewHost+path, body)
	request.AddCookie(&http.Cookie{Name: previewCookie, Value: "kpa_" + strings.Repeat("x", 43)})
	request.Header.Set("Sec-Fetch-Site", "same-origin")
	return request
}

func oidcGateway(t *testing.T, target http.Handler, controlHandler http.Handler) (*gateway, *httptest.Server) {
	t.Helper()
	upstream := httptest.NewServer(target)
	t.Cleanup(upstream.Close)
	if controlHandler == nil {
		controlHandler = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path != "/internal/previews/authorize" || r.URL.Query().Get("host") != testPreviewHost ||
				r.Header.Get("X-Kelpie-Preview-Token") != "kpa_"+strings.Repeat("x", 43) ||
				r.Header.Get("Authorization") != "Bearer "+strings.Repeat("s", 32) {
				t.Error("missing scoped control authorization")
			}
			fmt.Fprintf(w, `{"target_url":%q,"work_item_id":"work","read_only":true,"expires_at":%q}`,
				upstream.URL, time.Now().Add(time.Minute).Format(time.RFC3339Nano))
		})
	}
	control := httptest.NewServer(controlHandler)
	t.Cleanup(control.Close)
	g := &gateway{config: config{authMode: "oidc", gatewayToken: strings.Repeat("s", 32), controlURL: control.URL},
		client: control.Client(), logger: slog.New(slog.NewTextHandler(io.Discard, nil))}
	return g, upstream
}

func TestOIDCPreviewStripsControlCredentialsAndIsolatesResponseCookies(t *testing.T) {
	g, _ := oidcGateway(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "" || r.Header.Get("X-Kelpie-User") != "" ||
			r.Header.Get("X-Kelpie-Preview-Token") != "" || r.Header.Get("Forwarded") != "" ||
			r.Header.Get("X-Forwarded-Host") != "" || r.Header.Get("X-Kelpie-Work-Item") != "work" {
			t.Error("unexpected identity forwarding")
		}
		if r.Header.Get("Cookie") != "app_session=synthetic" {
			t.Errorf("unexpected upstream cookies: names/count must match application cookie only")
		}
		w.Header().Add("Set-Cookie", previewCookie+"=forged; Secure; Path=/")
		w.Header().Add("Set-Cookie", "kelpie_session=forged; Domain=example.net; Path=/")
		w.Header().Add("Set-Cookie", "app_session=next; Domain=example.net; Path=/; HttpOnly")
		w.Header().Set("Cache-Control", "public, max-age=86400")
		fmt.Fprint(w, "preview")
	}), nil)
	r := oidcRequest(http.MethodGet, "/", nil)
	r.AddCookie(&http.Cookie{Name: "kelpie_session", Value: "synthetic-control-session"})
	r.AddCookie(&http.Cookie{Name: "app_session", Value: "synthetic"})
	r.Header.Set("Authorization", "Bearer synthetic-secret")
	r.Header.Set("X-Kelpie-User", "forged")
	r.Header.Set("X-Kelpie-Preview-Token", "forged")
	r.Header.Set("X-Forwarded-Host", "forged")
	r.Header.Set("Forwarded", "host=forged")
	r.Header.Set("Connection", "X-Kelpie-Work-Item")
	w := httptest.NewRecorder()
	g.ServeHTTP(w, r)
	if w.Code != 200 || w.Body.String() != "preview" || w.Header().Get("Cache-Control") != "no-store" {
		t.Fatalf("unexpected preview response: %d", w.Code)
	}
	cookies := w.Result().Cookies()
	if len(cookies) != 1 || cookies[0].Name != "app_session" || cookies[0].Domain != "" || !cookies[0].Secure {
		t.Fatal("upstream altered control cookie or shared app cookies across hosts")
	}
	if !strings.Contains(w.Header().Get("Content-Security-Policy"), "worker-src 'none'") {
		t.Fatal("service worker policy missing")
	}
}

func TestOIDCPreviewRejectsCrossOriginAndUnprotectedRequestsBeforeResolving(t *testing.T) {
	cases := []struct {
		name string
		edit func(*http.Request)
		code int
	}{
		{"plain HTTP", func(r *http.Request) { r.TLS = nil; r.Header.Set("X-Forwarded-Proto", "https") }, 403},
		{"missing cookie", func(r *http.Request) { r.Header.Del("Cookie") }, 401},
		{"duplicate cookie", func(r *http.Request) { r.AddCookie(&http.Cookie{Name: previewCookie, Value: "forged"}) }, 401},
		{"wrong origin GET", func(r *http.Request) { r.Header.Set("Origin", "https://other.preview.example.net") }, 403},
		{"cross-site", func(r *http.Request) { r.Header.Set("Sec-Fetch-Site", "cross-site") }, 403},
		{"sibling-site", func(r *http.Request) { r.Header.Set("Sec-Fetch-Site", "same-site") }, 403},
		{"missing provenance", func(r *http.Request) { r.Header.Del("Sec-Fetch-Site") }, 403},
		{"POST without Origin", func(r *http.Request) { r.Method = "POST" }, 403},
		{"WebSocket without Origin", func(r *http.Request) { r.Header.Set("Upgrade", "websocket") }, 403},
		{"service worker", func(r *http.Request) { r.Header.Set("Service-Worker", "script") }, 403},
		{"console", func(r *http.Request) { r.URL.Path = "/console" }, 503},
		{"encoded console", func(r *http.Request) { r.URL.Path = "/CONSOLE/socket" }, 503},
		{"ambiguous path", func(r *http.Request) { r.URL.Path = "//console/socket" }, 400},
		{"dot path", func(r *http.Request) { r.URL.Path = "/a/../console" }, 400},
		{"reserved namespace", func(r *http.Request) { r.URL.Path = "/_kelpie/fake" }, 503},
		{"forged host", func(r *http.Request) { r.Host = "work.preview.example.net@127.0.0.1" }, 400},
	}
	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			g := &gateway{config: config{authMode: "oidc"}}
			r := oidcRequest(http.MethodGet, "/", nil)
			test.edit(r)
			w := httptest.NewRecorder()
			g.ServeHTTP(w, r)
			if w.Code != test.code || w.Header().Get("Cache-Control") != "no-store" {
				t.Fatalf("expected %d, got %d", test.code, w.Code)
			}
		})
	}
}

func TestOIDCLaunchExchangesCodeIntoHostOnlyHTTPOnlyCookie(t *testing.T) {
	launch := "kpl_" + strings.Repeat("c", 43)
	token := "kpa_" + strings.Repeat("t", 43)
	g, _ := oidcGateway(t, http.NotFoundHandler(), http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "POST" || r.URL.Path != "/internal/previews/exchange" ||
			r.Header.Get("X-Kelpie-Preview-Code") != launch ||
			r.Header.Get("X-Kelpie-Launch-Origin") != "https://dashboard.example.com" ||
			strings.Contains(r.URL.String(), launch) || r.Header.Get("Cookie") != "" {
			t.Error("invalid code exchange boundary")
		}
		fmt.Fprintf(w, `{"token":%q,"expires_at":%q}`, token, time.Now().Add(time.Minute).Format(time.RFC3339Nano))
	}))
	r := oidcRequest(http.MethodPost, "/_kelpie/authorize", strings.NewReader("code="+launch))
	r.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	r.Header.Set("Origin", "https://dashboard.example.com")
	r.Header.Set("Sec-Fetch-Site", "cross-site")
	w := httptest.NewRecorder()
	g.ServeHTTP(w, r)
	if w.Code != 200 || strings.Contains(w.Body.String(), token) || strings.Contains(w.Body.String(), launch) {
		t.Fatalf("invalid bootstrap response: %d", w.Code)
	}
	cookies := w.Result().Cookies()
	if len(cookies) != 1 || cookies[0].Value != token || cookies[0].Domain != "" || cookies[0].Path != "/" ||
		!cookies[0].Secure || !cookies[0].HttpOnly || cookies[0].SameSite != http.SameSiteStrictMode {
		t.Fatal("unsafe preview cookie")
	}
	if !strings.Contains(w.Header().Get("Content-Security-Policy"), "script-src 'sha256-") {
		t.Fatal("bootstrap script not protected")
	}
}

func TestOIDCLaunchRejectsAmbiguousForms(t *testing.T) {
	for _, body := range []string{"", "code=invalid", "code=x&code=y", "code=x&next=https://evil.example", strings.Repeat("x", 2048)} {
		g := &gateway{config: config{authMode: "oidc"}}
		r := oidcRequest(http.MethodPost, "/_kelpie/authorize", strings.NewReader(body))
		r.Header.Set("Content-Type", "application/x-www-form-urlencoded")
		r.Header.Set("Origin", "https://dashboard.example.com")
		w := httptest.NewRecorder()
		g.ServeHTTP(w, r)
		if w.Code != 400 || len(w.Result().Cookies()) != 0 {
			t.Fatalf("accepted invalid launch: %d", w.Code)
		}
	}
}

func TestOIDCControlRedirectDoesNotLeakCredentials(t *testing.T) {
	var reached atomic.Bool
	other := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { reached.Store(true) }))
	defer other.Close()
	g, _ := oidcGateway(t, http.NotFoundHandler(), http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, other.URL, http.StatusTemporaryRedirect)
	}))
	w := httptest.NewRecorder()
	g.ServeHTTP(w, oidcRequest(http.MethodGet, "/", nil))
	if w.Code != 502 || reached.Load() {
		t.Fatal("control credential followed redirect")
	}
}

func TestOIDCProxyBlocksCrossOriginRedirect(t *testing.T) {
	for _, location := range []string{"http://169.254.169.254/latest/meta-data", "//other.preview.example.net/", "https://evil.example/"} {
		g, _ := oidcGateway(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			http.Redirect(w, r, location, 302)
		}), nil)
		w := httptest.NewRecorder()
		g.ServeHTTP(w, oidcRequest(http.MethodGet, "/", nil))
		if w.Code != 502 || w.Header().Get("Location") != "" {
			t.Fatal("cross-origin redirect escaped preview")
		}
	}
}

func TestOIDCWebSocketClosesWhenAccessEnds(t *testing.T) {
	for _, cause := range []string{"revoked", "expired", "target_changed"} {
		t.Run(cause, func(t *testing.T) {
			var revoked atomic.Bool
			var targetURL atomic.Value
			expiry := time.Now().Add(time.Minute)
			if cause == "expired" {
				expiry = time.Now().Add(3 * time.Second)
			}
			g, target := oidcGateway(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				connection, buffer, err := w.(http.Hijacker).Hijack()
				if err != nil {
					return
				}
				defer connection.Close()
				fmt.Fprint(buffer, "HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n")
				buffer.Flush()
				io.Copy(connection, connection)
			}), http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if revoked.Load() {
					w.WriteHeader(401)
					return
				}
				fmt.Fprintf(w, `{"target_url":%q,"work_item_id":"work","read_only":true,"expires_at":%q}`,
					targetURL.Load().(string), expiry.Format(time.RFC3339Nano))
			}))
			targetURL.Store(target.URL)
			server := httptest.NewTLSServer(g)
			defer server.Close()
			connection, err := tls.DialWithDialer(&net.Dialer{Timeout: 2 * time.Second}, "tcp",
				server.Listener.Addr().String(), server.Client().Transport.(*http.Transport).TLSClientConfig)
			if err != nil {
				t.Fatal(err)
			}
			defer connection.Close()
			fmt.Fprintf(connection, "GET /socket HTTP/1.1\r\nHost: %s\r\nOrigin: https://%s\r\nConnection: Upgrade\r\nUpgrade: websocket\r\nCookie: %s=kpa_%s\r\n\r\n",
				testPreviewHost, testPreviewHost, previewCookie, strings.Repeat("x", 43))
			connection.SetDeadline(time.Now().Add(4 * time.Second))
			reader := bufio.NewReader(connection)
			response, err := http.ReadResponse(reader, &http.Request{Method: "GET"})
			if err != nil {
				t.Fatal(err)
			}
			if response.StatusCode != 101 {
				t.Fatalf("upgrade returned %d", response.StatusCode)
			}
			if _, err := connection.Write([]byte("ping")); err != nil {
				t.Fatal(err)
			}
			payload := make([]byte, 4)
			if _, err := io.ReadFull(reader, payload); err != nil || string(payload) != "ping" {
				t.Fatalf("tunnel echo failed: %v", err)
			}
			if cause == "revoked" {
				revoked.Store(true)
			}
			if cause == "target_changed" {
				targetURL.Store("http://127.0.0.1:9")
			}
			connection.SetReadDeadline(time.Now().Add(4 * time.Second))
			_, err = reader.ReadByte()
			if err == nil {
				t.Fatal("revoked connection still readable")
			}
			if networkError, ok := err.(net.Error); ok && networkError.Timeout() {
				t.Fatal("revoked connection was not closed within the authorization-check deadline")
			}
		})
	}
}

func TestOIDCExpiredGrantIsRejected(t *testing.T) {
	g, _ := oidcGateway(t, http.NotFoundHandler(), http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fmt.Fprintf(w, `{"target_url":"http://127.0.0.1:1234","work_item_id":"work","read_only":true,"expires_at":%q}`,
			time.Now().Add(-time.Second).Format(time.RFC3339Nano))
	}))
	w := httptest.NewRecorder()
	g.ServeHTTP(w, oidcRequest(http.MethodGet, "/", nil))
	if w.Code != 401 {
		t.Fatalf("expired grant returned %d", w.Code)
	}
}
