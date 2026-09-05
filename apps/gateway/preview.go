package main

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"path"
	"regexp"
	"strings"
	"time"
)

const previewCookie = "__Host-kelpie_preview"
const launchScript = `location.replace("/")`
const previewPolicy = "frame-ancestors 'none'; worker-src 'none'"

var previewToken = regexp.MustCompile(`^kpa_[A-Za-z0-9_-]{43}$`)
var previewCode = regexp.MustCompile(`^kpl_[A-Za-z0-9_-]{43}$`)
var previewHost = regexp.MustCompile(`^[a-z0-9-]+(?:\.[a-z0-9-]+)+$`)

func previewHeaders(header http.Header) {
	header.Set("Cache-Control", "no-store")
	header.Set("Referrer-Policy", "no-referrer")
	header.Set("X-Content-Type-Options", "nosniff")
	header.Set("X-Frame-Options", "DENY")
	header.Set("Cross-Origin-Opener-Policy", "same-origin")
}

func (g *gateway) serveOIDC(writer http.ResponseWriter, request *http.Request) {
	previewHeaders(writer.Header())
	if request.TLS == nil {
		http.Error(writer, "HTTPS is required", http.StatusForbidden)
		return
	}
	host := strings.ToLower(request.Host)
	if strings.Contains(host, ":") {
		var err error
		host, _, err = net.SplitHostPort(host)
		if err != nil {
			http.Error(writer, "invalid preview host", http.StatusBadRequest)
			return
		}
	}
	if len(host) > 253 || !previewHost.MatchString(host) || strings.Contains(request.URL.Path, `\`) ||
		path.Clean(request.URL.Path) != request.URL.Path {
		http.Error(writer, "invalid preview request", http.StatusBadRequest)
		return
	}
	if request.Header.Get("Service-Worker") != "" || request.Header.Get("Sec-Fetch-Dest") == "serviceworker" {
		http.Error(writer, "service workers are not supported on protected previews", http.StatusForbidden)
		return
	}
	if request.URL.Path == "/_kelpie/authorize" {
		g.exchangePreview(writer, request, host)
		return
	}
	if strings.HasPrefix(strings.ToLower(request.URL.Path), "/_kelpie") ||
		request.URL.Path == "/console" || strings.HasPrefix(strings.ToLower(request.URL.Path), "/console/") ||
		strings.EqualFold(request.URL.Path, "/console") {
		http.Error(writer, "console access is not available", http.StatusServiceUnavailable)
		return
	}
	// Cookie-authenticated mutations and WebSocket handshakes require this exact origin.
	if site := request.Header.Get("Sec-Fetch-Site"); site == "cross-site" || site == "same-site" {
		http.Error(writer, "cross-origin preview request blocked", http.StatusForbidden)
		return
	}
	if origin := request.Header.Get("Origin"); origin != "" && origin != "https://"+request.Host {
		http.Error(writer, "same-origin request required", http.StatusForbidden)
		return
	}
	if site := request.Header.Get("Sec-Fetch-Site"); site != "same-origin" && site != "none" &&
		request.Header.Get("Origin") != "https://"+request.Host {
		http.Error(writer, "preview request origin is unavailable", http.StatusForbidden)
		return
	}
	if (request.Method != http.MethodGet && request.Method != http.MethodHead) ||
		request.Header.Get("Upgrade") != "" {
		if request.Header.Get("Origin") != "https://"+request.Host {
			http.Error(writer, "same-origin request required", http.StatusForbidden)
			return
		}
	}
	var token string
	count := 0
	for _, cookie := range request.Cookies() {
		if cookie.Name == previewCookie {
			token = cookie.Value
			count++
		}
	}
	if count != 1 || !previewToken.MatchString(token) {
		http.Error(writer, "Open this preview from the Kelpie dashboard. / 대시보드에서 미리보기를 여세요.", http.StatusUnauthorized)
		return
	}
	resolved, code := g.authorizePreview(request.Context(), host, token)
	if code != http.StatusOK {
		http.Error(writer, "Preview unavailable; reopen it from the dashboard. / 대시보드에서 다시 열어 주세요.", code)
		return
	}
	g.proxyPreview(writer, request, host, token, resolved)
}

// Keep credentials in headers, never URLs, and never forward them on API redirects.
func (g *gateway) previewControl(ctx context.Context, method, route, host string, headers http.Header, result any) int {
	endpoint, err := url.Parse(g.config.controlURL + route)
	if err != nil || endpoint.User != nil || endpoint.Host == "" ||
		(endpoint.Scheme != "http" && endpoint.Scheme != "https") || endpoint.RawQuery != "" || endpoint.Fragment != "" {
		return http.StatusBadGateway
	}
	endpoint.RawQuery = url.Values{"host": {host}}.Encode()
	request, err := http.NewRequestWithContext(ctx, method, endpoint.String(), nil)
	if err != nil {
		return http.StatusBadGateway
	}
	request.Header = headers.Clone()
	request.Header.Set("Authorization", "Bearer "+g.config.gatewayToken)
	client := *g.client
	client.Timeout = 10 * time.Second
	client.CheckRedirect = func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse }
	response, err := client.Do(request)
	if err != nil {
		return http.StatusBadGateway
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		switch response.StatusCode {
		case http.StatusUnauthorized, http.StatusForbidden, http.StatusNotFound, http.StatusGone, http.StatusServiceUnavailable:
			return response.StatusCode
		default:
			return http.StatusBadGateway
		}
	}
	if json.NewDecoder(io.LimitReader(response.Body, 65536)).Decode(result) != nil {
		return http.StatusBadGateway
	}
	return http.StatusOK
}

func (g *gateway) authorizePreview(ctx context.Context, host, token string) (resolution, int) {
	var result resolution
	code := g.previewControl(ctx, http.MethodGet, "/internal/previews/authorize", host,
		http.Header{"X-Kelpie-Preview-Token": {token}}, &result)
	if code == http.StatusOK && (result.WorkItemID == "" || !result.ReadOnly || !result.ExpiresAt.After(time.Now())) {
		return resolution{}, http.StatusUnauthorized
	}
	return result, code
}

func (g *gateway) exchangePreview(writer http.ResponseWriter, request *http.Request, host string) {
	if request.Method != http.MethodPost || request.URL.RawQuery != "" ||
		request.Header.Get("Origin") == "" ||
		request.Header.Get("Content-Type") != "application/x-www-form-urlencoded" {
		http.Error(writer, "invalid preview launch", http.StatusBadRequest)
		return
	}
	request.Body = http.MaxBytesReader(writer, request.Body, 1024)
	if request.ParseForm() != nil || len(request.PostForm) != 1 || len(request.PostForm["code"]) != 1 ||
		!previewCode.MatchString(request.PostForm.Get("code")) {
		http.Error(writer, "invalid preview launch", http.StatusBadRequest)
		return
	}
	var exchanged struct {
		Token     string    `json:"token"`
		ExpiresAt time.Time `json:"expires_at"`
	}
	code := g.previewControl(request.Context(), http.MethodPost, "/internal/previews/exchange", host,
		http.Header{"X-Kelpie-Preview-Code": {request.PostForm.Get("code")},
			"X-Kelpie-Launch-Origin": {request.Header.Get("Origin")}}, &exchanged)
	if code != http.StatusOK {
		http.Error(writer, "Preview launch expired or denied; retry from the dashboard. / 대시보드에서 다시 시도해 주세요.", code)
		return
	}
	if !previewToken.MatchString(exchanged.Token) || !exchanged.ExpiresAt.After(time.Now()) ||
		exchanged.ExpiresAt.After(time.Now().Add(5*time.Minute)) {
		http.Error(writer, "invalid preview authorization", http.StatusBadGateway)
		return
	}
	http.SetCookie(writer, &http.Cookie{Name: previewCookie, Value: exchanged.Token, Path: "/",
		HttpOnly: true, Secure: true, SameSite: http.SameSiteStrictMode, Expires: exchanged.ExpiresAt})
	digest := sha256.Sum256([]byte(launchScript))
	writer.Header().Set("Content-Security-Policy", "default-src 'none'; base-uri 'none'; frame-ancestors 'none'; script-src 'sha256-"+
		base64.StdEncoding.EncodeToString(digest[:])+"'")
	writer.Header().Set("Content-Type", "text/html; charset=utf-8")
	// Establish a first-party document before navigation so a Strict cookie is sent.
	fmt.Fprintf(writer, `<!doctype html><meta charset="utf-8"><title>Kelpie Preview</title><p>Opening preview… / 미리보기 여는 중…</p><a href="/">Continue / 계속</a><script>%s</script>`, launchScript)
}

func reservedCookie(name string) bool {
	name = strings.ToLower(name)
	return strings.HasPrefix(name, "kelpie_") || strings.HasPrefix(name, "__host-kelpie_") ||
		strings.HasPrefix(name, "__secure-kelpie_")
}

func (g *gateway) proxyPreview(writer http.ResponseWriter, request *http.Request, host, token string, resolved resolution) {
	target, err := url.Parse(resolved.TargetURL)
	if err != nil || target.Scheme != "http" || target.User != nil || net.ParseIP(target.Hostname()) == nil ||
		target.RawQuery != "" || target.Fragment != "" || (target.Path != "" && target.Path != "/") {
		http.Error(writer, "invalid preview target", http.StatusBadGateway)
		return
	}
	ctx, cancel := context.WithDeadline(request.Context(), resolved.ExpiresAt)
	done := make(chan struct{})
	go func() {
		defer close(done)
		ticker := time.NewTicker(2 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				checkCtx, finish := context.WithTimeout(ctx, 2*time.Second)
				current, code := g.authorizePreview(checkCtx, host, token)
				finish()
				if code != http.StatusOK || current.TargetURL != resolved.TargetURL || current.WorkItemID != resolved.WorkItemID {
					cancel()
					return
				}
			}
		}
	}()
	defer func() { cancel(); <-done }()
	proxy := &httputil.ReverseProxy{
		Rewrite: func(proxyRequest *httputil.ProxyRequest) {
			proxyRequest.SetURL(target)
			proxyRequest.Out.Host = request.Host
			for name := range proxyRequest.Out.Header {
				lower := strings.ToLower(name)
				if strings.HasPrefix(lower, "x-kelpie-") || strings.HasPrefix(lower, "x-forwarded-") ||
					lower == "authorization" || lower == "proxy-authorization" || lower == "forwarded" {
					proxyRequest.Out.Header.Del(name)
				}
			}
			proxyRequest.Out.Header.Del("Cookie")
			for _, cookie := range request.Cookies() {
				if !reservedCookie(cookie.Name) {
					proxyRequest.Out.AddCookie(cookie)
				}
			}
			proxyRequest.Out.Header.Set("X-Kelpie-Work-Item", resolved.WorkItemID)
		},
		ModifyResponse: func(response *http.Response) error {
			if location := response.Header.Get("Location"); location != "" {
				destination, parseErr := url.Parse(location)
				if parseErr != nil {
					return errors.New("invalid preview redirect")
				}
				base := &url.URL{Scheme: "https", Host: request.Host, Path: request.URL.Path}
				destination = base.ResolveReference(destination)
				if destination.Scheme != "https" || destination.Host != request.Host {
					return errors.New("cross-origin preview redirect blocked")
				}
			}
			cookies := response.Cookies()
			response.Header.Del("Set-Cookie")
			for _, cookie := range cookies {
				if !reservedCookie(cookie.Name) {
					cookie.Domain = ""
					cookie.Secure = true
					response.Header.Add("Set-Cookie", cookie.String())
				}
			}
			previewHeaders(response.Header)
			response.Header.Add("Content-Security-Policy", previewPolicy)
			return nil
		},
		ErrorHandler: func(w http.ResponseWriter, _ *http.Request, _ error) {
			http.Error(w, "preview target unavailable", http.StatusBadGateway)
		},
	}
	proxy.ServeHTTP(writer, request.WithContext(ctx))
}
