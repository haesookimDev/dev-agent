package daemon

import (
	"context"
	"errors"
	"fmt"
	"net/http"
)

type diagnosticKind uint8

const (
	controlEncode diagnosticKind = iota + 1
	controlRequest
	controlTransport
	controlDecode
	controlStatus
)

// Only fixed classifications and numeric codes cross the diagnostic boundary.
// Do not retain or unwrap the original error: URLs, paths and response bodies
// can contain credentials even when the current lease is not known here.
type diagnosticError struct {
	kind diagnosticKind
	code int
}

func (e diagnosticError) Error() string {
	switch e.kind {
	case controlEncode:
		return "control request encoding failed"
	case controlRequest:
		return "control request configuration failed"
	case controlTransport:
		return "control request failed"
	case controlDecode:
		return "control response decoding failed"
	case controlStatus:
		return fmt.Sprintf("control plane returned HTTP %d %s", e.code, http.StatusText(e.code))
	default:
		return "worker execution failed"
	}
}

func privateFailure(err error, kind diagnosticKind) error {
	if errors.Is(err, context.Canceled) {
		return context.Canceled
	}
	if errors.Is(err, context.DeadlineExceeded) {
		return context.DeadlineExceeded
	}
	return diagnosticError{kind: kind}
}
