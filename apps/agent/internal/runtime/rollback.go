package runtime

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"slices"
	"sort"
	"strings"
	"time"

	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/config"
	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/redact"
)

// How long a recreated container gets to run and pass its health check.
const rollbackSettle = 45 * time.Second

// Rollback recreates the service's container from `image` (a local image id)
// with the same configuration: name, environment, labels (Compose keeps
// managing it), ports, restart policy, networks with their aliases, and every
// volume, anonymous ones included, so no data is swapped for an empty volume.
//
// The old container is stopped and renamed, not removed, until the new one
// runs and `healthy` passes; otherwise the new one is removed and the old one
// gets its name back and is started again.
//
// `ref` ("nginx:1.27") names the image when it still points at `image`, so the
// container shows its name rather than a bare id.
func (d *Docker) Rollback(ctx context.Context, s config.Service, image, ref string, healthy func(context.Context) error) error {
	return d.recreate(ctx, s, image, ref, nil, nil, healthy)
}

// recreate is Rollback with `env` (NAME -> value) set on top of the container's own.
func (d *Docker) recreate(ctx context.Context, s config.Service, image, ref string, env map[string]string, unset []string, healthy func(context.Context) error) error {
	c, err := d.container(ctx, s)
	if err != nil {
		return err
	}
	if _, status, err := d.do(ctx, http.MethodGet, "/images/"+url.PathEscape(image)+"/json"); err != nil || status != http.StatusOK {
		return fmt.Errorf("image %s is no longer on this host", short(image))
	}
	var raw map[string]any
	if err := d.get(ctx, "/containers/"+c.ID+"/json", &raw); err != nil {
		return err
	}
	var current struct {
		Config map[string]any `json:"Config"`
	}
	if err := d.get(ctx, "/images/"+url.PathEscape(c.Image)+"/json", &current); err != nil {
		return fmt.Errorf("reading the current image: %w", err)
	}
	use := image
	if ref != "" {
		var named struct {
			ID string `json:"Id"`
		}
		if d.get(ctx, "/images/"+url.PathEscape(ref)+"/json", &named) == nil && named.ID == image {
			use = ref
		}
	}
	body, err := recreateBody(raw, current.Config, use)
	if err != nil {
		return err
	}
	if len(env) > 0 || len(unset) > 0 {
		body["Env"] = withEnv(body["Env"], env, unset)
	}
	name := strings.TrimPrefix(c.Name, "/")
	parked := fmt.Sprintf("%s-gc-previous-%d", name, time.Now().Unix())

	if err := d.call(ctx, http.MethodPost, "/containers/"+c.ID+"/stop?t=10", nil, http.StatusNoContent, http.StatusNotModified); err != nil {
		return fmt.Errorf("stopping the current container: %w", err)
	}
	if err := d.call(ctx, http.MethodPost, "/containers/"+c.ID+"/rename?name="+url.QueryEscape(parked), nil, http.StatusNoContent); err != nil {
		_ = d.call(ctx, http.MethodPost, "/containers/"+c.ID+"/start", nil, http.StatusNoContent, http.StatusNotModified)
		return fmt.Errorf("renaming the current container: %w", err)
	}
	restore := func(newID string, cause error) error {
		if newID != "" {
			_ = d.call(ctx, http.MethodDelete, "/containers/"+newID+"?force=1", nil, http.StatusNoContent)
		}
		_ = d.call(ctx, http.MethodPost, "/containers/"+c.ID+"/rename?name="+url.QueryEscape(name), nil, http.StatusNoContent)
		_ = d.call(ctx, http.MethodPost, "/containers/"+c.ID+"/start", nil, http.StatusNoContent, http.StatusNotModified)
		return fmt.Errorf("%w; the previous container was started again", cause)
	}

	created, err := d.create(ctx, name, body)
	if err != nil {
		return restore(created, fmt.Errorf("creating the container: %w", err))
	}
	if err := d.call(ctx, http.MethodPost, "/containers/"+created+"/start", nil, http.StatusNoContent); err != nil {
		return restore(created, fmt.Errorf("starting the container: %w", err))
	}
	if err := d.settle(ctx, s, healthy); err != nil {
		return restore(created, err)
	}
	// Volumes stay: the new container uses them.
	_ = d.call(ctx, http.MethodDelete, "/containers/"+c.ID, nil, http.StatusNoContent)
	return nil
}

// A new container must run this many checks in a row (2 s apart) without
// restarting: a crash loop under a restart policy looks "running" between crashes.
const stableChecks = 3

// settle waits until the container runs steadily and the health check (if
// any) passes.
func (d *Docker) settle(ctx context.Context, s config.Service, healthy func(context.Context) error) error {
	deadline := time.Now().Add(rollbackSettle)
	var last error
	steady, restarts := 0, -1
	for time.Now().Before(deadline) {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(2 * time.Second):
		}
		st := d.State(ctx, s)
		if !st.Running() || (restarts >= 0 && st.Restarts > restarts) {
			steady, restarts = 0, st.Restarts
			last = fmt.Errorf("it is %s", st.State)
			if st.Restarts > 0 {
				last = fmt.Errorf("it keeps restarting (%d restarts)", st.Restarts)
			}
			if st.State == "exited" || st.State == "dead" || st.Restarts >= 3 {
				break
			}
			continue
		}
		restarts = st.Restarts
		if healthy != nil {
			if last = healthy(ctx); last != nil {
				steady = 0
				continue
			}
		}
		if steady++; steady >= stableChecks {
			return nil
		}
	}
	if last == nil {
		last = errors.New("it did not start in time")
	}
	return fmt.Errorf("the new container is not healthy (%v)%s", last, d.lastLines(ctx, s))
}

// FailedLogLines is how many of a failed container's last log lines go into the error, so
// the reason survives the container's removal.
const FailedLogLines = 15

// lastLines is the container's last log lines (secrets redacted), for an error message.
func (d *Docker) lastLines(ctx context.Context, s config.Service) string {
	lines, err := d.Logs(ctx, s, time.Now().Add(-rollbackSettle-time.Minute))
	if err != nil || len(lines) == 0 {
		return ""
	}
	if len(lines) > FailedLogLines {
		lines = lines[len(lines)-FailedLogLines:]
	}
	var b strings.Builder
	b.WriteString("; its last log lines:")
	for _, l := range lines {
		text := redact.Line(strings.TrimSpace(l.Text))
		if len(text) > 300 {
			text = text[:300] + "..."
		}
		b.WriteString("\n" + text)
	}
	return b.String()
}

// recreateBody is the create request for a copy of the inspected container
// running `image`. Docker reports the image's defaults and the user's settings
// together, so what only came from the current image (`imageCfg`) is left out:
// the earlier image brings its own command, entrypoint, environment, labels.
func recreateBody(raw, imageCfg map[string]any, image string) (map[string]any, error) {
	cfg, ok1 := raw["Config"].(map[string]any)
	host, ok2 := raw["HostConfig"].(map[string]any)
	if !ok1 || !ok2 {
		return nil, errors.New("unexpected container description from Docker")
	}
	body := map[string]any{}
	for k, v := range cfg {
		body[k] = v
	}
	body["Image"] = image
	withoutImageDefaults(body, imageCfg)
	// Hostname defaults to the container id; a fixed one set by the user is kept.
	if id, _ := raw["Id"].(string); id != "" && strings.HasPrefix(id, fmt.Sprint(cfg["Hostname"])) {
		delete(body, "Hostname")
	}
	hostCopy := map[string]any{}
	for k, v := range host {
		hostCopy[k] = v
	}
	hostCopy["Mounts"] = mounts(raw, host)
	body["HostConfig"] = hostCopy
	if nets, ok := raw["NetworkSettings"].(map[string]any)["Networks"].(map[string]any); ok && len(nets) > 0 {
		endpoints := map[string]any{}
		for name, n := range nets {
			ep, _ := n.(map[string]any)
			keep := map[string]any{}
			for _, k := range []string{"Aliases", "IPAMConfig", "Links", "DriverOpts", "MacAddress"} {
				if v, ok := ep[k]; ok && v != nil {
					keep[k] = v
				}
			}
			endpoints[name] = keep
		}
		body["NetworkingConfig"] = map[string]any{"EndpointsConfig": endpoints}
	}
	return body, nil
}

// mounts keeps the container's mounts and adds its anonymous and image
// volumes (in .Mounts but not in HostConfig), so they are reused, not new.
func mounts(raw, host map[string]any) []any {
	out, _ := host["Mounts"].([]any)
	taken := map[string]bool{}
	for _, m := range out {
		if mm, ok := m.(map[string]any); ok {
			taken[fmt.Sprint(mm["Target"])] = true
		}
	}
	if binds, ok := host["Binds"].([]any); ok {
		for _, b := range binds {
			parts := strings.Split(fmt.Sprint(b), ":")
			if len(parts) >= 2 {
				taken[parts[1]] = true
			}
		}
	}
	current, _ := raw["Mounts"].([]any)
	for _, m := range current {
		mm, ok := m.(map[string]any)
		if !ok || mm["Type"] != "volume" || taken[fmt.Sprint(mm["Destination"])] {
			continue
		}
		out = append(out, map[string]any{
			"Type":     "volume",
			"Source":   mm["Name"],
			"Target":   mm["Destination"],
			"ReadOnly": mm["RW"] == false,
		})
	}
	return out
}

// create makes the container and returns its id; an older Engine that takes
// one network at create gets the others connected afterwards.
func (d *Docker) create(ctx context.Context, name string, body map[string]any) (string, error) {
	var out struct {
		ID string `json:"Id"`
	}
	path := "/containers/create?name=" + url.QueryEscape(name)
	err := d.post(ctx, path, body, &out, http.StatusCreated)
	if err == nil {
		return out.ID, nil
	}
	nc, _ := body["NetworkingConfig"].(map[string]any)
	eps, _ := nc["EndpointsConfig"].(map[string]any)
	if len(eps) < 2 {
		return "", err
	}
	first, rest := "", map[string]any{}
	for k, v := range eps {
		if first == "" {
			first = k
			continue
		}
		rest[k] = v
	}
	body["NetworkingConfig"] = map[string]any{"EndpointsConfig": map[string]any{first: eps[first]}}
	if err := d.post(ctx, path, body, &out, http.StatusCreated); err != nil {
		return "", err
	}
	for net, ep := range rest {
		connect := map[string]any{"Container": out.ID, "EndpointConfig": ep}
		if err := d.post(ctx, "/networks/"+url.PathEscape(net)+"/connect", connect, nil, http.StatusOK); err != nil {
			return out.ID, err
		}
	}
	return out.ID, nil
}

func (d *Docker) post(ctx context.Context, path string, in, out any, ok ...int) error {
	b, err := json.Marshal(in)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, "http://docker"+path, bytes.NewReader(b))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := d.http.Do(req)
	if err != nil {
		return fmt.Errorf("cannot reach Docker: %w", err)
	}
	defer resp.Body.Close()
	var buf bytes.Buffer
	_, _ = buf.ReadFrom(resp.Body)
	for _, code := range ok {
		if resp.StatusCode == code {
			if out != nil && buf.Len() > 0 {
				return json.Unmarshal(buf.Bytes(), out)
			}
			return nil
		}
	}
	return fmt.Errorf("Docker answered %d: %s", resp.StatusCode, strings.TrimSpace(buf.String()))
}

func (d *Docker) call(ctx context.Context, method, path string, in any, ok ...int) error {
	if method == http.MethodPost {
		return d.post(ctx, path, orEmpty(in), nil, ok...)
	}
	body, status, err := d.do(ctx, method, path)
	if err != nil {
		return err
	}
	for _, code := range ok {
		if status == code {
			return nil
		}
	}
	return fmt.Errorf("Docker answered %d: %s", status, strings.TrimSpace(string(body)))
}

func orEmpty(in any) any {
	if in == nil {
		return map[string]any{}
	}
	return in
}

func short(image string) string {
	s := strings.TrimPrefix(image, "sha256:")
	if len(s) > 12 {
		s = s[:12]
	}
	return s
}

// withoutImageDefaults removes from a container config what equals the
// current image's own config: whole settings (command, entrypoint, ...), and
// the image's own environment variables, labels, exposed ports, and volumes.
func withoutImageDefaults(body, imageCfg map[string]any) {
	if imageCfg == nil {
		return
	}
	for _, k := range []string{"Cmd", "Entrypoint", "WorkingDir", "User", "StopSignal", "Healthcheck", "Shell", "OnBuild", "ArgsEscaped"} {
		if v, ok := body[k]; ok && fmt.Sprint(v) == fmt.Sprint(imageCfg[k]) {
			delete(body, k)
		}
	}
	if env, ok := body["Env"].([]any); ok {
		fromImage := map[string]bool{}
		if img, ok := imageCfg["Env"].([]any); ok {
			for _, e := range img {
				fromImage[fmt.Sprint(e)] = true
			}
		}
		kept := []any{}
		for _, e := range env {
			if !fromImage[fmt.Sprint(e)] {
				kept = append(kept, e)
			}
		}
		body["Env"] = kept
	}
	for _, k := range []string{"Labels", "ExposedPorts", "Volumes"} {
		have, ok := body[k].(map[string]any)
		img, ok2 := imageCfg[k].(map[string]any)
		if !ok || !ok2 {
			continue
		}
		kept := map[string]any{}
		for name, v := range have {
			if iv, same := img[name]; !same || fmt.Sprint(iv) != fmt.Sprint(v) {
				kept[name] = v
			}
		}
		body[k] = kept
	}
}

// withEnv sets `env` over a container's NAME=value list (same names replaced).
func withEnv(list any, env map[string]string, unset []string) []any {
	out := []any{}
	have, _ := list.([]any)
	for _, e := range have {
		name, _, _ := strings.Cut(fmt.Sprint(e), "=")
		if _, replaced := env[name]; !replaced && !slices.Contains(unset, name) {
			out = append(out, e)
		}
	}
	names := make([]string, 0, len(env))
	for name := range env {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		out = append(out, name+"="+env[name])
	}
	return out
}
