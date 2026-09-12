package daemon

import (
	"context"
	"errors"
	"time"
)

var errRunReconciliation = errors.New("worker lease reconciliation required before accepting work")

func (d *Daemon) prepareVMRecovery(ctx context.Context) (*runStore, error) {
	if d.config.Executor != "libvirt" {
		return nil, nil
	}
	store, err := openRunStore(d.config.WorkRoot)
	if err != nil {
		return nil, err
	}
	fail := func(err error) (*runStore, error) { store.Close(); return nil, err }
	runs, err := store.List()
	if err != nil {
		return fail(errRunStore)
	}
	ctx, cancel := context.WithTimeout(ctx, 60*time.Second)
	defer cancel()
	unbound := false
	for _, run := range runs {
		if run.Phase == "released" {
			continue
		}
		unbound = unbound || run.Record.Schema == 1
		if err := newVMCleanup(store, run.Record.RunID).Cleanup(ctx); err != nil {
			return fail(err)
		}
	}
	if unbound {
		// Schema 1 never recorded an API lease binding. Physical cleanup is safe,
		// but matching its random UUID to a remote lease would invent authority.
		return fail(errRunReconciliation)
	}
	d.executor = LibvirtExecutor{config: d.config, logger: d.logger, store: store}
	return store, nil
}

func (d *Daemon) reconcileVMLeases(ctx context.Context, store *runStore, worker Worker) (Worker, error) {
	runs, err := store.List()
	if err != nil {
		return Worker{}, errRunStore
	}
	ctx, cancel := context.WithTimeout(ctx, 60*time.Second)
	defer cancel()
	reconciled := false
	for _, run := range runs {
		if run.Phase == "released" {
			continue
		}
		if run.Record.Schema == 1 || run.Phase != "cleaned" {
			return Worker{}, errRunReconciliation
		}
		snapshot, err := d.client.InspectLease(ctx, worker.ID, run.Record.RunID)
		if err != nil {
			return Worker{}, err
		}
		if snapshot.WorkItemID != run.Record.WorkID ||
			(Resources{CPU: snapshot.CPU, MemoryMB: snapshot.MemoryMB, DiskGB: snapshot.DiskGB}) != run.Record.Resources {
			return Worker{}, errRunReconciliation
		}
		// Recheck physical absence after the network read, even for a previously
		// cleaned journal. Only this owned run may declare its cleanup to the API.
		cleanup := newVMCleanup(store, run.Record.RunID)
		if err := cleanup.Cleanup(ctx); err != nil {
			return Worker{}, err
		}
		if err := d.client.ReconcileLease(ctx, worker.ID, snapshot); err != nil {
			return Worker{}, err
		}
		if err := cleanup.Released(); err != nil {
			return Worker{}, err
		}
		reconciled = true
	}
	if reconciled {
		refreshed, err := d.client.RegisterRecovery(ctx, d.config)
		if err != nil {
			return Worker{}, err
		}
		if refreshed.ID != worker.ID {
			return Worker{}, errRunReconciliation
		}
		worker = refreshed
	}
	// An unrecorded Claim, a lost metadata directory or another unresolved lease
	// must not be overwritten by a zero-run heartbeat from this fresh process.
	if worker.ActiveRuns != 0 || worker.CPUAvailable != d.config.CPUTotal ||
		worker.MemoryMBAvailable != d.config.MemoryMBTotal || worker.DiskGBAvailable != d.config.DiskGBTotal {
		return Worker{}, errRunReconciliation
	}
	return worker, nil
}
