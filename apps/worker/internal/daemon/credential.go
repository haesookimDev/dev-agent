package daemon

import (
	"errors"
	"io"
	"os"
	"strings"
	"syscall"
)

const maxCredentialBytes = 64 << 10

var errCredentialUnavailable = errors.New("worker credential is unavailable or invalid")

func validCredential(value string) bool {
	if len(value) < 32 || len(value) > maxCredentialBytes {
		return false
	}
	for _, character := range []byte(value) {
		if character < 33 || character > 126 {
			return false
		}
	}
	return true
}

// Read on each authenticated request. Mounted symlinks may be rotated atomically,
// but directories/devices/FIFOs must never be read or block the daemon.
func readCredential(path, fallback string) (string, error) {
	if path == "" {
		if !validCredential(fallback) {
			return "", errCredentialUnavailable
		}
		return fallback, nil
	}
	descriptor, err := syscall.Open(path, syscall.O_RDONLY|syscall.O_NONBLOCK|syscall.O_CLOEXEC, 0)
	if err != nil {
		return "", errCredentialUnavailable
	}
	file := os.NewFile(uintptr(descriptor), "worker-credential")
	defer file.Close()
	info, err := file.Stat()
	if err != nil || !info.Mode().IsRegular() {
		return "", errCredentialUnavailable
	}
	data, err := io.ReadAll(io.LimitReader(file, maxCredentialBytes+1))
	if err != nil || len(data) > maxCredentialBytes {
		return "", errCredentialUnavailable
	}
	value := strings.TrimRight(string(data), "\r\n")
	if !validCredential(value) {
		return "", errCredentialUnavailable
	}
	return value, nil
}
