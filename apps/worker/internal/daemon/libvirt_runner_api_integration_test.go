//go:build linux && libvirt_integration

package daemon

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"slices"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

// Explicit disposable-host drill: the real Executor boots a credential-free
// Golden Image with its real NIC and cloud-init assignment. A temporary guest
// systemd override runs the current public Runner module, not a mock agent or a
// rebuilt/sealed image. The control-only policy intentionally denies GitHub
// cloning. The real API must observe that Runner failure and release the VM.
// The controller supplies only fresh fixture credentials and a fresh test CA.
func TestDedicatedLibvirtRunnerAPI(t *testing.T) {
	if os.Getenv("KELPIE_LIBVIRT_TEST_ACK") != "disposable-host-only" ||
		os.Getenv("KELPIE_LIBVIRT_API_TEST_ACK") != "isolated-api-only" {
		t.Skip("requires explicit disposable host and isolated API acknowledgements")
	}
	var input struct {
		ControlURL string `json:"control_url"`
		GuestURL   string `json:"guest_url"`
		GuestIPv4  string `json:"guest_ipv4"`
		Credential string `json:"credential"`
		WorkID     string `json:"work_id"`
		BaseImage  string `json:"base_image"`
		CA         []byte `json:"ca"`
		Runner     []byte `json:"runner"`
		Scenario   string `json:"scenario"`
	}
	decoder := json.NewDecoder(io.LimitReader(os.Stdin, 256<<10))
	decoder.DisallowUnknownFields()
	if decoder.Decode(&input) != nil || os.Geteuid() == 0 || runtime.GOARCH != "arm64" ||
		!workUUID.MatchString(input.WorkID) || len(input.Runner) == 0 || len(input.Runner) > 128<<10 ||
		!slices.Contains([]string{"trusted", "untrusted-ca", "wrong-hostname", "cancelled"}, input.Scenario) {
		t.Fatal("invalid isolated Runner fixture")
	}
	endpoint, err := url.Parse(input.ControlURL)
	if err != nil || endpoint.Scheme != "http" || endpoint.Hostname() != "127.0.0.1" ||
		endpoint.Port() == "" || endpoint.User != nil || endpoint.Path != "" || endpoint.RawQuery != "" || endpoint.Fragment != "" {
		t.Fatal("Worker fixture API must be loopback-only")
	}
	if _, err := parseGuestControl(input.GuestURL, input.GuestIPv4); err != nil {
		t.Fatal(err)
	}
	if _, err := canonicalGuestControlCA(input.CA); err != nil {
		t.Fatal(err)
	}
	if !filepath.IsAbs(input.BaseImage) || filepath.Clean(input.BaseImage) != input.BaseImage {
		t.Fatal("fixture image must be an explicit canonical path")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancel()
	domains, err := queryVirsh(ctx, "list", "--all", "--uuid")
	if err != nil || strings.TrimSpace(string(domains)) != "" {
		t.Fatal("requires empty dedicated-host domain inventory")
	}
	before, err := queryVirsh(ctx, "net-list", "--all", "--uuid")
	if err != nil {
		t.Fatal(err)
	}
	beforeFilters, err := queryVirsh(ctx, "nwfilter-list")
	if err != nil {
		t.Fatal(err)
	}
	beforeBindings, err := queryVirsh(ctx, "nwfilter-binding-list")
	if err != nil {
		t.Fatal(err)
	}
	verifyBase := packetBaseReadAccess(t, input.BaseImage)
	auth := t.TempDir()
	tokenPath, caPath := filepath.Join(auth, "worker-token"), filepath.Join(auth, "ca.pem")
	if os.WriteFile(tokenPath, []byte(input.Credential), 0600) != nil || os.WriteFile(caPath, input.CA, 0600) != nil {
		t.Fatal("could not prepare private fixture input")
	}
	input.Credential = ""
	root, err := os.MkdirTemp("/var/tmp", "kelpie-runner-api-")
	if err != nil {
		t.Fatal(err)
	}
	store, err := openRunStore(root)
	if err != nil {
		t.Fatal(err)
	}
	defer store.Close()
	config := Config{ControlURL: input.ControlURL, GuestControlURL: input.GuestURL,
		GuestControlIPv4: input.GuestIPv4, GuestControlCAFile: caPath,
		WorkerName: "worker-one", WorkerTokenFile: tokenPath, Executor: "libvirt",
		BaseImage: input.BaseImage, NetworkPool: "10.240.0.0/24", WorkRoot: root,
		CPUTotal: 4, MemoryMBTotal: 8192, DiskGBTotal: 60,
		RunResources: Resources{CPU: 2, MemoryMB: 2048, DiskGB: 40}}
	logger := slog.New(slog.NewJSONHandler(io.Discard, nil))
	d := New(config, logger)
	d.executor = LibvirtExecutor{config: config, logger: logger, store: store}
	worker, err := d.client.RegisterRecovery(ctx, config)
	if err != nil || worker.ActiveRuns != 0 {
		t.Fatal("could not register isolated Worker")
	}
	claim, err := d.claim(ctx, worker.ID)
	if err != nil || claim == nil || claim.WorkItem.ID != input.WorkID || claim.WorkItem.Version != 2 {
		t.Fatal("could not claim the exact fresh fixture work")
	}
	var releaseChecks atomic.Int32
	// Observe the real HTTP request immediately before it reaches the actual
	// API. No response is mocked and no lifecycle method is replaced.
	d.client.http.Transport = runnerAPITransport(func(request *http.Request) (*http.Response, error) {
		if strings.HasSuffix(request.URL.Path, "/release") {
			current, loadErr := store.Load(claim.LeaseID)
			domains, domainErr := queryVirsh(request.Context(), "list", "--all", "--uuid")
			networks, networkErr := queryVirsh(request.Context(), "net-list", "--all", "--uuid")
			filters, filterErr := queryVirsh(request.Context(), "nwfilter-list")
			bindings, bindingErr := queryVirsh(request.Context(), "nwfilter-binding-list")
			if loadErr != nil || current.Phase != "cleaned" || domainErr != nil || strings.TrimSpace(string(domains)) != "" ||
				networkErr != nil || string(networks) != string(before) || filterErr != nil || string(filters) != string(beforeFilters) ||
				bindingErr != nil || string(bindings) != string(beforeBindings) {
				return nil, errors.New("physical cleanup was not confirmed before API release")
			}
			for _, artifact := range runArtifacts {
				if _, err := os.Lstat(filepath.Join(root, claim.LeaseID, artifact)); !errors.Is(err, os.ErrNotExist) {
					return nil, errors.New("owned VM artifact remains before API release")
				}
			}
			interfaces, interfaceErr := hostNetworkReader().interfaces()
			if interfaceErr != nil {
				return nil, errRunNetwork
			}
			for _, device := range interfaces {
				if device.Name == current.Record.Network.Bridge {
					return nil, errors.New("owned bridge remains before API release")
				}
			}
			releaseChecks.Add(1)
		}
		return http.DefaultTransport.RoundTrip(request)
	})
	// The production run identity is the lease identity, not the work ID.
	t.Logf("preserved Runner/API journal: %s/%s", root, claim.LeaseID)
	done := make(chan struct{})
	go func() { defer close(done); d.execute(ctx, *claim) }()
	defer func() {
		cancel()
		select {
		case <-done:
		case <-time.After(85 * time.Second):
			t.Error("bounded Worker cleanup did not finish; journal retained")
		}
	}()
	deadline := time.Now().Add(110 * time.Second)
	for {
		select {
		case <-done:
			t.Fatal("production Executor exited before guest readiness; journal retained")
		default:
		}
		if _, err := queryVirsh(ctx, "qemu-agent-command", claim.LeaseID, `{"execute":"guest-ping"}`); err == nil {
			break
		}
		if ctx.Err() != nil || !time.Now().Before(deadline) {
			t.Fatal("actual Golden Image guest agent did not become ready")
		}
		time.Sleep(time.Second)
	}
	verifyBase()
	if input.Scenario == "trusted" {
		owned, err := store.Load(claim.LeaseID)
		if err != nil || owned.Record.Network == nil {
			t.Fatal("production network record is unavailable")
		}
		tap := packetFixtureNIC(t, ctx, owned.Record)
		initial := packetReadDropCounter(t, ctx, "libvirt-I-"+tap, false)
		result := packetGuestExec(t, ctx, claim.LeaseID, "/usr/bin/python3", "-c", runnerAPIForbiddenTraffic,
			input.GuestIPv4, owned.Record.Network.Gateway)
		final := packetReadDropCounter(t, ctx, "libvirt-I-"+tap, false)
		if strings.TrimSpace(string(result)) != "forbidden-tcp=8" || final < initial+8 {
			t.Fatal("production NIC did not account for forbidden TCP traffic")
		}
		t.Logf("production NIC: eight host/metadata/other-task/private/public/wrong-port TCP attempts denied; TAP DROP %d -> %d", initial, final)
	}
	// Wait for the original cloud-init unit to complete before applying the
	// explicitly disclosed current-source runtime overlay. No host mount or
	// credential directory is copied; the sealed backing image remains unchanged.
	packetGuestExecWithin(t, ctx, 90*time.Second, claim.LeaseID, "/usr/bin/python3", "-c", runnerAPICurrentSource,
		base64.StdEncoding.EncodeToString(input.Runner), fmt.Sprintf("%x", sha256.Sum256(input.Runner)))
	t.Logf("actual guest systemd Runner started as kelpie; current module SHA256 %x (temporary runtime overlay)", sha256.Sum256(input.Runner))
	if input.Scenario == "untrusted-ca" || input.Scenario == "wrong-hostname" {
		result := packetGuestExec(t, ctx, claim.LeaseID, "/usr/bin/python3", "-c", runnerAPIObserveTLS, input.Scenario)
		if strings.TrimSpace(string(result)) != "current-runner-tls-rejection="+input.Scenario {
			t.Fatal("current Runner TLS verification failure was not confirmed")
		}
		t.Logf("current Runner invocation confirmed TLS verification rejection: %s; raw journal withheld", input.Scenario)
	}
	// The VM can disappear as soon as the Worker observes terminal work.
	// Wait for execution to settle, then use individual Worker authentication
	// and the terminal-only inspection API, never an already released run token.
	select {
	case <-done:
	case <-ctx.Done():
		t.Fatal("terminal Runner failure did not trigger production cleanup")
	}
	state, err := d.client.InspectLease(ctx, worker.ID, claim.LeaseID)
	wantStatus := "failed"
	if input.Scenario == "cancelled" {
		wantStatus = "cancelled"
	}
	if err != nil || state.WorkStatus != wantStatus || state.State != "released" || releaseChecks.Load() != 1 {
		t.Fatal("actual API did not confirm terminal failure and lease release")
	}
	t.Logf("actual API recorded Runner failure and release; work version=%d", state.WorkVersion)
	final, err := store.Load(claim.LeaseID)
	if err != nil || final.Phase != "released" {
		t.Fatal("actual API ACK not recorded after VM cleanup")
	}
	after, err := queryVirsh(ctx, "net-list", "--all", "--uuid")
	if err != nil || string(before) != string(after) {
		t.Fatal("fixture changed pre-existing networks")
	}
	verifyBase()
	t.Logf("scenario=%s; real Executor -> current guest Runner -> TLS API -> physical cleanup before release request -> real API ACK; no coding-agent success claimed", input.Scenario)
}

type runnerAPITransport func(*http.Request) (*http.Response, error)

func (transport runnerAPITransport) RoundTrip(request *http.Request) (*http.Response, error) {
	return transport(request)
}

const runnerAPIForbiddenTraffic = `
import socket, sys
address, gateway = sys.argv[1:]
targets = [(address, 22), (address, 80), (gateway, 443),
           ('192.168.5.15', 443), ('169.254.169.254', 80),
           ('10.240.0.6', 443), ('172.16.0.1', 443), ('1.1.1.1', 443)]
for target, port in targets:
    try:
        connection = socket.create_connection((target, port), timeout=.5)
    except (TimeoutError, OSError):
        pass
    else:
        connection.close()
        raise AssertionError('forbidden TCP connection succeeded')
print('forbidden-tcp=8')
`

const runnerAPIObserveTLS = `
import json, re, subprocess, sys, time
def query(*args):
    return subprocess.run(args, check=True, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, timeout=2).stdout.decode()
try:
    invocation = query('systemctl', 'show', 'kelpie-runner.service',
                       '--property=InvocationID', '--value').strip()
    assert re.fullmatch('[0-9a-f]{32}', invocation)
    reason = {'untrusted-ca': 'self-signed certificate',
              'wrong-hostname': 'Hostname mismatch'}[sys.argv[1]]
    deadline = time.monotonic() + 8
    while True:
        lines = query('journalctl', '--no-pager', '--output=json', '--lines=200',
                      '_SYSTEMD_INVOCATION_ID=' + invocation).splitlines()
        messages = '\n'.join(json.loads(line).get('MESSAGE', '') for line in lines)
        if ('/run/kelpie/current-runner.py' in messages and
                'CERTIFICATE_VERIFY_FAILED' in messages and reason in messages):
            break
        assert time.monotonic() < deadline
        time.sleep(.2)
except Exception:
    raise SystemExit(30)
print('current-runner-tls-rejection=' + sys.argv[1])
`

const runnerAPICurrentSource = `
import base64, hashlib, os, pathlib, subprocess, sys, time
# Fixed phase codes diagnose a failed overlay without exposing guest output,
# environment, assignment, credential, certificate or subprocess arguments.
phase = 20
try:
    deadline = time.monotonic() + 60
    while not pathlib.Path('/var/lib/cloud/instance/boot-finished').exists():
        assert time.monotonic() < deadline
        time.sleep(.25)
    phase = 21
    user = subprocess.run(['systemctl', 'show', 'kelpie-runner.service',
                           '--property=User', '--value'], check=True, timeout=2,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout
    assert user.strip() == b'kelpie'
    subprocess.run(['systemctl', 'stop', 'kelpie-runner.service'], check=True, timeout=5,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    phase = 22
    source = base64.b64decode(sys.argv[1], validate=True)
    assert hashlib.sha256(source).hexdigest() == sys.argv[2]
    path = pathlib.Path('/run/kelpie/current-runner.py')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, 'wb') as output:
        output.write(source)
    phase = 23
    directory = pathlib.Path('/run/systemd/system/kelpie-runner.service.d')
    directory.mkdir(mode=0o755)
    with (directory / 'current-source.conf').open('x') as output:
        output.write('[Service]\nExecStart=\nExecStart=/opt/kelpie/runner/bin/python /run/kelpie/current-runner.py\n')
    phase = 24
    subprocess.run(['systemctl', 'daemon-reload'], check=True, timeout=5,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    phase = 25
    subprocess.run(['systemctl', 'start', 'kelpie-runner.service'], check=True, timeout=5,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
except Exception:
    raise SystemExit(phase)
print('current-source-runner-started')
`
