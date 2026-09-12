package daemon

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"math"
	"net/http"
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
	store, err := d.prepareVMRecovery(ctx)
	if err != nil {
		return err
	}
	var worker Worker
	if store != nil {
		defer store.Close()
		worker, err = d.client.RegisterRecovery(ctx, d.config)
	} else {
		worker, err = d.client.Register(ctx, d.config)
	}
	if err != nil {
		return fmt.Errorf("register worker: %w", err)
	}
	if store != nil {
		worker, err = d.reconcileVMLeases(ctx, store, worker)
		if err != nil {
			return err
		}
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
		d.settleExecutionFailure(failureContext, client, claim)
	}
}

func (d *Daemon) settleExecutionFailure(ctx context.Context, client *reservedRunClient, claim Claim) {
	for attempt := 0; attempt < 2; attempt++ {
		current, err := client.ReadRun(ctx, claim.WorkItem.ID, claim.LeaseToken)
		if err != nil || current.ID != claim.WorkItem.ID || current.Version < 1 || current.Version < claim.WorkItem.Version {
			return
		}
		switch current.Status {
		case "completed", "failed", "cancelled":
			d.releaseFailedExecution(ctx, client, claim)
			return
		case "provisioning", "analyzing", "implementing", "verifying":
			if current.Version == math.MaxInt {
				return
			}
			acknowledged, transitionErr := client.Transition(
				ctx, claim.WorkItem.ID, claim.LeaseToken,
				"failed", current.Version, "Worker executor failed",
			)
			if transitionErr == nil {
				if acknowledged.ID != current.ID || acknowledged.Status != "failed" || acknowledged.Version != current.Version+1 {
					return
				}
				d.releaseFailedExecution(ctx, client, claim)
				return
			}
			var diagnostic diagnosticError
			if attempt == 0 && errors.As(transitionErr, &diagnostic) && diagnostic.kind == controlStatus && diagnostic.code == http.StatusConflict {
				continue
			}
			return
		default:
			// Physical cleanup has completed, but approval, input, feedback,
			// budget and delivery states remain API-owned. Retain the lease
			// until active-run recovery can safely settle those states.
			return
		}
	}
}

func (d *Daemon) releaseFailedExecution(ctx context.Context, client *reservedRunClient, claim Claim) {
	if err := client.Release(ctx, claim.WorkItem.ID, claim.LeaseToken); err != nil {
		d.logger.Warn("resource release failed; reservation retained", "work_id", claim.WorkItem.ID, "error", safeDiagnostic(err))
	}
}
