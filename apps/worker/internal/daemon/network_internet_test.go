package daemon

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

func internetNetworkFixture(t *testing.T) runNetwork {
	t.Helper()
	network := controlNetworkFixture(t)
	network.Version, network.DNSIPv4 = 3, "1.1.1.1"
	network.DeniedIPv4 = "127.0.0.1/32,192.0.2.7/32,192.168.5.15/32,203.0.113.0/24"
	if !network.valid(network.UUID) {
		t.Fatal("invalid internet fixture")
	}
	return network
}

func TestInternetPolicyRejectsAmbiguousOrUnsafeConfiguration(t *testing.T) {
	for _, dns := range []string{"", "::1", "::ffff:1.1.1.1", "01.1.1.1", " 1.1.1.1", "192.0.0.9", "169.254.169.254", "10.0.0.1", "192.168.5.15", "100.64.0.1", "224.0.0.1", "240.0.0.1", "203.0.113.1", "127.0.0.1"} {
		if _, err := parseGuestInternet(dns, "203.0.113.0/24"); err == nil {
			t.Fatalf("unsafe DNS accepted: %q", dns)
		}
	}
	for _, denied := range []string{"", "0.0.0.0/0", "203.0.113.1/24", "2001:db8::/32", "::ffff:192.0.2.1/128", "203.0.113.0/24,", "203.0.113.0/24,192.0.2.0/24", "203.0.113.0/24,203.0.113.0/24", "1.1.1.1/32", "203.0.113.0/24\n", strings.Repeat("203.0.113.0/24,", 40)} {
		if _, err := parseGuestInternet("1.1.1.1", denied); err == nil {
			t.Fatalf("ambiguous or unsafe deny policy accepted: %q", denied)
		}
	}
	if _, err := parseGuestInternet("1.1.1.1", "192.0.2.0/24,203.0.113.0/24"); err != nil {
		t.Fatal(err)
	}
	for _, mode := range []string{"true", "public", "logged-public-ipv4 ", "enabled"} {
		if _, err := (Config{GuestInternet: mode}).internetPolicy(); err == nil {
			t.Fatal("unknown mode fell back to a permissive default")
		}
	}
	if policy, err := (Config{}).internetPolicy(); err != nil || policy != nil {
		t.Fatal("existing installations changed policy")
	}
	if _, err := (Config{GuestDNSIPv4: "1.1.1.1"}).internetPolicy(); err == nil {
		t.Fatal("partial internet settings were ignored")
	}
	if _, err := (Config{GuestInternet: "logged-public-ipv4", GuestDNSIPv4: "1.1.1.1", GuestDeniedIPv4: "203.0.113.0/24"}).internetPolicy(); err != nil {
		t.Fatal(err)
	}
}

func TestInternetPolicyRequiresCurrentHostAddressesAndExactControlPin(t *testing.T) {
	network := internetNetworkFixture(t)
	control, _ := parseGuestControl(network.ControlOrigin, network.ControlIPv4)
	config, _ := parseGuestInternet(network.DNSIPv4, "203.0.113.0/24")
	host := networkHostInventory{complete: true, hostIPv4: []netip.Addr{netip.MustParseAddr("127.0.0.1"), netip.MustParseAddr("192.168.5.15")}}
	policy, err := config.withHost(host, control)
	if err != nil || policy.DeniedIPv4 != network.DeniedIPv4 || !host.available(network) {
		t.Fatal("host/control boundary not preserved", err)
	}
	for _, ip := range []string{"8.8.8.8", network.ControlIPv4} {
		changed := host
		changed.hostIPv4 = append(slices.Clone(host.hostIPv4), netip.MustParseAddr(ip))
		if changed.available(network) {
			t.Fatal("new publicly addressed Host or local control pin was admitted")
		}
	}
	for _, invalid := range []networkHostInventory{{}, {complete: true}, {complete: true, hostIPv4: []netip.Addr{netip.MustParseAddr(control.IPv4)}}} {
		if _, err := config.withHost(invalid, control); err == nil {
			t.Fatal("incomplete or unsafe Host inventory admitted")
		}
	}
	for _, version := range []int{1, 2, 4} {
		altered := network
		altered.Version = version
		if altered.valid(altered.UUID) {
			t.Fatal("cross-version internet authority accepted")
		}
	}
	altered := network
	altered.DeniedIPv4 = "203.0.113.0/24"
	if altered.valid(altered.UUID) {
		t.Fatal("control endpoint's other ports escaped denial")
	}
}

func TestInternetPolicyKeepsLayerTwoDeniesBeforeConnectionTracking(t *testing.T) {
	network := internetNetworkFixture(t)
	data, err := network.policyXML()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := parseNetworkXML(data, "filter"); err != nil || len(data) > 16<<10 {
		t.Fatal("invalid or oversized policy")
	}
	text := string(data)
	for _, want := range []string{
		"priority='-1000'><mac match='no' srcmacaddr='" + network.GuestMAC,
		"priority='-850'><ip srcipaddr='" + network.Guest + "' dstipaddr='192.0.2.7' dstmacaddr='" + network.GatewayMAC + "' protocol='tcp' dstportstart='8443' dstportend='8443'",
		"priority='-800'><ip srcipaddr='" + network.Guest + "' dstmacaddr='" + network.GatewayMAC + "' protocol='udp'",
		"priority='-800'><ip dstipaddr='" + network.Guest + "' dstmacaddr='" + network.GuestMAC + "' protocol='udp'",
		"priority='-700'><mac/>", "priority='1000' statematch='false'><all/>",
	} {
		if !strings.Contains(text, want) {
			t.Fatal("required anti-spoofing/directional rule missing", want)
		}
	}
	for _, value := range append(slices.Clone(internetReservedIPv4), strings.Split(network.DeniedIPv4, ",")...) {
		prefix := netip.MustParsePrefix(value)
		for _, direction := range []string{"src", "dst"} {
			if !strings.Contains(text, "priority='-840'><ip "+direction+"ipaddr='"+prefix.Addr().String()+"'") {
				t.Fatal("missing pre-conntrack private or management deny", value)
			}
		}
	}
	if strings.Contains(text, "<ipv6") || strings.Contains(text, "filterref") || strings.Contains(text, "<vlan") {
		t.Fatal("policy introduced unrelated protocols or mutable shared filters")
	}
	seed, err := network.guestNetworkConfig()
	if err != nil || !strings.Contains(string(seed), "nameservers:\n      addresses: ['1.1.1.1']") || !strings.Contains(string(seed), "accept-ra: false") {
		t.Fatal("explicit static DNS missing or IPv6 enabled")
	}
	legacy := controlNetworkFixture(t)
	old, _ := legacy.guestNetworkConfig()
	if strings.Contains(string(old), "nameservers") {
		t.Fatal("version 2 acquired DNS")
	}
	currentXML, _ := network.definitionXML()
	legacyXML, _ := legacy.definitionXML()
	if string(currentXML) != string(legacyXML) {
		t.Fatal("policy unexpectedly changed root-hook network identity")
	}
}

func TestInternetPolicyMaximumBoundaryFitsDurableDefinition(t *testing.T) {
	network := internetNetworkFixture(t)
	values := []string{network.ControlIPv4 + "/32"}
	for index := 0; index < 31; index++ {
		values = append(values, fmt.Sprintf("203.0.113.%d/32", index))
	}
	slices.Sort(values)
	network.DeniedIPv4 = strings.Join(values, ",")
	data, err := network.policyXML()
	if err != nil || len(data) > 16<<10 {
		t.Fatal("maximum accepted policy cannot be durably provisioned", err, len(data))
	}
	data, err = json.Marshal(network)
	if err != nil || len(data) > maxRunRecord-1024 {
		t.Fatal("maximum policy leaves no room for its run record")
	}
}

func TestInternetPolicyStoresVersionedBoundary(t *testing.T) {
	store := newTestRunStore(t)
	network := internetNetworkFixture(t)
	control, _ := parseGuestControl(network.ControlOrigin, network.ControlIPv4)
	internet, _ := parseGuestInternet(network.DNSIPv4, "203.0.113.0/24")
	inventory := networkHostInventory{complete: true, hostIPv4: []netip.Addr{netip.MustParseAddr("192.168.5.15"), netip.MustParseAddr("127.0.0.1")}}
	run, err := store.CreateInternet(storeTestWork, networkTestRun, testResources, "10.240.0.0/30", inventory, control, internet)
	if err != nil || run.Record.Schema != 5 || *run.Record.Network != network {
		t.Fatal("public policy not durably versioned", err)
	}
	if loaded, err := store.Load(networkTestRun); err != nil || *loaded.Record.Network != network {
		t.Fatal("public policy changed on recovery")
	}
	for _, schema := range []int{1, 2, 3, 4, 6} {
		altered := run.Record
		altered.Schema = schema
		writeInternetRecord(t, store, altered)
		if _, err := store.Load(networkTestRun); err == nil {
			t.Fatal("cross-schema internet authority accepted")
		}
	}
}

func TestInternetControlExceptionCannotTargetAnotherTaskSubnet(t *testing.T) {
	for _, address := range []string{"10.240.0.1", "10.240.0.6", "10.240.255.254"} {
		t.Run(address, func(t *testing.T) {
			store := newTestRunStore(t)
			control, err := parseGuestControl("https://control.example.test", address)
			if err != nil {
				t.Fatal(err)
			}
			internet, _ := parseGuestInternet("1.1.1.1", "203.0.113.0/24")
			inventory := networkHostInventory{complete: true, hostIPv4: []netip.Addr{netip.MustParseAddr("127.0.0.1")}}
			if _, err := store.CreateInternet(storeTestWork, networkTestRun, testResources, "10.240.0.0/16", inventory, control, internet); err == nil {
				t.Fatal("control exception admitted another current or future task subnet")
			}
			if _, err := os.Lstat(filepath.Join(store.root.Name(), networkTestRun)); !os.IsNotExist(err) {
				t.Fatal("invalid control pin created artifacts")
			}
		})
	}
}

func TestInternetCleanupRequiresExactPrivateCreationIntent(t *testing.T) {
	for _, scenario := range []string{"missing-filter", "changed-policy"} {
		t.Run(scenario, func(t *testing.T) {
			p, f := internetProvisionFixture(t)
			p.logReceipt = func(runNetwork, string, string) (string, error) { return networkTestRun, nil }
			if err := p.create(context.Background(), networkTestRun); err != nil {
				t.Fatal(err)
			}
			path := filepath.Join(p.store.root.Name(), networkTestRun, "filter.xml")
			if scenario == "missing-filter" {
				if err := os.Remove(path); err != nil {
					t.Fatal(err)
				}
			} else {
				if err := os.WriteFile(path, []byte("changed policy"), 0600); err != nil {
					t.Fatal(err)
				}
			}
			f.cleanup.logReceipt = func(runNetwork, string, string) (string, error) {
				t.Fatal("root logging was substituted for private creation ownership")
				return networkTestRun, nil
			}
			if f.cleanup.Cleanup(context.Background()) == nil || !f.active || !f.defined || !f.filter {
				t.Fatal("missing private ownership evidence deleted network resources")
			}
		})
	}
}

func writeInternetRecord(t *testing.T, store *runStore, record runRecord) {
	t.Helper()
	data, _ := json.Marshal(record)
	if err := os.WriteFile(filepath.Join(store.root.Name(), record.RunID, "run.json"), append(data, '\n'), 0600); err != nil {
		t.Fatal(err)
	}
}

func internetProvisionFixture(t *testing.T) (networkProvisioner, *networkCleanupFixture) {
	t.Helper()
	p, f := provisionFixture(t)
	network := internetNetworkFixture(t)
	f.run.Record.Schema, f.run.Record.Network = 5, &network
	f.filterXML, _ = network.policyXML()
	writeInternetRecord(t, p.store, f.run.Record)
	return p, f
}

func TestInternetProvisionRequiresNewLoggingBeforeNICAdmission(t *testing.T) {
	for _, failure := range []string{"", "absent", "active"} {
		t.Run(failure, func(t *testing.T) {
			p, f := internetProvisionFixture(t)
			var phases []string
			p.logReceipt = func(network runNetwork, phase, bootID string) (string, error) {
				phases = append(phases, phase)
				if phase == "absent" && (f.active || f.defined || f.filter) {
					t.Fatal("root receipt preflight happened after host mutation")
				}
				if phase == "active" {
					intent, exists, err := p.store.networkIntent(network.UUID)
					if err != nil || !exists || intent.BootID != bootID || !f.active {
						t.Fatal("activation omitted durable intent")
					}
				}
				if phase == failure {
					return "", errRunNetwork
				}
				return networkTestRun, nil
			}
			err := p.create(context.Background(), networkTestRun)
			if (failure == "") != (err == nil) {
				t.Fatal("logging admission result mismatch", err)
			}
			if failure == "absent" && (f.active || f.defined || f.filter) {
				t.Fatal("missing or stale logger allowed host mutation")
			}
			if failure != "absent" && !slices.Equal(phases, []string{"absent", "active"}) {
				t.Fatal("missing post-start receipt gate")
			}
		})
	}
}

func TestInternetCleanupRetainsReservationUntilRootStoppedReceipt(t *testing.T) {
	p, f := internetProvisionFixture(t)
	p.logReceipt = func(runNetwork, string, string) (string, error) { return networkTestRun, nil }
	if err := p.create(context.Background(), networkTestRun); err != nil {
		t.Fatal(err)
	}
	f.cleanup.logReceipt = func(_ runNetwork, phase, boot string) (string, error) {
		if phase != "stopped" || boot != networkTestRun || f.defined || f.active || f.filter || f.bridge {
			t.Fatal("logging completion checked before physical cleanup")
		}
		return "", errRunNetwork
	}
	if err := f.cleanup.Cleanup(context.Background()); err == nil {
		t.Fatal("missing root stopped receipt allowed release")
	}
	if run, err := p.store.Load(networkTestRun); err != nil || run.Phase != "cleanup-pending" {
		t.Fatal("reservation not retained")
	}
	f.cleanup.logReceipt = func(_ runNetwork, phase, boot string) (string, error) {
		if phase != "stopped" || boot != networkTestRun {
			t.Fatal("recovery did not preserve logging identity")
		}
		return networkTestRun, nil
	}
	if err := f.cleanup.Cleanup(context.Background()); err != nil || f.cleanup.Released() != nil {
		t.Fatal("verified cleanup was not recoverable", err)
	}
	if _, exists, err := p.store.networkIntent(networkTestRun); err != nil || !exists {
		t.Fatal("cleanup discarded durable start evidence")
	}
	f.cleanup.logReceipt = func(runNetwork, string, string) (string, error) { return "", errRunNetwork }
	if err := f.cleanup.Cleanup(context.Background()); err == nil {
		t.Fatal("released run accepted missing logging history")
	}
}

func TestInternetRootReceiptGuardsRemoteLeaseAcknowledgement(t *testing.T) {
	for _, confirmed := range []bool{false, true} {
		t.Run(fmt.Sprint(confirmed), func(t *testing.T) {
			p, f := internetProvisionFixture(t)
			p.logReceipt = func(runNetwork, string, string) (string, error) { return networkTestRun, nil }
			if err := p.create(context.Background(), networkTestRun); err != nil {
				t.Fatal(err)
			}
			f.cleanup.logReceipt = func(_ runNetwork, phase, boot string) (string, error) {
				if phase != "stopped" || boot != networkTestRun {
					t.Fatal("wrong root lifecycle checked before acknowledgement")
				}
				if !confirmed {
					return "", errRunNetwork
				}
				return networkTestRun, nil
			}
			releases := 0
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				switch {
				case strings.HasSuffix(r.URL.Path, "/release"):
					releases++
					stored, err := p.store.Load(networkTestRun)
					if err != nil || stored.Phase != "cleaned" || !confirmed || f.active || f.defined || f.filter || f.bridge {
						t.Error("remote release preceded physical/logging cleanup")
					}
					w.WriteHeader(http.StatusNoContent)
				case r.Method == http.MethodGet:
					_ = json.NewEncoder(w).Encode(WorkItem{ID: storeTestWork, Status: "failed", Version: 3})
				default:
					w.WriteHeader(http.StatusNoContent)
				}
			}))
			defer server.Close()
			d := resourceDaemon(server)
			d.tracker.Reserve(testResources)
			d.executor = executorFunc(func(ctx context.Context, client RunClient, claim Claim) error {
				if err := client.BindResources(f.cleanup); err != nil {
					return err
				}
				return client.Release(ctx, claim.WorkItem.ID, claim.LeaseToken)
			})
			claim := resourceClaim()
			claim.WorkItem.ID, claim.LeaseID = storeTestWork, networkTestRun
			d.execute(context.Background(), claim)
			if confirmed {
				if releases != 1 {
					t.Fatal("confirmed logging did not precede a single release")
				}
				assertResources(t, d, testResources, 0)
			} else {
				if releases != 0 {
					t.Fatal("missing root stop confirmation reached remote acknowledgement")
				}
				assertResources(t, d, Resources{}, 1)
			}
		})
	}
}
