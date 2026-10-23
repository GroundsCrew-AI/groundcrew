package runtime

import (
	"bufio"
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/config"
)

// RegistryLogin is what a deploy pulls with (fetched from Groundcrew for that deploy only).
type RegistryLogin struct {
	Server   string `json:"server"`
	Username string `json:"username"`
	Password string `json:"password"`
}

// DeployRequest is one deploy of a built image to a service.
type DeployRequest struct {
	Image string
	// The app's port in the container. A new container publishes it on the same host port
	// when that is free, else on the next free one above it.
	Port  int
	Env   map[string]string
	Login *RegistryLogin
	// HealthPath is checked on the host port the app is published on (empty: none).
	HealthPath string
	// Healthy checks the new container through the service's own health URL; it wins over
	// HealthPath (nil: HealthPath, or running is enough).
	Healthy func(context.Context) error
}

// DeployResult says what a deploy did.
type DeployResult struct {
	// Created: there was no container yet and the deploy made one.
	Created bool
	// HostPort is where a created container's port is published (0 for an update, which
	// keeps the container's own ports).
	HostPort int
}

// Deployer pulls an image and puts it in place (Docker and Compose services).
type Deployer interface {
	Deploy(ctx context.Context, s config.Service, req DeployRequest) (DeployResult, error)
}

// Deploy pulls the image, then recreates the service's container from it with
// the same settings and volumes (env values set on top), or creates the
// container when there is none yet: the app's port published on a free host
// port, restarted unless stopped. A new container that does not run and pass
// its health check is removed and the previous one, if any, is put back.
func (d *Docker) Deploy(ctx context.Context, s config.Service, req DeployRequest) (DeployResult, error) {
	if err := d.pull(ctx, req.Image, req.Login); err != nil {
		return DeployResult{}, err
	}
	var img struct {
		ID string `json:"Id"`
	}
	if err := d.get(ctx, "/images/"+url.PathEscape(req.Image)+"/json", &img); err != nil {
		return DeployResult{}, fmt.Errorf("the pulled image is not on this host: %w", err)
	}
	c, err := d.container(ctx, s)
	if err == nil {
		// The container keeps its ports; its health is checked where they are published.
		healthy := healthCheck(req, hostPortOf(c, req.Port))
		return DeployResult{}, d.recreate(ctx, s, img.ID, req.Image, req.Env, healthy)
	}
	if s.Runtime != config.Docker || !strings.HasPrefix(err.Error(), "no container named") {
		return DeployResult{}, err
	}
	port, err := d.createNew(ctx, s, req)
	return DeployResult{Created: true, HostPort: port}, err
}

// healthCheck is the service's own health URL, else HealthPath on the host port.
func healthCheck(req DeployRequest, hostPort int) func(context.Context) error {
	if req.Healthy != nil || req.HealthPath == "" || hostPort <= 0 {
		return req.Healthy
	}
	url := fmt.Sprintf("http://127.0.0.1:%d%s", hostPort, req.HealthPath)
	return func(ctx context.Context) error { return HealthCheck(ctx, url, 5*time.Second) }
}

// hostPortOf is the host port a container publishes the app's port on (a deploy that found
// it taken chose another), or 0 when it is not published.
func hostPortOf(c container, appPort int) int {
	bindings, _ := c.NetworkSettings.Ports[fmt.Sprintf("%d/tcp", appPort)].([]any)
	for _, b := range bindings {
		m, _ := b.(map[string]any)
		if p, err := strconv.Atoi(fmt.Sprint(m["HostPort"])); err == nil && p > 0 {
			return p
		}
	}
	return 0
}

// PortSearch is how many host ports from the app's own port a new container may take.
const PortSearch = 100

// createNew creates and starts the container on a free host port. Another process can take
// that port between the check and the start: then the next free one is tried, up to 3 times.
func (d *Docker) createNew(ctx context.Context, s config.Service, req DeployRequest) (int, error) {
	port := fmt.Sprintf("%d/tcp", req.Port)
	taken := map[int]bool{}
	for range 3 {
		hostPort, err := d.freePort(ctx, req.Port, taken)
		if err != nil {
			return 0, err
		}
		body := map[string]any{
			"Image":        req.Image,
			"Env":          withEnv(nil, req.Env),
			"ExposedPorts": map[string]any{port: map[string]any{}},
			"Labels":       map[string]any{"io.groundcrew.deployed": "true"},
			"HostConfig": map[string]any{
				"PortBindings":  map[string]any{port: []any{map[string]any{"HostPort": fmt.Sprint(hostPort)}}},
				"RestartPolicy": map[string]any{"Name": "unless-stopped"},
			},
		}
		id, err := d.create(ctx, s.Container, body)
		if err != nil {
			return 0, fmt.Errorf("creating the container: %w", err)
		}
		if err := d.call(ctx, http.MethodPost, "/containers/"+id+"/start", nil, http.StatusNoContent); err != nil {
			_ = d.call(ctx, http.MethodDelete, "/containers/"+id+"?force=1", nil, http.StatusNoContent)
			if portInUse(err) {
				taken[hostPort] = true
				continue
			}
			return 0, fmt.Errorf("starting the container: %w", err)
		}
		if err := d.settle(ctx, s, healthCheck(req, hostPort)); err != nil {
			_ = d.call(ctx, http.MethodDelete, "/containers/"+id+"?force=1", nil, http.StatusNoContent)
			return 0, fmt.Errorf("%w; the new container was removed", err)
		}
		return hostPort, nil
	}
	return 0, fmt.Errorf("starting the container: host ports were taken by others while it started (%s)", portList(taken))
}

// freePort is the app's own port when nothing on the host holds it, else the next free one
// above it. Held: a running container publishes it, or listening on it fails.
func (d *Docker) freePort(ctx context.Context, want int, taken map[int]bool) (int, error) {
	var containers []struct {
		Ports []struct {
			PublicPort int `json:"PublicPort"`
		} `json:"Ports"`
	}
	if err := d.get(ctx, "/containers/json", &containers); err != nil {
		return 0, fmt.Errorf("listing the ports containers publish: %w", err)
	}
	published := map[int]bool{}
	for _, c := range containers {
		for _, p := range c.Ports {
			published[p.PublicPort] = true
		}
	}
	last := min(want+PortSearch-1, 65535)
	for p := want; p <= last; p++ {
		if !taken[p] && !published[p] && listenable(p) {
			return p, nil
		}
	}
	return 0, fmt.Errorf("no free host port for the app: %d to %d are all in use", want, last)
}

// listenable: nothing listens on the port. A port the agent may not bind (below 1024 for a
// user without the right) is left to Docker, which binds it as root.
func listenable(port int) bool {
	l, err := net.Listen("tcp", fmt.Sprintf(":%d", port))
	if err != nil {
		return errors.Is(err, syscall.EACCES)
	}
	_ = l.Close()
	return true
}

func portInUse(err error) bool {
	msg := err.Error()
	return strings.Contains(msg, "port is already allocated") || strings.Contains(msg, "address already in use")
}

func portList(ports map[int]bool) string {
	out := make([]string, 0, len(ports))
	for p := range ports {
		out = append(out, fmt.Sprint(p))
	}
	sort.Strings(out)
	return strings.Join(out, ", ")
}

// pull fetches the image through the Engine API, with the login if one is given.
func (d *Docker) pull(ctx context.Context, image string, login *RegistryLogin) error {
	from, tag := image, "latest"
	if i := strings.LastIndex(image, ":"); i > strings.LastIndex(image, "/") {
		from, tag = image[:i], image[i+1:]
	}
	q := url.Values{"fromImage": {from}, "tag": {tag}}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, "http://docker/images/create?"+q.Encode(), nil)
	if err != nil {
		return err
	}
	if login != nil {
		auth, _ := json.Marshal(map[string]string{
			"username": login.Username, "password": login.Password, "serveraddress": login.Server,
		})
		req.Header.Set("X-Registry-Auth", base64.URLEncoding.EncodeToString(auth))
	}
	client := *d.http
	client.Timeout = 15 * time.Minute
	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("cannot reach Docker: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		var buf bytes.Buffer
		_, _ = buf.ReadFrom(resp.Body)
		return fmt.Errorf("pulling %s failed: %s", image, strings.TrimSpace(buf.String()))
	}
	// The answer is a stream of progress messages; an error ends it.
	sc := bufio.NewScanner(resp.Body)
	sc.Buffer(make([]byte, 64<<10), 1<<20)
	for sc.Scan() {
		var msg struct {
			Error string `json:"error"`
		}
		if json.Unmarshal(sc.Bytes(), &msg) == nil && msg.Error != "" {
			return fmt.Errorf("pulling %s failed: %s", image, msg.Error)
		}
	}
	if err := sc.Err(); err != nil && !errors.Is(err, context.Canceled) {
		return fmt.Errorf("pulling %s: %w", image, err)
	}
	return nil
}
