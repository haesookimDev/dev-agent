package daemon

import "sync"

type Tracker struct {
	mu       sync.Mutex
	total    Resources
	reserved Resources
	active   int
}

func NewTracker(total Resources) *Tracker {
	return &Tracker{total: total}
}

func (t *Tracker) Available() (Resources, int) {
	t.mu.Lock()
	defer t.mu.Unlock()
	return Resources{
		CPU:      t.total.CPU - t.reserved.CPU,
		MemoryMB: t.total.MemoryMB - t.reserved.MemoryMB,
		DiskGB:   t.total.DiskGB - t.reserved.DiskGB,
	}, t.active
}

func (t *Tracker) Reserve(resources Resources) bool {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.total.CPU-t.reserved.CPU < resources.CPU ||
		t.total.MemoryMB-t.reserved.MemoryMB < resources.MemoryMB ||
		t.total.DiskGB-t.reserved.DiskGB < resources.DiskGB {
		return false
	}
	t.reserved.CPU += resources.CPU
	t.reserved.MemoryMB += resources.MemoryMB
	t.reserved.DiskGB += resources.DiskGB
	t.active++
	return true
}

func (t *Tracker) Release(resources Resources) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.reserved.CPU = max(0, t.reserved.CPU-resources.CPU)
	t.reserved.MemoryMB = max(0, t.reserved.MemoryMB-resources.MemoryMB)
	t.reserved.DiskGB = max(0, t.reserved.DiskGB-resources.DiskGB)
	t.active = max(0, t.active-1)
}
