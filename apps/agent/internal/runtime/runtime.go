// Package runtime reads and acts on services where they run: Docker
// containers and Compose services through the Docker Engine API, systemd
// units through systemctl and journalctl.
package runtime

import (
	"context"
	"errors"
	"fmt"
	"net/http"
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
	// Docker and Compose: the image reference and local image id it runs, and the one it ran
	// before (still on this host), which a rollback goes back to.
	Image           string `json:"image,omitempty"`
	ImageID         string `json:"image_id,omitempty"`
	PreviousImage   string `json:"previous_image,omitempty"`
	PreviousImageID string `json:"previous_image_id,omitempty"`
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
	// HasImage reports whether a local image id is still on this host.
	HasImage(ctx context.Context, id string) bool
}

// PullRefresher keeps the image pull credentials a deploy left in a cluster current
// (Kubernetes, ECS): pods and tasks pull again when they start, after a deploy's login expired.
type PullRefresher interface {
	// RefreshPull replaces the service's pull credentials, if a deploy made them; false if not.
	RefreshPull(ctx context.Context, s config.Service, login *RegistryLogin) (bool, error)
}

// Rollbacker recreates a service from an earlier image (Docker and Compose).
type Rollbacker interface {
	Rollback(ctx context.Context, s config.Service, image, ref string, healthy func(context.Context) error) error
}

// Set is every runtime the agent can use; For picks a service's.
type Set struct {
	Docker, Systemd, Kubernetes, ECS Runtime
}

// For picks the runtime of a service. One the agent cannot reach (Kubernetes outside a
// cluster) reports every service as not found, with the reason.
func (r Set) For(s config.Service) Runtime {
	var rt Runtime
	switch s.Runtime {
	case config.Systemd:
		rt = r.Systemd
	case config.Kubernetes:
		rt = r.Kubernetes
	case config.ECS:
		rt = r.ECS
	default:
		rt = r.Docker
	}
	if rt == nil {
		return Unavailable{Why: "this agent cannot reach " + s.Runtime}
	}
	return rt
}

// Inspector is For as an inventory.Inspector.
func (r Set) Inspector(s config.Service) inventory.Inspector { return r.For(s) }

// Unavailable is a runtime the agent cannot reach: every service is missing, every action fails.
type Unavailable struct{ Why string }

func (u Unavailable) Inspect(_ context.Context, s config.Service) inventory.Service {
	return inventory.Service{Problem: u.Why}
}

func (u Unavailable) State(_ context.Context, s config.Service) State {
	return State{Name: s.Name, State: "unknown", Problem: u.Why}
}

func (u Unavailable) Logs(context.Context, config.Service, time.Time) ([]LogLine, error) {
	return nil, errors.New(u.Why)
}

func (u Unavailable) Restart(context.Context, config.Service) error { return errors.New(u.Why) }

func (u Unavailable) HasImage(context.Context, string) bool { return false }

var healthClient = &http.Client{
	// A health check judges the service, not where it redirects.
	CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
}

// HealthCheck is one HTTP health check: any answer below 400 passes.
func HealthCheck(ctx context.Context, url string, timeout time.Duration) error {
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return err
	}
	req.Header.Set("User-Agent", "groundcrew-agent health check")
	resp, err := healthClient.Do(req)
	if err != nil {
		return err
	}
	resp.Body.Close()
	if resp.StatusCode >= 400 {
		return fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	return nil
}
