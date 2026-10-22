package runtime

import (
	"bufio"
	"bytes"
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/config"
	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/inventory"
)

// Docker reads and restarts containers through the Docker Engine API on its
// Unix socket (plain HTTP, no SDK). A Compose service is the container that
// carries Compose's project and service labels (a running one first).
type Docker struct {
	http *http.Client
}

func NewDocker(socket string) *Docker {
	return &Docker{http: &http.Client{
		Timeout: 30 * time.Second,
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
		Status    string `json:"Status"`
		ExitCode  int    `json:"ExitCode"`
		OOMKilled bool   `json:"OOMKilled"`
		StartedAt string `json:"StartedAt"`
	} `json:"State"`
	RestartCount int `json:"RestartCount"`
	Config       struct {
		Image string `json:"Image"`
		Tty   bool   `json:"Tty"`
	} `json:"Config"`
	NetworkSettings struct {
		Ports map[string]any `json:"Ports"`
	} `json:"NetworkSettings"`
}

// errNotFound is Docker's 404: no such container or image.
type errNotFound struct{ what string }

func (e errNotFound) Error() string { return e.what + " not found" }

func (d *Docker) do(ctx context.Context, method, path string) ([]byte, int, error) {
	req, err := http.NewRequestWithContext(ctx, method, "http://docker"+path, nil)
	if err != nil {
		return nil, 0, err
	}
	resp, err := d.http.Do(req)
	if err != nil {
		return nil, 0, fmt.Errorf("cannot reach Docker: %w", err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	return body, resp.StatusCode, err
}

func (d *Docker) get(ctx context.Context, path string, out any) error {
	body, status, err := d.do(ctx, http.MethodGet, path)
	if err != nil {
		return err
	}
	if status == http.StatusNotFound {
		return errNotFound{path}
	}
	if status != http.StatusOK {
		return fmt.Errorf("Docker answered %d: %s", status, strings.TrimSpace(string(body)))
	}
	return json.Unmarshal(body, out)
}

// container finds the service's container; a missing one is a readable problem.
func (d *Docker) container(ctx context.Context, s config.Service) (container, error) {
	id := s.Container
	if s.Runtime == config.Compose {
		found, err := d.composeContainer(ctx, s.Compose.Project, s.Compose.Service)
		if err != nil {
			return container{}, err
		}
		id = found
	}
	var c container
	if err := d.get(ctx, "/containers/"+url.PathEscape(id)+"/json", &c); err != nil {
		if errors.As(err, new(errNotFound)) {
			return c, fmt.Errorf("no container named %s", id)
		}
		return c, err
	}
	return c, nil
}

func (d *Docker) Inspect(ctx context.Context, s config.Service) inventory.Service {
	c, err := d.container(ctx, s)
	if err != nil {
		return inventory.Service{Problem: err.Error()}
	}
	out := inventory.Service{Found: true, Image: c.Config.Image, Ports: []string{}}
	for port := range c.NetworkSettings.Ports {
		out.Ports = append(out.Ports, port)
	}
	out.ImageDigest = d.digest(ctx, c)
	return out
}

func (d *Docker) digest(ctx context.Context, c container) string {
	var img struct {
		RepoDigests []string `json:"RepoDigests"`
	}
	if err := d.get(ctx, "/images/"+url.PathEscape(c.Image)+"/json", &img); err != nil {
		return ""
	}
	return digestFor(c.Config.Image, img.RepoDigests)
}

func (d *Docker) State(ctx context.Context, s config.Service) State {
	c, err := d.container(ctx, s)
	if err != nil {
		return State{Name: s.Name, Problem: err.Error()}
	}
	st := State{
		Name:        s.Name,
		Found:       true,
		State:       c.State.Status,
		Restarts:    c.RestartCount,
		OOMKilled:   c.State.OOMKilled,
		ImageDigest: d.digest(ctx, c),
		Image:       c.Config.Image,
		ImageID:     c.Image,
	}
	if c.State.Status != "running" {
		code := c.State.ExitCode
		st.ExitCode = &code
	}
	if t, err := time.Parse(time.RFC3339Nano, c.State.StartedAt); err == nil && !t.IsZero() && t.Year() > 1 {
		st.StartedAt = &t
	}
	return st
}

// Logs returns the container's log lines since `since` (at most 500).
func (d *Docker) Logs(ctx context.Context, s config.Service, since time.Time) ([]LogLine, error) {
	c, err := d.container(ctx, s)
	if err != nil {
		return nil, err
	}
	q := url.Values{
		"stdout": {"1"}, "stderr": {"1"}, "timestamps": {"1"}, "tail": {"500"},
		"since": {strconv.FormatInt(since.Unix(), 10)},
	}
	body, status, err := d.do(ctx, http.MethodGet, "/containers/"+c.ID+"/logs?"+q.Encode())
	if err != nil {
		return nil, err
	}
	if status != http.StatusOK {
		return nil, fmt.Errorf("Docker answered %d for logs", status)
	}
	if !c.Config.Tty {
		body = demux(body)
	}
	var out []LogLine
	sc := bufio.NewScanner(bytes.NewReader(body))
	sc.Buffer(make([]byte, 64<<10), 1<<20)
	for sc.Scan() {
		stamp, text, ok := strings.Cut(sc.Text(), " ")
		if !ok {
			continue
		}
		at, err := time.Parse(time.RFC3339Nano, stamp)
		if err != nil || !at.After(since) {
			continue
		}
		out = append(out, LogLine{At: at, Text: text})
	}
	return out, nil
}

// demux strips the 8-byte frame headers Docker adds when a container has no TTY.
func demux(raw []byte) []byte {
	var out bytes.Buffer
	for len(raw) >= 8 {
		size := int(binary.BigEndian.Uint32(raw[4:8]))
		raw = raw[8:]
		if size > len(raw) {
			size = len(raw)
		}
		out.Write(raw[:size])
		raw = raw[size:]
	}
	return out.Bytes()
}

// HasImage reports whether the image id is still on this host.
func (d *Docker) HasImage(ctx context.Context, id string) bool {
	_, status, err := d.do(ctx, http.MethodGet, "/images/"+url.PathEscape(id)+"/json")
	return err == nil && status == http.StatusOK
}

// Restart stops the container (10 s grace) and starts it again.
func (d *Docker) Restart(ctx context.Context, s config.Service) error {
	c, err := d.container(ctx, s)
	if err != nil {
		return err
	}
	body, status, err := d.do(ctx, http.MethodPost, "/containers/"+c.ID+"/restart?t=10")
	if err != nil {
		return err
	}
	if status != http.StatusNoContent && status != http.StatusOK {
		return fmt.Errorf("Docker answered %d: %s", status, strings.TrimSpace(string(body)))
	}
	return nil
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
	for _, c := range found {
		if c.State == "running" {
			return c.ID, nil
		}
	}
	return found[0].ID, nil
}
