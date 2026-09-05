package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"
)

type config struct {
	listen       string
	controlURL   string
	gatewayToken string
	authMode     string
}

type resolution struct {
	TargetURL  string `json:"target_url"`
	WorkItemID string `json:"work_item_id"`
	ReadOnly   bool   `json:"read_only"`
}

type gateway struct {
	config config
	client *http.Client
	logger *slog.Logger
}

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	configuration := config{
		listen:       env("KELPIE_GATEWAY_LISTEN", ":8080"),
		controlURL:   strings.TrimRight(env("KELPIE_CONTROL_URL", "http://api:8000"), "/"),
		gatewayToken: os.Getenv("KELPIE_GATEWAY_TOKEN"),
		authMode:     env("KELPIE_GATEWAY_AUTH_MODE", "disabled"),
	}
	if len(configuration.gatewayToken) < 32 {
		logger.Error("KELPIE_GATEWAY_TOKEN must contain at least 32 characters")
		os.Exit(2)
	}
	handler := &gateway{config: configuration, client: &http.Client{Timeout: 10 * time.Second}, logger: logger}
	server := &http.Server{Addr: configuration.listen, Handler: handler, ReadHeaderTimeout: 10 * time.Second}
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	go func() {
		<-ctx.Done()
		shutdown, done := context.WithTimeout(context.Background(), 10*time.Second)
		defer done()
		_ = server.Shutdown(shutdown)
	}()
	logger.Info("preview gateway listening", "address", configuration.listen)
	if err := server.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		logger.Error("gateway stopped", "error", err)
		os.Exit(1)
	}
}

func (g *gateway) ServeHTTP(response http.ResponseWriter, request *http.Request) {
	if g.config.authMode != "development" {
		http.Error(
			response,
			"preview gateway authentication is not configured",
			http.StatusServiceUnavailable,
		)
		return
	}
	host, _, err := net.SplitHostPort(request.Host)
	if err != nil {
		host = request.Host
	}
	isConsole := request.URL.Path == "/console" || strings.HasPrefix(request.URL.Path, "/console/")
	resolved, err := g.resolve(request.Context(), strings.ToLower(host), isConsole)
	if err != nil {
		g.logger.Warn("preview resolution failed", "host", host, "error", err)
		http.Error(response, "preview unavailable", http.StatusBadGateway)
		return
	}
	target, err := url.Parse(resolved.TargetURL)
	if err != nil || target.Scheme != "http" {
		http.Error(response, "invalid preview target", http.StatusBadGateway)
		return
	}
	proxy := httputil.NewSingleHostReverseProxy(target)
	originalDirector := proxy.Director
	proxy.Director = func(outbound *http.Request) {
		originalDirector(outbound)
		if isConsole {
			outbound.URL.Path = strings.TrimPrefix(outbound.URL.Path, "/console")
			if outbound.URL.Path == "" {
				outbound.URL.Path = "/"
			}
		}
		outbound.Header.Set("X-Kelpie-Work-Item", resolved.WorkItemID)
		outbound.Header.Set("X-Kelpie-Console-Read-Only", fmt.Sprint(resolved.ReadOnly))
	}
	proxy.ErrorHandler = func(writer http.ResponseWriter, _ *http.Request, proxyErr error) {
		g.logger.Warn("preview proxy failed", "work_id", resolved.WorkItemID, "error", proxyErr)
		http.Error(writer, "preview target unavailable", http.StatusBadGateway)
	}
	proxy.ServeHTTP(response, request)
}

func (g *gateway) resolve(ctx context.Context, host string, console bool) (resolution, error) {
	endpoint, err := url.Parse(g.config.controlURL + "/internal/previews/resolve")
	if err != nil {
		return resolution{}, err
	}
	query := endpoint.Query()
	query.Set("host", host)
	query.Set("console", fmt.Sprint(console))
	endpoint.RawQuery = query.Encode()
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint.String(), nil)
	if err != nil {
		return resolution{}, err
	}
	request.Header.Set("Authorization", "Bearer "+g.config.gatewayToken)
	response, err := g.client.Do(request)
	if err != nil {
		return resolution{}, err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return resolution{}, fmt.Errorf("control plane returned %s", response.Status)
	}
	var result resolution
	if err := json.NewDecoder(response.Body).Decode(&result); err != nil {
		return resolution{}, err
	}
	return result, nil
}

func env(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}
