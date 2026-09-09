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
	vmCommandStart
	vmCommandExit
	vmBaseImage
	vmRunDirectory
	vmAssignment
	vmSeedData
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
	case vmCommandStart:
		return "VM command could not start"
	case vmCommandExit:
		return fmt.Sprintf("VM command failed (exit %d)", e.code)
	case vmBaseImage:
		return "VM base image unavailable"
	case vmRunDirectory:
		return "VM run directory unavailable"
	case vmAssignment:
		return "VM assignment encoding failed"
	case vmSeedData:
		return "VM seed data could not be written"
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

// Executors may return arbitrary errors. Never render those in a log or event,
// even if a future executor forgets to classify its own failure at the source.
func safeDiagnostic(err error) string {
	var diagnostic diagnosticError
	if errors.As(err, &diagnostic) {
		return diagnostic.Error()
	}
	if errors.Is(err, errCredentialUnavailable) {
		return errCredentialUnavailable.Error()
	}
	return privateFailure(err, 0).Error()
}
