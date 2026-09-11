package daemon

import (
	"context"
	"errors"
	"time"
)

var errRunReconciliation = errors.New("worker lease reconciliation required before accepting work")

func (d *Daemon) prepareVMRecovery(ctx context.Context) (func(), error) {
	if d.config.Executor != "libvirt" {
		return func() {}, nil
	}
	store, err := openRunStore(d.config.WorkRoot)
	if err != nil {
		return nil, err
	}
	fail := func(err error) (func(), error) { store.Close(); return nil, err }
	runs, err := store.List()
	if err != nil {
		return fail(errRunStore)
	}
	ctx, cancel := context.WithTimeout(ctx, 60*time.Second)
	defer cancel()
	unsettled := false
	for _, run := range runs {
		if run.Phase == "released" {
			continue
		}
		unsettled = true
		if err := newVMCleanup(store, run.Record.RunID).Cleanup(ctx); err != nil {
			return fail(err)
		}
	}
	if unsettled {
		// A local clean disk is not proof that a remote release was acknowledged.
		// Do not publish free capacity after a crash/lost reply. A worker-authenticated
		// lease reconciliation contract is a separate follow-up, not a forged token.
		return fail(errRunReconciliation)
	}
	d.executor = LibvirtExecutor{config: d.config, logger: d.logger, store: store}
	return store.Close, nil
}
