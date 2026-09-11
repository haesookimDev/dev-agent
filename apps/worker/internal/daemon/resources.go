package daemon

import (
	"context"
	"errors"
	"sync"
	"time"
)

func (d *Daemon) heartbeat(ctx context.Context, workerID string) error {
	d.resourcesMu.Lock()
	defer d.resourcesMu.Unlock()
	available, active := d.tracker.Available()
	return d.client.Heartbeat(ctx, workerID, available, active)
}

func (d *Daemon) claim(ctx context.Context, workerID string) (*Claim, error) {
	d.resourcesMu.Lock()
	defer d.resourcesMu.Unlock()
	if !d.tracker.Reserve(d.config.RunResources) {
		return nil, nil
	}
	claim, err := d.client.Claim(ctx, workerID, d.config.RunResources)
	if err != nil || claim == nil {
		d.tracker.Release(d.config.RunResources)
	}
	return claim, err
}

type reservedRunClient struct {
	*Client
	daemon         *Daemon
	claim          Claim
	released       bool       // Protected by daemon.resourcesMu, including repeated releases.
	releaseMu      sync.Mutex // Physical cleanup must not hold the global heartbeat lock.
	lifecycle      ResourceLifecycle
	remoteReleased bool // A lost local journal write must not repeat an acknowledged API call.
}

func (c *reservedRunClient) BindResources(lifecycle ResourceLifecycle) error {
	c.releaseMu.Lock()
	defer c.releaseMu.Unlock()
	if lifecycle == nil || c.lifecycle != nil || c.remoteReleased || c.released {
		return errVMCleanup
	}
	c.lifecycle = lifecycle
	return nil
}

func (c *reservedRunClient) cleanupLocked(ctx context.Context) error {
	if c.lifecycle == nil || c.released {
		return nil
	}
	// Cancellation must stop the VM, not cancel the attempt to stop it. This
	// independent deadline is bounded and does not prolong its execution lease.
	cleanupCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 60*time.Second)
	defer cancel()
	if err := c.lifecycle.Cleanup(cleanupCtx); err != nil {
		return errVMCleanup
	}
	return nil
}

func (c *reservedRunClient) cleanup(ctx context.Context) error {
	c.releaseMu.Lock()
	defer c.releaseMu.Unlock()
	return c.cleanupLocked(ctx)
}

func (c *reservedRunClient) Release(ctx context.Context, workID, lease string) error {
	if workID != c.claim.WorkItem.ID || lease != c.claim.LeaseToken {
		return errors.New("resource release does not match assigned work")
	}
	c.releaseMu.Lock()
	defer c.releaseMu.Unlock()
	if err := c.cleanupLocked(ctx); err != nil {
		return err
	}
	c.daemon.resourcesMu.Lock()
	defer c.daemon.resourcesMu.Unlock()
	if c.released {
		return nil
	}
	if !c.remoteReleased {
		if err := c.Client.Release(ctx, workID, lease); err != nil {
			return err
		}
		c.remoteReleased = true
	}
	if c.lifecycle != nil {
		if err := c.lifecycle.Released(); err != nil {
			return errVMCleanup
		}
	}
	// Do not let a heartbeat publish a pre-release snapshot after the API has
	// freed the lease. Failed/uncertain releases must retain the local reservation.
	c.daemon.tracker.Release(c.daemon.config.RunResources)
	c.released = true
	return nil
}
