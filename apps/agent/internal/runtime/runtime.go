// Package runtime reads and acts on services where they run: Docker
// containers and Compose services through the Docker Engine API, systemd
// units through systemctl and journalctl.
package runtime

import (
	"context"
	"time"

	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/config"
	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/inventory"
)

// State is a service's live state, as Groundcrew's /agent/v1/events takes it
// (apps/api/app/remote_agents/signals.py ServiceState). The watcher fills the
// health fields.
type State struct {
	Name           string     `json:"name"`
	Found          bool       `json:"found"`
	State          string     `json:"state"`
	Restarts       int        `json:"restarts"`
	ExitCode       *int       `json:"exit_code"`
	OOMKilled      bool       `json:"oom_killed"`
	StartedAt      *time.Time `json:"started_at"`
	HealthFailures int        `json:"health_failures"`
	HealthDetail   string     `json:"health_detail,omitempty"`
	ImageDigest    string     `json:"image_digest,omitempty"`
	// Why the service could not be read (not sent).
	Problem string `json:"-"`
}

// Running reports whether the state means the service runs.
func (s State) Running() bool {
	return s.Found && (s.State == "running" || s.State == "active")
}

type LogLine struct {
	At   time.Time
	Text string
}

// Runtime is one way services run.
type Runtime interface {
	inventory.Inspector
	State(ctx context.Context, s config.Service) State
	Logs(ctx context.Context, s config.Service, since time.Time) ([]LogLine, error)
	Restart(ctx context.Context, s config.Service) error
}

// For picks the runtime of a service.
func For(s config.Service, docker, systemd Runtime) Runtime {
	if s.Runtime == config.Systemd {
		return systemd
	}
	return docker
}
