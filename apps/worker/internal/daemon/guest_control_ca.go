package daemon

import (
	"bytes"
	"crypto/x509"
	"encoding/pem"
	"errors"
	"io"
	"os"
	"path/filepath"
	"syscall"
	"time"
)

var errGuestControlCA = errors.New("guest control CA bundle unavailable or invalid")

// An explicit operator input, never an ambient host trust/credential directory.
// The certificate bundle is scoped to the guest API client, not the VM's system
// roots or the coding agent. Private keys and non-certificate content are refused.
func readGuestControlCA(path string) ([]byte, error) {
	if path == "" {
		return nil, nil
	}
	if !filepath.IsAbs(path) || filepath.Clean(path) != path {
		return nil, errGuestControlCA
	}
	file, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
	if err != nil {
		return nil, errGuestControlCA
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil || !privateOwned(info, false) || info.Size() <= 0 || info.Size() > 64<<10 {
		return nil, errGuestControlCA
	}
	data, err := io.ReadAll(io.LimitReader(file, (64<<10)+1))
	if err != nil {
		return nil, errGuestControlCA
	}
	return canonicalGuestControlCA(data)
}

func canonicalGuestControlCA(data []byte) ([]byte, error) {
	if len(data) == 0 || len(data) > 64<<10 {
		return nil, errGuestControlCA
	}
	var result []byte
	now := time.Now()
	for count := 0; len(bytes.TrimSpace(data)) != 0; count++ {
		data = bytes.TrimSpace(data)
		if count >= 8 || !bytes.HasPrefix(data, []byte("-----BEGIN CERTIFICATE-----")) {
			return nil, errGuestControlCA
		}
		block, rest := pem.Decode(data)
		if block == nil || block.Type != "CERTIFICATE" || len(block.Headers) != 0 {
			return nil, errGuestControlCA
		}
		// pem.Decode can skip malformed blocks while looking for a later one.
		// Do not accept a valid certificate following a discarded input block.
		if bytes.Count(data[:len(data)-len(rest)], []byte("-----BEGIN")) != 1 {
			return nil, errGuestControlCA
		}
		cert, err := x509.ParseCertificate(block.Bytes)
		if err != nil || !cert.BasicConstraintsValid || !cert.IsCA || cert.KeyUsage&x509.KeyUsageCertSign == 0 ||
			now.Before(cert.NotBefore) || !now.Before(cert.NotAfter) {
			return nil, errGuestControlCA
		}
		result = append(result, pem.EncodeToMemory(block)...)
		data = rest
	}
	if len(result) == 0 {
		return nil, errGuestControlCA
	}
	return result, nil
}
