package daemon

import (
	"errors"
	"os"
	"runtime"
	"strconv"
	"time"
)

type Config struct {
	ControlURL    string
	WorkerToken   string
	WorkerName    string
	Executor      string
	BaseImage     string
	WorkRoot      string
	CPUTotal      int
	MemoryMBTotal int
	DiskGBTotal   int
	PollInterval  time.Duration
	RunResources  Resources
}

func ConfigFromEnv() (Config, error) {
	config := Config{
		ControlURL:    env("KELPIE_CONTROL_URL", "http://localhost:8000"),
		WorkerToken:   os.Getenv("KELPIE_WORKER_TOKEN"),
		WorkerName:    env("KELPIE_WORKER_NAME", hostname()),
		Executor:      env("KELPIE_EXECUTOR", "mock"),
		BaseImage:     env("KELPIE_BASE_IMAGE", "/var/lib/kelpie/images/ubuntu-desktop.qcow2"),
		WorkRoot:      env("KELPIE_WORK_ROOT", "/var/lib/kelpie/runs"),
		CPUTotal:      envInt("KELPIE_CPU_TOTAL", runtime.NumCPU()),
		MemoryMBTotal: envInt("KELPIE_MEMORY_MB_TOTAL", 16384),
		DiskGBTotal:   envInt("KELPIE_DISK_GB_TOTAL", 100),
		PollInterval:  time.Duration(envInt("KELPIE_POLL_SECONDS", 3)) * time.Second,
		RunResources: Resources{
			CPU:      envInt("KELPIE_RUN_CPU", 2),
			MemoryMB: envInt("KELPIE_RUN_MEMORY_MB", 4096),
			DiskGB:   envInt("KELPIE_RUN_DISK_GB", 30),
		},
	}
	if len(config.WorkerToken) < 32 {
		return Config{}, errors.New("KELPIE_WORKER_TOKEN must contain at least 32 characters")
	}
	if config.Executor != "mock" && config.Executor != "libvirt" {
		return Config{}, errors.New("KELPIE_EXECUTOR must be mock or libvirt")
	}
	return config, nil
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
