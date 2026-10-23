package main

import (
	"context"
	"log/slog"
	"time"

	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/client"
	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/config"
	agentrt "github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/runtime"
)

// PullRefresh is how often the pull credentials deploys left in a cluster are renewed: well
// inside a Groundcrew pull token's 30 minutes.
const PullRefresh = 10 * time.Minute

// refreshPulls keeps the pull credentials of cluster services that allow deploy current, so a
// pod or task that starts later (a new node, a scale-up) can still pull its image. It returns at
// once when no service needs it.
func refreshPulls(ctx context.Context, cfg *config.Config, c *client.Client, rts agentrt.Set, log *slog.Logger) {
	var services []config.Service
	for _, s := range cfg.Services {
		if s.Allows(config.Deploy) && (s.Runtime == config.Kubernetes || s.Runtime == config.ECS) {
			services = append(services, s)
		}
	}
	if len(services) == 0 {
		return
	}
	wait := time.Minute
	for {
		select {
		case <-ctx.Done():
			return
		case <-time.After(wait):
		}
		wait = PullRefresh
		var login *agentrt.RegistryLogin
		if err := c.Post(ctx, "/agent/v1/registry-login", map[string]any{}, &login); err != nil {
			log.Warn("cannot renew image pull credentials", "error", err)
			wait = time.Minute
			continue
		}
		if login == nil {
			continue // a public repository: nothing to renew
		}
		for _, s := range services {
			r, ok := rts.For(s).(agentrt.PullRefresher)
			if !ok {
				continue
			}
			if renewed, err := r.RefreshPull(ctx, s, login); err != nil {
				log.Warn("cannot renew the image pull credentials", "service", s.Name, "error", err)
			} else if renewed {
				log.Info("renewed the image pull credentials", "service", s.Name)
			}
		}
	}
}
