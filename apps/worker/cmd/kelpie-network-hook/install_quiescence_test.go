package main

import (
	"context"
	"fmt"
	"os/exec"
	"strings"
	"testing"
	"time"
)

const maskedInactiveWorker = "LoadState=masked\nActiveState=inactive\nUnitFileState=masked-runtime\nMainPID=0"

func TestInstallerRequiresExactWorkerQuiescence(t *testing.T) {
	if output, err := runInstallerFunction(t, `worker_quiescence_state "$2"`, maskedInactiveWorker); err != nil || output != "" {
		t.Fatal("stopped runtime-masked Worker was rejected")
	}
	for _, test := range []struct {
		name  string
		state string
	}{
		{"missing", "LoadState=not-found\nActiveState=inactive\nUnitFileState="},
		{"active", "LoadState=masked\nActiveState=active\nUnitFileState=masked-runtime"},
		{"unmasked", "LoadState=loaded\nActiveState=inactive\nUnitFileState=enabled"},
		{"persistent-mask", "LoadState=masked\nActiveState=inactive\nUnitFileState=masked"},
		{"missing-property", "LoadState=masked\nActiveState=inactive"},
		{"duplicate-property", maskedInactiveWorker + "\nActiveState=inactive"},
		{"unknown-property", maskedInactiveWorker + "\nSubState=dead"},
		{"missing-pid", strings.ReplaceAll(maskedInactiveWorker, "\nMainPID=0", "")},
		{"nonzero-pid", strings.ReplaceAll(maskedInactiveWorker, "MainPID=0", "MainPID=123")},
		{"duplicate-pid", maskedInactiveWorker + "\nMainPID=0"},
		{"ambiguous-value", "LoadState=masked\nActiveState=inactive\nUnitFileState=masked-runtime generated"},
	} {
		t.Run(test.name, func(t *testing.T) {
			output, err := runInstallerFunction(t, `worker_quiescence_state "$2"`, test.state)
			if err == nil || output != "" {
				t.Fatal("unsafe or ambiguous Worker state was accepted or disclosed")
			}
		})
	}
}

func TestInstallerCommandFailuresRemainFailures(t *testing.T) {
	for _, operation := range []string{"directory", "binary", "restart"} {
		t.Run(operation, func(t *testing.T) {
			// Substitute only the fixed external commands inside these exact
			// functions. No privileged command or installed path is touched.
			body := `
scenario="$2"
effects=""
test_install() {
  effects="${effects:+$effects,}install:$1"
  if [[ "$scenario" == directory && "$1" == -d ]] ||
     [[ "$scenario" == binary && "$1" == -o ]]; then return 23; fi
}
test_systemctl() {
  effects="${effects:+$effects,}systemctl:$1"
  [[ "$1" != restart ]]
}
trusted_hook_source() { effects="${effects:+$effects,}verify"; }
hook_sha256() { printf '%s' approved; }
definition="$(declare -f install_network_hook_files)"
eval "${definition//\/usr\/bin\/install/test_install}"
definition="$(declare -f restart_libvirt_for_hook)"
eval "${definition//\/usr\/bin\/systemctl/test_systemctl}"
if [[ "$scenario" == restart ]]; then
  if restart_libvirt_for_hook; then status=ok; else status=failed; fi
else
  if install_network_hook_files synthetic-stage approved; then status=ok; else status=failed; fi
fi
printf 'status=%s;effects=%s\n' "$status" "$effects"
`
			output, err := runInstallerFunction(t, body, operation)
			want := "status=failed;effects=install:-d"
			if operation == "binary" {
				want += ",install:-o"
			} else if operation == "restart" {
				want = "status=failed;effects=systemctl:restart"
			}
			if err != nil || output != want {
				t.Fatal("failed command continued into later effects or appeared successful")
			}
		})
	}
}

func TestInstallerQuiescenceCoversInventoryAndWorkerRace(t *testing.T) {
	for _, test := range []struct {
		name     string
		before   string
		domains  string
		networks string
		after    string
		want     bool
	}{
		{"safe", maskedInactiveWorker, "", "", maskedInactiveWorker, true},
		{"missing-worker", "LoadState=not-found\nActiveState=inactive\nUnitFileState=", "", "", maskedInactiveWorker, false},
		{"active-worker", "LoadState=masked\nActiveState=active\nUnitFileState=masked-runtime", "", "", maskedInactiveWorker, false},
		{"unmasked-worker", "LoadState=loaded\nActiveState=inactive\nUnitFileState=disabled", "", "", maskedInactiveWorker, false},
		{"domain-appeared", maskedInactiveWorker, "private-domain-uuid", "", maskedInactiveWorker, false},
		{"network-appeared", maskedInactiveWorker, "", "private-network-uuid", maskedInactiveWorker, false},
		{"worker-restarted", maskedInactiveWorker, "", "", "LoadState=loaded\nActiveState=active\nUnitFileState=enabled", false},
		{"worker-unmasked", maskedInactiveWorker, "", "", "LoadState=loaded\nActiveState=inactive\nUnitFileState=disabled", false},
	} {
		t.Run(test.name, func(t *testing.T) {
			body := `install_conditions_safe "$2" "$3" "$4" "$5"`
			output, err := runInstallerFunction(t, body, test.before, test.domains, test.networks, test.after)
			leaked := test.domains != "" && strings.Contains(output, test.domains) ||
				test.networks != "" && strings.Contains(output, test.networks)
			if test.want && (err != nil || output != "") {
				t.Fatal("safe idle snapshot was rejected")
			}
			if !test.want && (err == nil || leaked) {
				t.Fatal("unsafe snapshot was accepted or disclosed inventory")
			}
		})
	}
}

func TestInstallerGuardsFilesAndRestartSeparately(t *testing.T) {
	for _, test := range []struct {
		name        string
		checks      string
		wantEffects string
		wantOK      bool
	}{
		{"unsafe-before-files", "fail,fail", "effects=;checks=1", false},
		{"unsafe-before-restart", "ok,fail", "effects=install;checks=2", false},
		{"safe", "ok,ok", "effects=install,restart;checks=2", true},
	} {
		t.Run(test.name, func(t *testing.T) {
			body := `
scenario="$2"
checks=0
effects=""
install_host_quiescent() {
  checks=$((checks + 1))
  case "$scenario:$checks" in
    ok,ok:1|ok,ok:2|ok,fail:1) return 0 ;;
    *) return 1 ;;
  esac
}
install_network_hook_files() { effects="install"; }
restart_libvirt_for_hook() { effects="${effects:+$effects,}restart"; }
if guarded_network_hook_install synthetic-stage synthetic-digest; then
  status=ok
else
  status=failed
fi
printf 'status=%s;effects=%s;checks=%d\n' "$status" "$effects" "$checks"
`
			output, err := runInstallerFunction(t, body, test.checks)
			if err != nil || !strings.Contains(output, test.wantEffects) || strings.Contains(output, "private") {
				t.Fatal("guarded install sequence did not preserve its safety boundary")
			}
			if test.wantOK != strings.Contains(output, "status=ok") {
				t.Fatal("guarded install returned the wrong status")
			}
		})
	}
}

func runInstallerFunction(t *testing.T, body string, args ...string) (string, error) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	arguments := append([]string{"-c", fmt.Sprintf("source \"$1\"\n%s", body), "quiescence-test", installerPath(t)}, args...)
	command := exec.CommandContext(ctx, "/bin/bash", arguments...)
	command.Env = []string{"PATH=/usr/sbin:/usr/bin:/sbin:/bin", "LANG=C", "LC_ALL=C"}
	output, err := command.CombinedOutput()
	return strings.TrimSpace(string(output)), err
}
