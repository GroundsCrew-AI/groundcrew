package inventory

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/config"
)

// Docker reads containers through the Docker Engine API on its Unix socket
// (plain HTTP, no SDK). Compose services are the containers carrying
// Compose's project and service labels.
type Docker struct {
	http *http.Client
}

func NewDocker(socket string) *Docker {
	return &Docker{http: &http.Client{
		Timeout: 10 * time.Second,
		Transport: &http.Transport{
			DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
				return (&net.Dialer{}).DialContext(ctx, "unix", socket)
			},
		},
	}}
}

type container struct {
	ID    string `json:"Id"`
	Name  string `json:"Name"`
	Image string `json:"Image"`
	State struct {
		Status string `json:"Status"`
	} `json:"State"`
	Config struct {
		Image string `json:"Image"`
	} `json:"Config"`
	NetworkSettings struct {
		Ports map[string]any `json:"Ports"`
	} `json:"NetworkSettings"`
}

// errNotFound is Docker's 404: no such container or image.
type errNotFound struct{ what string }

func (e errNotFound) Error() string { return e.what + " not found" }

func (d *Docker) get(ctx context.Context, path string, out any) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, "http://docker"+path, nil)
	if err != nil {
		return err
	}
	resp, err := d.http.Do(req)
	if err != nil {
		return fmt.Errorf("cannot reach Docker: %w", err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if resp.StatusCode == http.StatusNotFound {
		return errNotFound{path}
	}
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("Docker answered %d: %s", resp.StatusCode, strings.TrimSpace(string(body)))
	}
	return json.Unmarshal(body, out)
}

func (d *Docker) Inspect(ctx context.Context, s config.Service) Service {
	id := s.Container
	if s.Runtime == config.Compose {
		found, err := d.composeContainer(ctx, s.Compose.Project, s.Compose.Service)
		if err != nil {
			return Service{Problem: err.Error()}
		}
		id = found
	}
	var c container
	if err := d.get(ctx, "/containers/"+url.PathEscape(id)+"/json", &c); err != nil {
		if _, ok := err.(errNotFound); ok {
			return Service{Problem: fmt.Sprintf("no container named %s", id)}
		}
		return Service{Problem: err.Error()}
	}
	out := Service{Found: true, Image: c.Config.Image, Ports: []string{}}
	for port := range c.NetworkSettings.Ports {
		out.Ports = append(out.Ports, port)
	}
	var img struct {
		RepoDigests []string `json:"RepoDigests"`
	}
	if err := d.get(ctx, "/images/"+url.PathEscape(c.Image)+"/json", &img); err == nil {
		out.ImageDigest = digestFor(c.Config.Image, img.RepoDigests)
	}
	return out
}

// digestFor picks the digest of the image's own repository from RepoDigests
// ("repo@sha256:..."), else the first one.
func digestFor(image string, digests []string) string {
	repo := image
	if i := strings.LastIndex(repo, "@"); i >= 0 {
		repo = repo[:i]
	}
	if i := strings.LastIndex(repo, ":"); i > strings.LastIndex(repo, "/") {
		repo = repo[:i]
	}
	for _, d := range digests {
		if r, sum, ok := strings.Cut(d, "@"); ok && r == repo {
			return sum
		}
	}
	if len(digests) > 0 {
		if _, sum, ok := strings.Cut(digests[0], "@"); ok {
			return sum
		}
	}
	return ""
}

func (d *Docker) composeContainer(ctx context.Context, project, service string) (string, error) {
	filters, _ := json.Marshal(map[string][]string{"label": {
		"com.docker.compose.project=" + project,
		"com.docker.compose.service=" + service,
	}})
	var found []struct {
		ID    string `json:"Id"`
		State string `json:"State"`
	}
	if err := d.get(ctx, "/containers/json?all=1&filters="+url.QueryEscape(string(filters)), &found); err != nil {
		return "", err
	}
	if len(found) == 0 {
		return "", fmt.Errorf("no container for Compose service %s in project %s", service, project)
	}
	// A running replica first.
	for _, c := range found {
		if c.State == "running" {
			return c.ID, nil
		}
	}
	return found[0].ID, nil
}
