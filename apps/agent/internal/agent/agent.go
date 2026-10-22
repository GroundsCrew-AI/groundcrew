// Package agent runs the reporting loop: a heartbeat every 30 seconds (or what
// Groundcrew asks for), and the inventory when it changes, plus every 15
// minutes so Groundcrew catches up after a restart on its side. The heartbeat
// also brings Groundcrew's command key (pinned on first sight) and how often
// the watcher should report.
package agent

import (
	"context"
	"log/slog"
	"time"

	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/client"
	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/config"
	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/inventory"
	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/state"
	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/watch"
)

const (
	defaultHeartbeat = 30 * time.Second
	inventoryCheck   = 60 * time.Second
	inventoryResend  = 15 * time.Minute
	// After a refusal retrying cannot fix (agent removed), wait longer between tries.
	refusedBackoff = 5 * time.Minute
)

type Agent struct {
	Config  *config.Config
	Client  *client.Client
	Docker  inventory.Inspector
	Systemd inventory.Inspector
	Log     *slog.Logger
	Version string
	// Optional: where the command key is pinned, and the watcher to tune.
	State   *state.Dir
	Watcher *watch.Watcher

	started   time.Time
	keyWarned bool
	sentHash  string
	sentAt    time.Time
}

type heartbeatIn struct {
	Version       string `json:"version"`
	UptimeSeconds int64  `json:"uptime_seconds"`
}

type heartbeatOut struct {
	HeartbeatSeconds int     `json:"heartbeat_seconds"`
	EventsSeconds    int     `json:"events_seconds"`
	InventoryHash    *string `json:"inventory_hash"`
	CommandKey       *string `json:"command_key"`
}

type inventoryOut struct {
	Changed        bool    `json:"changed"`
	DiscoveryRunID *string `json:"discovery_run_id"`
}

// Run reports until ctx ends.
func (a *Agent) Run(ctx context.Context) error {
	a.started = time.Now()
	a.Log.Info("agent started", "version", a.Version, "url", a.Config.Groundcrew.URL, "services", len(a.Config.Services))
	every := defaultHeartbeat
	nextBeat := time.Now()
	nextInventory := time.Now()
	for {
		now := time.Now()
		if !now.Before(nextInventory) {
			wait := inventoryCheck
			if err := a.reportInventory(ctx); err != nil {
				wait = a.backoff(err, inventoryCheck)
			}
			nextInventory = now.Add(wait)
		}
		if !now.Before(nextBeat) {
			wait := every
			out, err := a.heartbeat(ctx)
			if err != nil {
				wait = a.backoff(err, every)
			} else {
				if out.HeartbeatSeconds > 0 {
					every = time.Duration(out.HeartbeatSeconds) * time.Second
					wait = every
				}
				a.apply(out)
				if out.InventoryHash == nil && a.sentHash != "" {
					// Groundcrew holds no inventory (the connection was reset): send it again.
					a.sentHash = ""
					nextInventory = now
				}
			}
			nextBeat = now.Add(wait)
		}
		next := nextBeat
		if nextInventory.Before(next) {
			next = nextInventory
		}
		select {
		case <-ctx.Done():
			a.Log.Info("agent stopping")
			return nil
		case <-time.After(time.Until(next)):
		}
	}
}

// apply takes what the heartbeat answer asks for: the report interval, and
// Groundcrew's command key to pin.
func (a *Agent) apply(out heartbeatOut) {
	if a.Watcher != nil && out.EventsSeconds > 0 {
		a.Watcher.SetEvery(time.Duration(out.EventsSeconds) * time.Second)
	}
	if a.State == nil || out.CommandKey == nil {
		return
	}
	if _, err := a.State.PinCommandKey(*out.CommandKey); err != nil && !a.keyWarned {
		a.keyWarned = true
		a.Log.Error("commands from Groundcrew will be refused", "error", err)
	}
}

func (a *Agent) heartbeat(ctx context.Context) (heartbeatOut, error) {
	var out heartbeatOut
	err := a.Client.Post(ctx, "/agent/v1/heartbeat", heartbeatIn{
		Version:       a.Version,
		UptimeSeconds: int64(time.Since(a.started).Seconds()),
	}, &out)
	return out, err
}

// reportInventory sends the inventory when it changed since the last one sent,
// or when the resend interval is over.
func (a *Agent) reportInventory(ctx context.Context) error {
	inv := inventory.Collect(ctx, a.Config, a.Docker, a.Systemd)
	hash := inv.Hash()
	if hash == a.sentHash && time.Since(a.sentAt) < inventoryResend {
		return nil
	}
	var out inventoryOut
	if err := a.Client.Post(ctx, "/agent/v1/inventory", inv, &out); err != nil {
		return err
	}
	a.sentHash, a.sentAt = hash, time.Now()
	if out.Changed {
		missing := 0
		for _, s := range inv.Services {
			if !s.Found {
				missing++
				a.Log.Warn("service not found", "service", s.Name, "problem", s.Problem)
			}
		}
		a.Log.Info("inventory sent", "services", len(inv.Services), "not_found", missing)
	}
	return nil
}

func (a *Agent) backoff(err error, normal time.Duration) time.Duration {
	if client.IsPermanent(err) {
		a.Log.Error("Groundcrew refused the agent; retrying in 5 minutes", "error", err)
		return refusedBackoff
	}
	a.Log.Warn("cannot reach Groundcrew; retrying", "error", err)
	return normal
}
