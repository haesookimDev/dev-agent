package daemon

import (
	"context"
	"fmt"
	"log/slog"
	"time"
)

type Daemon struct {
	config   Config
	logger   *slog.Logger
	client   *Client
	tracker  *Tracker
	executor Executor
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
			available, active := d.tracker.Available()
			if err := d.client.Heartbeat(ctx, worker.ID, available, active); err != nil {
				d.logger.Warn("heartbeat failed", "error", err)
			}
		case <-poll.C:
			if !d.tracker.Reserve(d.config.RunResources) {
				continue
			}
			claim, err := d.client.Claim(ctx, worker.ID, d.config.RunResources)
			if err != nil {
				d.tracker.Release(d.config.RunResources)
				d.logger.Warn("claim failed", "error", err)
				continue
			}
			if claim == nil {
				d.tracker.Release(d.config.RunResources)
				continue
			}
			go d.execute(ctx, *claim)
		}
	}
}

func (d *Daemon) execute(ctx context.Context, claim Claim) {
	defer d.tracker.Release(d.config.RunResources)
	ctx = ContextWithCorrelationID(ctx, claim.WorkItem.CorrelationID)
	d.logger.Info(
		"starting work",
		"work_id", claim.WorkItem.ID,
		"correlation_id", claim.WorkItem.CorrelationID,
		"title", claim.WorkItem.Title,
	)
	if err := d.executor.Execute(ctx, d.client, claim); err != nil {
		d.logger.Error(
			"work execution failed",
			"work_id", claim.WorkItem.ID,
			"correlation_id", claim.WorkItem.CorrelationID,
			"error", err,
		)
		failureContext := ContextWithCorrelationID(context.Background(), claim.WorkItem.CorrelationID)
		_ = d.client.Event(failureContext, claim.WorkItem.ID, claim.LeaseToken, AgentEvent{
			EventType: "worker.failed", Source: "worker", Level: "error", Message: err.Error(), Payload: map[string]any{},
		})
		current, readErr := d.client.ReadRun(failureContext, claim.WorkItem.ID, claim.LeaseToken)
		if readErr == nil && current.Status != "completed" && current.Status != "failed" && current.Status != "cancelled" {
			failed, transitionErr := d.client.Transition(
				failureContext, claim.WorkItem.ID, claim.LeaseToken,
				"failed", current.Version, "Worker executor failed",
			)
			if transitionErr == nil {
				_ = d.client.Release(failureContext, failed.ID, claim.LeaseToken)
			}
		}
	}
}
