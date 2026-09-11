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
	closeStore, err := d.prepareVMRecovery(ctx)
	if err != nil {
		return err
	}
	defer closeStore()
	worker, err := d.client.Register(ctx, d.config)
	if err != nil {
		return fmt.Errorf("register worker: %w", err)
	}
	if d.config.Executor == "libvirt" && worker.ActiveRuns != 0 {
		return errRunReconciliation
	}
	var executions sync.WaitGroup
	defer executions.Wait() // Keep ownership locked until bounded cleanup/reporting finishes.
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
				d.logger.Warn("heartbeat failed", "error", safeDiagnostic(err))
			}
		case <-poll.C:
			claim, err := d.claim(ctx, worker.ID)
			if err != nil {
				d.logger.Warn("claim failed", "error", safeDiagnostic(err))
				continue
			}
			if claim == nil {
				continue
			}
			executions.Add(1)
			go func(claim Claim) {
				defer executions.Done()
				d.execute(ctx, claim)
			}(*claim)
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
	)
	err := d.executor.Execute(ctx, client, claim)
	if cleanupErr := client.cleanup(ctx); cleanupErr != nil {
		d.logger.Error("VM cleanup unconfirmed; reservation retained", "work_id", claim.WorkItem.ID, "error", safeDiagnostic(cleanupErr))
		return
	}
	if err != nil {
		d.logger.Error(
			"work execution failed",
			"work_id", claim.WorkItem.ID,
			"correlation_id", claim.WorkItem.CorrelationID,
			"error", safeDiagnostic(err),
		)
		failureContext, cancel := context.WithTimeout(context.Background(), 20*time.Second)
		defer cancel()
		failureContext = ContextWithCorrelationID(failureContext, claim.WorkItem.CorrelationID)
		_ = client.Event(failureContext, claim.WorkItem.ID, claim.LeaseToken, AgentEvent{
			EventType: "worker.failed", Source: "worker", Level: "error", Message: safeDiagnostic(err), Payload: map[string]any{},
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
			d.logger.Warn("resource release failed; reservation retained", "work_id", claim.WorkItem.ID, "error", safeDiagnostic(err))
		}
	}
}
