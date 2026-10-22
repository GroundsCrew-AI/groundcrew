package runtime

import (
	"bufio"
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strings"
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
	Port  int
	Env   map[string]string
	Login *RegistryLogin
	// Healthy checks the new container (nil: running is enough).
	Healthy func(context.Context) error
}

// Deployer pulls an image and puts it in place (Docker and Compose services).
type Deployer interface {
	Deploy(ctx context.Context, s config.Service, req DeployRequest) (created bool, err error)
}

// Deploy pulls the image, then recreates the service's container from it with
// the same settings and volumes (env values set on top), or creates the
// container when there is none yet: the app's port published, restarted
// unless stopped. A new container that does not run and pass its health check
// is removed and the previous one, if any, is put back.
func (d *Docker) Deploy(ctx context.Context, s config.Service, req DeployRequest) (bool, error) {
	if err := d.pull(ctx, req.Image, req.Login); err != nil {
		return false, err
	}
	var img struct {
		ID string `json:"Id"`
	}
	if err := d.get(ctx, "/images/"+url.PathEscape(req.Image)+"/json", &img); err != nil {
		return false, fmt.Errorf("the pulled image is not on this host: %w", err)
	}
	_, err := d.container(ctx, s)
	if err == nil {
		return false, d.recreate(ctx, s, img.ID, req.Image, req.Env, req.Healthy)
	}
	if s.Runtime != config.Docker || !strings.HasPrefix(err.Error(), "no container named") {
		return false, err
	}
	return true, d.createNew(ctx, s, req)
}

func (d *Docker) createNew(ctx context.Context, s config.Service, req DeployRequest) error {
	port := fmt.Sprintf("%d/tcp", req.Port)
	env := withEnv(nil, req.Env)
	body := map[string]any{
		"Image":        req.Image,
		"Env":          env,
		"ExposedPorts": map[string]any{port: map[string]any{}},
		"Labels":       map[string]any{"io.groundcrew.deployed": "true"},
		"HostConfig": map[string]any{
			"PortBindings":  map[string]any{port: []any{map[string]any{"HostPort": fmt.Sprint(req.Port)}}},
			"RestartPolicy": map[string]any{"Name": "unless-stopped"},
		},
	}
	id, err := d.create(ctx, s.Container, body)
	if err != nil {
		return fmt.Errorf("creating the container: %w", err)
	}
	if err := d.call(ctx, http.MethodPost, "/containers/"+id+"/start", nil, http.StatusNoContent); err != nil {
		_ = d.call(ctx, http.MethodDelete, "/containers/"+id+"?force=1", nil, http.StatusNoContent)
		return fmt.Errorf("starting the container: %w", err)
	}
	if err := d.settle(ctx, s, req.Healthy); err != nil {
		_ = d.call(ctx, http.MethodDelete, "/containers/"+id+"?force=1", nil, http.StatusNoContent)
		return fmt.Errorf("%w; the new container was removed", err)
	}
	return nil
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
