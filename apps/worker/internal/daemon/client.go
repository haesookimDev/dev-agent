package daemon

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

type Client struct {
	baseURL string
	token   string
	http    *http.Client
}

func NewClient(baseURL, token string) *Client {
	return &Client{
		baseURL: strings.TrimRight(baseURL, "/"),
		token:   token,
		http:    &http.Client{Timeout: 30 * time.Second},
	}
}

func (c *Client) call(ctx context.Context, method, path string, body any, headers map[string]string, result any) error {
	var reader io.Reader
	if body != nil {
		encoded, err := json.Marshal(body)
		if err != nil {
			return err
		}
		reader = bytes.NewReader(encoded)
	}
	request, err := http.NewRequestWithContext(ctx, method, c.baseURL+path, reader)
	if err != nil {
		return err
	}
	request.Header.Set("Content-Type", "application/json")
	for name, value := range headers {
		request.Header.Set(name, value)
	}
	response, err := c.http.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		data, _ := io.ReadAll(io.LimitReader(response.Body, 16<<10))
		return fmt.Errorf("control plane returned %s: %s", response.Status, strings.TrimSpace(string(data)))
	}
	if result == nil || response.StatusCode == http.StatusNoContent {
		return nil
	}
	if err := json.NewDecoder(response.Body).Decode(result); err != nil {
		if errors.Is(err, io.EOF) {
			return nil
		}
		return err
	}
	return nil
}

func (c *Client) workerHeaders() map[string]string {
	return map[string]string{"Authorization": "Bearer " + c.token}
}

func leaseHeaders(token string) map[string]string {
	return map[string]string{"X-Kelpie-Lease": token}
}

func (c *Client) Register(ctx context.Context, config Config) (Worker, error) {
	body := map[string]any{
		"name": config.WorkerName, "cpu_total": config.CPUTotal,
		"memory_mb_total": config.MemoryMBTotal, "disk_gb_available": config.DiskGBTotal,
		"labels": map[string]string{"virtualization": config.Executor},
	}
	var worker Worker
	err := c.call(ctx, http.MethodPost, "/api/workers/register", body, c.workerHeaders(), &worker)
	return worker, err
}

func (c *Client) Heartbeat(ctx context.Context, workerID string, available Resources, active int) error {
	body := map[string]any{
		"state": "online", "cpu_available": available.CPU,
		"memory_mb_available": available.MemoryMB, "disk_gb_available": available.DiskGB,
		"active_runs": active,
	}
	return c.call(ctx, http.MethodPost, "/api/workers/"+workerID+"/heartbeat", body, c.workerHeaders(), nil)
}

func (c *Client) Claim(ctx context.Context, workerID string, resources Resources) (*Claim, error) {
	body := map[string]int{"cpu": resources.CPU, "memory_mb": resources.MemoryMB, "disk_gb": resources.DiskGB}
	var raw json.RawMessage
	if err := c.call(ctx, http.MethodPost, "/api/workers/"+workerID+"/claim", body, c.workerHeaders(), &raw); err != nil {
		return nil, err
	}
	if len(raw) == 0 || string(raw) == "null" {
		return nil, nil
	}
	var claim Claim
	if err := json.Unmarshal(raw, &claim); err != nil {
		return nil, err
	}
	return &claim, nil
}

func (c *Client) Event(ctx context.Context, workID, lease string, event AgentEvent) error {
	return c.call(ctx, http.MethodPost, "/api/runs/"+workID+"/events", event, leaseHeaders(lease), nil)
}

func (c *Client) Transition(ctx context.Context, workID, lease, target string, version int, message string) (WorkItem, error) {
	body := map[string]any{"status": target, "expected_version": version, "message": message, "payload": map[string]any{}}
	var work WorkItem
	err := c.call(ctx, http.MethodPost, "/api/runs/"+workID+"/transition", body, leaseHeaders(lease), &work)
	return work, err
}

func (c *Client) ReadRun(ctx context.Context, workID, lease string) (WorkItem, error) {
	var work WorkItem
	err := c.call(ctx, http.MethodGet, "/api/runs/"+workID, nil, leaseHeaders(lease), &work)
	return work, err
}

func (c *Client) Release(ctx context.Context, workID, lease string) error {
	return c.call(ctx, http.MethodPost, "/api/runs/"+workID+"/release", nil, leaseHeaders(lease), nil)
}
