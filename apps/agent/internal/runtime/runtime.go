// Package runtime reads and acts on services where they run: Docker
// containers and Compose services through the Docker Engine API, systemd
// units through systemctl and journalctl.
package runtime

import (
	"context"
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

// Rollbacker recreates a service from an earlier image (Docker and Compose).
type Rollbacker interface {
	Rollback(ctx context.Context, s config.Service, image, ref string, healthy func(context.Context) error) error
}

// For picks the runtime of a service.
func For(s config.Service, docker, systemd Runtime) Runtime {
	if s.Runtime == config.Systemd {
		return systemd
	}
	return docker
}

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
