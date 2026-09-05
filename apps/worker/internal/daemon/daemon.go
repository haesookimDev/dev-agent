package daemon

import (
	"context"
	"fmt"
	"log/slog"
	"sync"
	"time"
)

type Daemon struct {
	config  Config
	logger  *slog.Logger
	client  *Client
	tracker *Tracker
	// Serialize remote resource writes with the matching local accounting.
	resourcesMu sync.Mutex
	executor    Executor
}

func New(config Config, logger *slog.Logger) *Daemon {
	var executor Executor = MockExecutor{}
	if config.Executor == "libvirt" {
		executor = LibvirtExecutor{config: config, logger: logger}
	}
	client := NewClient(config.ControlURL, config.WorkerToken)
	client.tokenFile = config.WorkerTokenFile
	return &Daemon{
		config: config, logger: logger,
		client:   client,
		tracker:  NewTracker(Resources{CPU: config.CPUTotal, MemoryMB: config.MemoryMBTotal, DiskGB: config.DiskGBTotal}),
		executor: executor,
	}
}

func (d *Daemon) Run(ctx context.Context) error {
	worker, err := d.client.Register(ctx, d.config)
	if err != nil {
		return fmt.Errorf("register worker: %w", err)
	}
	d.logger.Info("worker registered", "worker_id", worker.ID, "executor", d.config.Executor)
	heartbeat := time.NewTicker(10 * time.Second)
	poll := time.NewTicker(d.config.PollInterval)
	defer heartbeat.Stop()
	defer poll.Stop()
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-heartbeat.C:
			if err := d.heartbeat(ctx, worker.ID); err != nil {
				d.logger.Warn("heartbeat failed", "error", err)
			}
		case <-poll.C:
			claim, err := d.claim(ctx, worker.ID)
			if err != nil {
				d.logger.Warn("claim failed", "error", err)
				continue
			}
			if claim == nil {
				continue
			}
			go d.execute(ctx, *claim)
		}
	}
}

func (d *Daemon) execute(ctx context.Context, claim Claim) {
	client := &reservedRunClient{Client: d.client, daemon: d, claim: claim}
	ctx = ContextWithCorrelationID(ctx, claim.WorkItem.CorrelationID)
	d.logger.Info(
		"starting work",
		"work_id", claim.WorkItem.ID,
		"correlation_id", claim.WorkItem.CorrelationID,
		"title", claim.WorkItem.Title,
	)
	if err := d.executor.Execute(ctx, client, claim); err != nil {
		d.logger.Error(
			"work execution failed",
			"work_id", claim.WorkItem.ID,
			"correlation_id", claim.WorkItem.CorrelationID,
			"error", err,
		)
		failureContext := ContextWithCorrelationID(context.Background(), claim.WorkItem.CorrelationID)
		_ = client.Event(failureContext, claim.WorkItem.ID, claim.LeaseToken, AgentEvent{
			EventType: "worker.failed", Source: "worker", Level: "error", Message: err.Error(), Payload: map[string]any{},
		})
		current, readErr := client.ReadRun(failureContext, claim.WorkItem.ID, claim.LeaseToken)
		if readErr != nil {
			return
		}
		if current.Status != "completed" && current.Status != "failed" && current.Status != "cancelled" {
			_, transitionErr := client.Transition(
				failureContext, claim.WorkItem.ID, claim.LeaseToken,
				"failed", current.Version, "Worker executor failed",
			)
			if transitionErr != nil {
				return
			}
		}
		if err := client.Release(failureContext, claim.WorkItem.ID, claim.LeaseToken); err != nil {
			d.logger.Warn("resource release failed; reservation retained", "work_id", claim.WorkItem.ID, "error", err)
		}
	}
}
