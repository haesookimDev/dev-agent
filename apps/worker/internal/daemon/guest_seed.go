package daemon

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"strings"
)

func guestUserData(control guestControl, claim Claim) ([]byte, error) {
	verified, err := parseGuestControl(control.Origin, control.IPv4)
	if err != nil || verified != control || strings.ContainsAny(claim.LeaseToken+claim.WorkItem.CorrelationID, "\r\n\x00") {
		return nil, privateFailure(errGuestControl, vmAssignment)
	}
	work := claim.WorkItem
	work.Status = "analyzing"
	work.Version++
	assignment, err := json.Marshal(work)
	if err != nil {
		return nil, privateFailure(err, vmAssignment)
	}
	environment := fmt.Sprintf(
		"KELPIE_CONTROL_URL=%s\nKELPIE_LEASE_TOKEN=%s\nKELPIE_CORRELATION_ID=%s\nKELPIE_ASSIGNMENT=%s\nKELPIE_WORK_ROOT=/workspace\n",
		control.Origin, claim.LeaseToken, work.CorrelationID, base64.URLEncoding.EncodeToString(assignment),
	)
	// Only the trusted guest endpoint is pinned. Never forward the Worker's
	// loopback URL, environment, credential file or host resolver configuration.
	return []byte(fmt.Sprintf(`#cloud-config
ssh_pwauth: false
manage_etc_hosts: false
write_files:
  - path: /etc/hosts
    owner: root:root
    permissions: '0644'
    content: |
      127.0.0.1 localhost
      127.0.1.1 kelpie-run
      ::1 localhost ip6-localhost ip6-loopback
      %s %s
  - path: /run/kelpie/assignment.env
    owner: kelpie:kelpie
    permissions: '0600'
    encoding: b64
    content: %s
runcmd:
  - [ systemctl, start, kelpie-runner.service ]
`, control.IPv4, control.Host, base64.StdEncoding.EncodeToString([]byte(environment)))), nil
}
