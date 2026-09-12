package daemon

import (
	"errors"
	"net/netip"
	"net/url"
	"strconv"
	"strings"
)

var errGuestControl = errors.New("libvirt requires an explicit HTTPS guest control origin and pinned IPv4 address")

// The Worker's control URL may be loopback or use a host-only transport. Never
// copy it into a guest. This separate trusted origin is pinned in cloud-init
// and the NIC policy; repository/assignment data cannot supply either value.
type guestControl struct {
	Origin string
	Host   string
	IPv4   string
	Port   uint16
}

func parseGuestControl(origin, address string) (guestControl, error) {
	if len(origin) > 300 || strings.ContainsAny(origin, "\r\n\t %\\") {
		return guestControl{}, errGuestControl
	}
	u, err := url.Parse(origin)
	ip, ipErr := netip.ParseAddr(address)
	if err != nil || ipErr != nil || !ip.Is4() || ip.String() != address ||
		!ip.IsGlobalUnicast() || ip.IsLoopback() || ip.As4()[0] == 0 ||
		u.Scheme != "https" || u.User != nil || u.Host == "" || u.Path != "" ||
		u.RawQuery != "" || u.ForceQuery || u.Fragment != "" || u.Opaque != "" || u.String() != origin {
		return guestControl{}, errGuestControl
	}
	host := u.Hostname()
	if !guestControlHostname(host) {
		return guestControl{}, errGuestControl
	}
	if literal, err := netip.ParseAddr(host); err == nil && literal != ip {
		return guestControl{}, errGuestControl
	}
	port := uint64(443)
	if u.Port() != "" {
		port, err = strconv.ParseUint(u.Port(), 10, 16)
		if err != nil || port == 0 || strconv.FormatUint(port, 10) != u.Port() {
			return guestControl{}, errGuestControl
		}
	}
	canonicalHost := host
	if u.Port() != "" {
		canonicalHost += ":" + u.Port()
	}
	if u.Host != canonicalHost {
		return guestControl{}, errGuestControl
	}
	return guestControl{Origin: origin, Host: host, IPv4: address, Port: uint16(port)}, nil
}

func guestControlHostname(host string) bool {
	if ip, err := netip.ParseAddr(host); err == nil {
		return ip.Is4() && ip.IsGlobalUnicast() && !ip.IsLoopback() && ip.As4()[0] != 0 && ip.String() == host
	}
	if len(host) > 253 || host != strings.ToLower(host) || !strings.Contains(host, ".") ||
		host == "localhost" || strings.HasSuffix(host, ".localhost") || strings.HasSuffix(host, ".local") {
		return false
	}
	for _, label := range strings.Split(host, ".") {
		if len(label) == 0 || len(label) > 63 || label[0] == '-' || label[len(label)-1] == '-' {
			return false
		}
		for _, char := range label {
			if !(char >= 'a' && char <= 'z' || char >= '0' && char <= '9' || char == '-') {
				return false
			}
		}
	}
	// libc also accepts legacy octal/hex/short numeric IPv4 spellings. Those
	// must not bypass the literal-address equality check above as DNS names.
	tld := host[strings.LastIndexByte(host, '.')+1:]
	if tld[0] < 'a' || tld[0] > 'z' {
		return false
	}
	return true
}
