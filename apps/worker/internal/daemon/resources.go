package daemon

import (
	"context"
	"errors"
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
	daemon   *Daemon
	claim    Claim
	released bool // Protected by daemon.resourcesMu, including repeated releases.
}

func (c *reservedRunClient) Release(ctx context.Context, workID, lease string) error {
	if workID != c.claim.WorkItem.ID || lease != c.claim.LeaseToken {
		return errors.New("resource release does not match assigned work")
	}
	c.daemon.resourcesMu.Lock()
	defer c.daemon.resourcesMu.Unlock()
	if c.released {
		return nil
	}
	if err := c.Client.Release(ctx, workID, lease); err != nil {
		return err
	}
	// Do not let a heartbeat publish a pre-release snapshot after the API has
	// freed the lease. Failed/uncertain releases must retain the local reservation.
	c.daemon.tracker.Release(c.daemon.config.RunResources)
	c.released = true
	return nil
}
