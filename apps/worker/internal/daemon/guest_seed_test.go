package daemon

import (
	"encoding/base64"
	"encoding/json"
	"strings"
	"testing"
)

func TestGuestSeedUsesSeparatePinnedControlEndpoint(t *testing.T) {
	control, err := parseGuestControl("https://control.example.test:8443", "192.0.2.7")
	if err != nil {
		t.Fatal(err)
	}
	claim := resourceClaim()
	claim.WorkItem.Status, claim.WorkItem.Version = "provisioning", 2
	claim.WorkItem.Requirement = "untrusted requirement\nKELPIE_CONTROL_URL=http://localhost:8000"
	body, err := guestUserData(control, claim, nil)
	if err != nil {
		t.Fatal(err)
	}
	text := string(body)
	if !strings.Contains(text, "192.0.2.7 control.example.test\n") || !strings.Contains(text, "manage_etc_hosts: false") ||
		!strings.Contains(text, "permissions: '0600'") || strings.Contains(text, claim.LeaseToken) || strings.Contains(text, "http://localhost") {
		t.Fatal("seed did not pin the reviewed endpoint or protect assignment encoding")
	}
	parts := strings.Split(text, "    encoding: b64\n    content: ")
	if len(parts) != 2 {
		t.Fatal("missing private assignment environment")
	}
	environment, err := base64.StdEncoding.DecodeString(strings.SplitN(parts[1], "\n", 2)[0])
	if err != nil {
		t.Fatal(err)
	}
	values := map[string]string{}
	for _, line := range strings.Split(strings.TrimSpace(string(environment)), "\n") {
		key, value, ok := strings.Cut(line, "=")
		if !ok || values[key] != "" {
			t.Fatal("invalid or duplicate guest environment entry")
		}
		values[key] = value
	}
	if len(values) != 5 || values["KELPIE_CONTROL_URL"] != control.Origin || values["KELPIE_LEASE_TOKEN"] != claim.LeaseToken ||
		values["KELPIE_WORK_ROOT"] != "/workspace" {
		t.Fatal("guest environment used host control configuration")
	}
	assignment, err := base64.URLEncoding.DecodeString(values["KELPIE_ASSIGNMENT"])
	var work WorkItem
	if err != nil || json.Unmarshal(assignment, &work) != nil || work.Version != 3 || work.Status != "analyzing" || work.Requirement != claim.WorkItem.Requirement {
		t.Fatal("assignment did not preserve data and expected Runner version")
	}
}

func TestGuestSeedRejectsEnvironmentInjectionAndAlteredPin(t *testing.T) {
	control, _ := parseGuestControl("https://control.example.test", "192.0.2.7")
	for _, field := range []string{"lease", "correlation", "pin"} {
		claim, current := resourceClaim(), control
		switch field {
		case "lease":
			claim.LeaseToken += "\nINJECTED=1"
		case "correlation":
			claim.WorkItem.CorrelationID = "correlation\rINJECTED=1"
		case "pin":
			current.Host = "other.example.test"
		}
		if data, err := guestUserData(current, claim, nil); err == nil || data != nil {
			t.Errorf("unsafe seed accepted for %s", field)
		}
	}
}
