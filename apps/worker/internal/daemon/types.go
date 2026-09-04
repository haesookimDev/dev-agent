package daemon

import "time"

type WorkItem struct {
	ID               string `json:"id"`
	Title            string `json:"title"`
	Requirement      string `json:"requirement"`
	Repository       string `json:"repository"`
	Status           string `json:"status"`
	Version          int    `json:"version"`
	BudgetMinutes    int    `json:"budget_minutes"`
	ReplanLimit      int    `json:"replan_limit"`
	AssignedWorkerID string `json:"assigned_worker_id"`
}

type Worker struct {
	ID                string            `json:"id"`
	Name              string            `json:"name"`
	CPUTotal          int               `json:"cpu_total"`
	CPUAvailable      int               `json:"cpu_available"`
	MemoryMBTotal     int               `json:"memory_mb_total"`
	MemoryMBAvailable int               `json:"memory_mb_available"`
	DiskGBAvailable   int               `json:"disk_gb_available"`
	ActiveRuns        int               `json:"active_runs"`
	Labels            map[string]string `json:"labels"`
}

type Claim struct {
	WorkItem     WorkItem  `json:"work_item"`
	LeaseToken   string    `json:"lease_token"`
	LeaseExpires time.Time `json:"lease_expires_at"`
}

type Resources struct {
	CPU      int
	MemoryMB int
	DiskGB   int
}

type AgentEvent struct {
	EventType string         `json:"event_type"`
	Source    string         `json:"source"`
	Level     string         `json:"level"`
	Message   string         `json:"message"`
	Payload   map[string]any `json:"payload"`
}
