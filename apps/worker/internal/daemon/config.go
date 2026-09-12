package daemon

import (
	"errors"
	"os"
	"runtime"
	"strconv"
	"time"
)

type Config struct {
	ControlURL         string
	GuestControlURL    string
	GuestControlIPv4   string
	GuestControlCAFile string
	GuestInternet      string
	GuestDNSIPv4       string
	GuestDeniedIPv4    string
	NetworkPool        string
	WorkerToken        string
	WorkerTokenFile    string
	WorkerName         string
	Executor           string
	BaseImage          string
	WorkRoot           string
	CPUTotal           int
	MemoryMBTotal      int
	DiskGBTotal        int
	PollInterval       time.Duration
	RunResources       Resources
}

func ConfigFromEnv() (Config, error) {
	config := Config{
		ControlURL:         env("KELPIE_CONTROL_URL", "http://localhost:8000"),
		GuestControlURL:    os.Getenv("KELPIE_GUEST_CONTROL_URL"),
		GuestControlIPv4:   os.Getenv("KELPIE_GUEST_CONTROL_IPV4"),
		GuestControlCAFile: os.Getenv("KELPIE_GUEST_CONTROL_CA_FILE"),
		GuestInternet:      os.Getenv("KELPIE_GUEST_INTERNET"),
		GuestDNSIPv4:       os.Getenv("KELPIE_GUEST_DNS_IPV4"),
		GuestDeniedIPv4:    os.Getenv("KELPIE_GUEST_DENIED_IPV4"),
		NetworkPool:        env("KELPIE_NETWORK_POOL", "10.240.0.0/16"),
		WorkerToken:        os.Getenv("KELPIE_WORKER_TOKEN"),
		WorkerTokenFile:    os.Getenv("KELPIE_WORKER_TOKEN_FILE"),
		WorkerName:         env("KELPIE_WORKER_NAME", hostname()),
		Executor:           env("KELPIE_EXECUTOR", "mock"),
		BaseImage:          env("KELPIE_BASE_IMAGE", "/var/lib/kelpie/images/ubuntu-desktop.qcow2"),
		WorkRoot:           env("KELPIE_WORK_ROOT", "/var/lib/kelpie/runs"),
		CPUTotal:           envInt("KELPIE_CPU_TOTAL", runtime.NumCPU()),
		MemoryMBTotal:      envInt("KELPIE_MEMORY_MB_TOTAL", 16384),
		DiskGBTotal:        envInt("KELPIE_DISK_GB_TOTAL", 100),
		PollInterval:       time.Duration(envInt("KELPIE_POLL_SECONDS", 3)) * time.Second,
		RunResources: Resources{
			CPU:      envInt("KELPIE_RUN_CPU", 2),
			MemoryMB: envInt("KELPIE_RUN_MEMORY_MB", 4096),
			DiskGB:   envInt("KELPIE_RUN_DISK_GB", 30),
		},
	}
	if config.WorkerTokenFile != "" {
		config.WorkerToken = ""
	}
	if _, err := readCredential(config.WorkerTokenFile, config.WorkerToken); err != nil {
		return Config{}, err
	}
	if config.Executor != "mock" && config.Executor != "libvirt" {
		return Config{}, errors.New("KELPIE_EXECUTOR must be mock or libvirt")
	}
	if config.Executor == "libvirt" {
		if _, err := config.internetPolicy(); err != nil {
			return Config{}, err
		}
		if _, err := parseGuestControl(config.GuestControlURL, config.GuestControlIPv4); err != nil {
			return Config{}, err
		}
		if _, err := privateNetworkPool(config.NetworkPool); err != nil {
			return Config{}, err
		}
		if _, err := readGuestControlCA(config.GuestControlCAFile); err != nil {
			return Config{}, err
		}
	}
	return config, nil
}

func (config Config) internetPolicy() (*guestInternet, error) {
	if config.GuestInternet == "" || config.GuestInternet == "disabled" {
		if config.GuestDNSIPv4 != "" || config.GuestDeniedIPv4 != "" {
			return nil, errRunNetwork
		}
		return nil, nil
	}
	if config.GuestInternet != "logged-public-ipv4" {
		return nil, errRunNetwork
	}
	policy, err := parseGuestInternet(config.GuestDNSIPv4, config.GuestDeniedIPv4)
	if err != nil {
		return nil, err
	}
	return &policy, nil
}

func env(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}

func envInt(name string, fallback int) int {
	value, err := strconv.Atoi(os.Getenv(name))
	if err != nil || value < 1 {
		return fallback
	}
	return value
}

func hostname() string {
	value, err := os.Hostname()
	if err != nil {
		return "kelpie-worker"
	}
	return value
}
