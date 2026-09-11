package daemon

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
)

const maxLeaseSnapshotBytes = 4096

type LeaseSnapshot struct {
	LeaseID     string `json:"lease_id"`
	WorkerID    string `json:"worker_id"`
	WorkItemID  string `json:"work_item_id"`
	State       string `json:"state"`
	WorkStatus  string `json:"work_status"`
	WorkVersion int    `json:"work_version"`
	CPU         int    `json:"cpu"`
	MemoryMB    int    `json:"memory_mb"`
	DiskGB      int    `json:"disk_gb"`
}

func validLeaseSnapshot(snapshot LeaseSnapshot) bool {
	if !workUUID.MatchString(snapshot.LeaseID) || !workUUID.MatchString(snapshot.WorkerID) ||
		!workUUID.MatchString(snapshot.WorkItemID) || snapshot.WorkVersion < 1 ||
		snapshot.CPU < 1 || snapshot.MemoryMB < 1 || snapshot.DiskGB < 1 {
		return false
	}
	if snapshot.State != "active" && snapshot.State != "released" {
		return false
	}
	switch snapshot.WorkStatus {
	case "completed", "failed", "cancelled":
		return true
	default:
		return false
	}
}

func (c *Client) leaseRequest(ctx context.Context, method, path string, body any) (*http.Response, error) {
	credential, err := readCredential(c.tokenFile, c.token)
	if err != nil {
		return nil, err
	}
	var reader io.Reader
	if body != nil {
		encoded, err := json.Marshal(body)
		if err != nil {
			return nil, privateFailure(err, controlEncode)
		}
		reader = bytes.NewReader(encoded)
	}
	request, err := http.NewRequestWithContext(ctx, method, c.baseURL+path, reader)
	if err != nil {
		return nil, privateFailure(err, controlRequest)
	}
	request.Header.Set("Authorization", "Bearer "+credential)
	request.Header.Set("Content-Type", "application/json")
	if correlationID, ok := ctx.Value(correlationContextKey{}).(string); ok && correlationID != "" {
		request.Header.Set("X-Kelpie-Correlation-ID", correlationID)
	}

	httpClient := *c.http
	httpClient.CheckRedirect = func(*http.Request, []*http.Request) error {
		return http.ErrUseLastResponse
	}
	response, err := httpClient.Do(request)
	if err != nil {
		return nil, privateFailure(err, controlTransport)
	}
	return response, nil
}

func (c *Client) InspectLease(ctx context.Context, workerID, leaseID string) (LeaseSnapshot, error) {
	var snapshot LeaseSnapshot
	if !workUUID.MatchString(workerID) || !workUUID.MatchString(leaseID) {
		return snapshot, diagnosticError{kind: controlRequest}
	}
	response, err := c.leaseRequest(ctx, http.MethodGet,
		"/api/workers/"+workerID+"/leases/"+leaseID, nil)
	if err != nil {
		return snapshot, err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return snapshot, diagnosticError{kind: controlStatus, code: response.StatusCode}
	}
	data, err := io.ReadAll(io.LimitReader(response.Body, maxLeaseSnapshotBytes+1))
	if err != nil || len(data) > maxLeaseSnapshotBytes {
		return snapshot, privateFailure(err, controlDecode)
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	// Decode each exact contract field once. encoding/json's struct decoder
	// otherwise accepts duplicate keys, case aliases and null scalar values.
	fields := map[string]any{
		"lease_id": &snapshot.LeaseID, "worker_id": &snapshot.WorkerID,
		"work_item_id": &snapshot.WorkItemID, "state": &snapshot.State,
		"work_status": &snapshot.WorkStatus, "work_version": &snapshot.WorkVersion,
		"cpu": &snapshot.CPU, "memory_mb": &snapshot.MemoryMB, "disk_gb": &snapshot.DiskGB,
	}
	if opening, err := decoder.Token(); err != nil || opening != json.Delim('{') {
		return LeaseSnapshot{}, diagnosticError{kind: controlDecode}
	}
	for decoder.More() {
		key, err := decoder.Token()
		name, ok := key.(string)
		target, known := fields[name]
		if err != nil || !ok || !known {
			return LeaseSnapshot{}, diagnosticError{kind: controlDecode}
		}
		var value json.RawMessage
		if err := decoder.Decode(&value); err != nil || bytes.Equal(bytes.TrimSpace(value), []byte("null")) {
			return LeaseSnapshot{}, diagnosticError{kind: controlDecode}
		}
		if err := json.Unmarshal(value, target); err != nil {
			return LeaseSnapshot{}, privateFailure(err, controlDecode)
		}
		delete(fields, name)
	}
	if closing, err := decoder.Token(); err != nil || closing != json.Delim('}') || len(fields) != 0 {
		return LeaseSnapshot{}, diagnosticError{kind: controlDecode}
	}
	var trailing any
	if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) {
		return LeaseSnapshot{}, privateFailure(err, controlDecode)
	}
	if !validLeaseSnapshot(snapshot) || snapshot.WorkerID != workerID || snapshot.LeaseID != leaseID {
		return LeaseSnapshot{}, diagnosticError{kind: controlDecode}
	}
	return snapshot, nil
}

func (c *Client) ReconcileLease(ctx context.Context, workerID string, snapshot LeaseSnapshot) error {
	if !workUUID.MatchString(workerID) || !validLeaseSnapshot(snapshot) || snapshot.WorkerID != workerID {
		return diagnosticError{kind: controlRequest}
	}
	body := struct {
		WorkItemID       string `json:"work_item_id"`
		ExpectedVersion  int    `json:"expected_version"`
		CleanupConfirmed bool   `json:"cleanup_confirmed"`
	}{
		WorkItemID:       snapshot.WorkItemID,
		ExpectedVersion:  snapshot.WorkVersion,
		CleanupConfirmed: true,
	}
	response, err := c.leaseRequest(ctx, http.MethodPost,
		"/api/workers/"+workerID+"/leases/"+snapshot.LeaseID+"/reconcile", body)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusNoContent {
		return diagnosticError{kind: controlStatus, code: response.StatusCode}
	}
	return nil
}
