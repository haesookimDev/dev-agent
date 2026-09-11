package daemon

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"
)

const maxWorkerRecoveryBytes = 4096

var workerRecoveryRequiredFields = []string{
	"id",
	"name",
	"state",
	"cpu_total",
	"cpu_available",
	"memory_mb_total",
	"memory_mb_available",
	"disk_gb_available",
	"active_runs",
}

// RegisterRecovery re-establishes a libvirt Worker's control-plane identity
// without treating a partial or ambiguous registration response as capacity
// that is safe to admit new work.
func (c *Client) RegisterRecovery(ctx context.Context, config Config) (Worker, error) {
	var worker Worker
	if config.Executor != "libvirt" || config.WorkerName == "" || config.CPUTotal < 1 ||
		config.MemoryMBTotal < 1024 || config.DiskGBTotal < 1 {
		return worker, diagnosticError{kind: controlRequest}
	}
	body := map[string]any{
		"name": config.WorkerName, "cpu_total": config.CPUTotal,
		"memory_mb_total": config.MemoryMBTotal, "disk_gb_available": config.DiskGBTotal,
		"labels": map[string]string{"virtualization": config.Executor},
	}
	response, err := c.leaseRequest(ctx, http.MethodPost, "/api/workers/register", body)
	if err != nil {
		return worker, err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return worker, diagnosticError{kind: controlStatus, code: response.StatusCode}
	}
	data, err := io.ReadAll(io.LimitReader(response.Body, maxWorkerRecoveryBytes+1))
	if err != nil || len(data) > maxWorkerRecoveryBytes {
		return worker, privateFailure(err, controlDecode)
	}
	if err := validateWorkerRecoveryJSON(data); err != nil {
		return worker, diagnosticError{kind: controlDecode}
	}

	var fields map[string]json.RawMessage
	if err := json.Unmarshal(data, &fields); err != nil || fields == nil {
		return worker, diagnosticError{kind: controlDecode}
	}
	for name := range fields {
		for _, required := range workerRecoveryRequiredFields {
			if name != required && strings.EqualFold(name, required) {
				return Worker{}, diagnosticError{kind: controlDecode}
			}
		}
	}
	var state string
	values := map[string]any{
		"id": &worker.ID, "name": &worker.Name, "state": &state,
		"cpu_total": &worker.CPUTotal, "cpu_available": &worker.CPUAvailable,
		"memory_mb_total": &worker.MemoryMBTotal, "memory_mb_available": &worker.MemoryMBAvailable,
		"disk_gb_available": &worker.DiskGBAvailable, "active_runs": &worker.ActiveRuns,
	}
	for _, name := range workerRecoveryRequiredFields {
		raw, ok := fields[name]
		if !ok || bytes.Equal(bytes.TrimSpace(raw), []byte("null")) || json.Unmarshal(raw, values[name]) != nil {
			return Worker{}, diagnosticError{kind: controlDecode}
		}
	}
	if raw, ok := fields["labels"]; ok {
		if bytes.Equal(bytes.TrimSpace(raw), []byte("null")) || json.Unmarshal(raw, &worker.Labels) != nil {
			return Worker{}, diagnosticError{kind: controlDecode}
		}
	}
	if !workUUID.MatchString(worker.ID) || worker.Name != config.WorkerName || state != "online" ||
		worker.CPUTotal != config.CPUTotal || worker.MemoryMBTotal != config.MemoryMBTotal ||
		worker.CPUAvailable < 0 || worker.CPUAvailable > worker.CPUTotal ||
		worker.MemoryMBAvailable < 0 || worker.MemoryMBAvailable > worker.MemoryMBTotal ||
		worker.DiskGBAvailable < 0 || worker.DiskGBAvailable > config.DiskGBTotal || worker.ActiveRuns < 0 {
		return Worker{}, diagnosticError{kind: controlDecode}
	}
	return worker, nil
}

func validateWorkerRecoveryJSON(data []byte) error {
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.UseNumber()
	if err := consumeWorkerRecoveryJSONValue(decoder); err != nil {
		return err
	}
	var trailing any
	if err := decoder.Decode(&trailing); !errors.Is(err, io.EOF) {
		return errors.New("trailing JSON value")
	}
	return nil
}

func consumeWorkerRecoveryJSONValue(decoder *json.Decoder) error {
	token, err := decoder.Token()
	if err != nil {
		return err
	}
	delimiter, ok := token.(json.Delim)
	if !ok {
		return nil
	}
	switch delimiter {
	case '{':
		seen := make(map[string]struct{})
		for decoder.More() {
			key, err := decoder.Token()
			if err != nil {
				return err
			}
			name, ok := key.(string)
			if !ok {
				return errors.New("non-string JSON object key")
			}
			if _, duplicate := seen[name]; duplicate {
				return errors.New("duplicate JSON object key")
			}
			seen[name] = struct{}{}
			if err := consumeWorkerRecoveryJSONValue(decoder); err != nil {
				return err
			}
		}
		closing, err := decoder.Token()
		if err != nil || closing != json.Delim('}') {
			return errors.New("invalid JSON object")
		}
	case '[':
		for decoder.More() {
			if err := consumeWorkerRecoveryJSONValue(decoder); err != nil {
				return err
			}
		}
		closing, err := decoder.Token()
		if err != nil || closing != json.Delim(']') {
			return errors.New("invalid JSON array")
		}
	default:
		return errors.New("invalid JSON delimiter")
	}
	return nil
}
