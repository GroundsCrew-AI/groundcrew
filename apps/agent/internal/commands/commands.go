// Package commands runs the actions Groundcrew sends after a person approved
// them. The agent long-polls /agent/v1/commands/poll and runs a command only
// if all of these hold, else it refuses and says why:
//
//   - it is signed by Groundcrew's command key, pinned on first sight (state dir);
//   - it is for this agent, not expired, and not seen before;
//   - the service is in the configuration and its `allow` lists the action.
//
// The result goes to /agent/v1/commands/{id}/result.
package commands

import (
	"context"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/client"
	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/config"
	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/runtime"
)

// Command is what Groundcrew signed (apps/api/app/remote_agents/commands.py).
type Command struct {
	V        int    `json:"v"`
	ID       string `json:"id"`
	AgentID  string `json:"agent_id"`
	Action   string `json:"action"`
	Service  string `json:"service"`
	PlanHash string `json:"plan_hash"`
	// rollback: {"image": the local image id to go back to}
	Params    map[string]string `json:"params"`
	IssuedAt  time.Time         `json:"issued_at"`
	ExpiresAt time.Time         `json:"expires_at"`
}

type signed struct {
	ID        string `json:"id"`
	Payload   string `json:"payload"`
	Signature string `json:"signature"`
}

type pollOut struct {
	Commands []signed `json:"commands"`
}

type resultIn struct {
	OK      bool   `json:"ok"`
	Message string `json:"message"`
}

// Keys returns the pinned command key, or nil before the first heartbeat.
type Keys func() ed25519.PublicKey

type Runner struct {
	Config   *config.Config
	AgentID  string
	Client   *client.Client
	Runtimes runtime.Set
	Key      Keys
	Log      *slog.Logger
	// After runs after each command (the watcher reports at once).
	After func()

	mu   sync.Mutex
	seen map[string]time.Time
	now  func() time.Time
}

// Run polls until ctx ends.
func (r *Runner) Run(ctx context.Context) {
	for ctx.Err() == nil {
		var out pollOut
		// The server holds the poll up to 25 s; give it room.
		pctx, cancel := context.WithTimeout(ctx, 40*time.Second)
		err := r.Client.Post(pctx, "/agent/v1/commands/poll", map[string]any{}, &out)
		cancel()
		if err != nil {
			wait := 5 * time.Second
			if client.IsPermanent(err) {
				wait = 5 * time.Minute
			}
			if ctx.Err() == nil {
				r.Log.Warn("cannot poll for commands", "error", err)
			}
			sleep(ctx, wait)
			continue
		}
		for _, c := range out.Commands {
			r.handle(ctx, c)
		}
	}
}

func (r *Runner) handle(ctx context.Context, c signed) {
	cmd, svc, err := r.check(c)
	ok, message := false, ""
	if err != nil {
		message = "refused: " + err.Error()
		r.Log.Warn("command refused", "command", c.ID, "reason", err)
	} else {
		r.Log.Info("running command", "command", cmd.ID, "action", cmd.Action, "service", cmd.Service)
		ok, message = r.run(ctx, cmd, svc)
		r.Log.Info("command finished", "command", cmd.ID, "ok", ok, "result", message)
	}
	rctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	if err := r.Client.Post(rctx, "/agent/v1/commands/"+c.ID+"/result", resultIn{OK: ok, Message: message}, nil); err != nil {
		r.Log.Error("cannot report the command result", "command", c.ID, "error", err)
	}
	if r.After != nil {
		r.After()
	}
}

// check verifies a command and returns it with its service.
func (r *Runner) check(c signed) (Command, config.Service, error) {
	var cmd Command
	key := r.Key()
	if key == nil {
		return cmd, config.Service{}, errors.New("no Groundcrew command key pinned yet")
	}
	sig, err := base64.StdEncoding.DecodeString(c.Signature)
	if err != nil || !ed25519.Verify(key, []byte(c.Payload), sig) {
		return cmd, config.Service{}, errors.New("the signature does not match Groundcrew's command key")
	}
	if err := json.Unmarshal([]byte(c.Payload), &cmd); err != nil {
		return cmd, config.Service{}, fmt.Errorf("unreadable command: %v", err)
	}
	if cmd.V != 1 || cmd.ID != c.ID {
		return cmd, config.Service{}, errors.New("unknown command version or mismatched id")
	}
	if cmd.AgentID != r.AgentID {
		return cmd, config.Service{}, errors.New("the command is for another agent")
	}
	now := r.clock()
	if !now.Before(cmd.ExpiresAt) {
		return cmd, config.Service{}, errors.New("the command expired")
	}
	r.mu.Lock()
	if r.seen == nil {
		r.seen = map[string]time.Time{}
	}
	for id, exp := range r.seen {
		if now.After(exp) {
			delete(r.seen, id)
		}
	}
	_, dup := r.seen[cmd.ID]
	r.seen[cmd.ID] = cmd.ExpiresAt
	r.mu.Unlock()
	if dup {
		return cmd, config.Service{}, errors.New("this command already ran")
	}
	for _, s := range r.Config.Services {
		if s.Name == cmd.Service {
			if !s.Allows(cmd.Action) {
				return cmd, s, fmt.Errorf("%s does not allow %s in this agent's configuration", s.Name, cmd.Action)
			}
			if cmd.Action == config.Deploy && s.Deploy != nil && s.Deploy.Image != "" &&
				!strings.HasPrefix(cmd.Params["image"], s.Deploy.Image+":") {
				return cmd, s, fmt.Errorf("%s may be deployed from %s only, not %s", s.Name, s.Deploy.Image, cmd.Params["image"])
			}
			return cmd, s, nil
		}
	}
	return cmd, config.Service{}, fmt.Errorf("%s is not in this agent's configuration", cmd.Service)
}

func (r *Runner) run(ctx context.Context, cmd Command, s config.Service) (bool, string) {
	rt := r.Runtimes.For(s)
	switch cmd.Action {
	case config.Restart:
		if err := rt.Restart(ctx, s); err != nil {
			return false, err.Error()
		}
		// Give it a moment, then say how it looks.
		sleep(ctx, 3*time.Second)
		st := rt.State(ctx, s)
		if !st.Running() {
			return false, fmt.Sprintf("restarted, but it is %s now", st.State)
		}
		return true, "restarted; it is running"
	case config.Deploy:
		return r.deploy(ctx, cmd, s, rt)
	case config.Rollback:
		image := cmd.Params["image"]
		rb, ok := rt.(runtime.Rollbacker)
		if !ok || image == "" {
			return false, "a rollback needs a Docker, Compose, Kubernetes, or ECS service and the image to go back to"
		}
		var healthy func(context.Context) error
		if s.Health != nil {
			healthy = func(ctx context.Context) error {
				return runtime.HealthCheck(ctx, s.Health.URL, s.Health.Timeout)
			}
		}
		if err := rb.Rollback(ctx, s, image, cmd.Params["ref"], healthy); err != nil {
			return false, err.Error()
		}
		if healthy != nil {
			return true, "recreated on the previous image; it is running and healthy"
		}
		return true, "recreated on the previous image; it is running"
	default:
		return false, fmt.Sprintf("this agent does not run %q yet", cmd.Action)
	}
}

func (r *Runner) clock() time.Time {
	if r.now != nil {
		return r.now()
	}
	return time.Now()
}

func sleep(ctx context.Context, d time.Duration) {
	select {
	case <-ctx.Done():
	case <-time.After(d):
	}
}

type secretsOut struct {
	Registry *runtime.RegistryLogin `json:"registry"`
	Env      []struct {
		Name  string `json:"name"`
		Value string `json:"value"`
	} `json:"env"`
}

// deploy fetches the deploy's secrets (registry login, env values; once, from
// Groundcrew), then pulls the image and puts it in place.
func (r *Runner) deploy(ctx context.Context, cmd Command, s config.Service, rt runtime.Runtime) (bool, string) {
	d, ok := rt.(runtime.Deployer)
	if !ok {
		return false, "a deploy needs a Docker, Compose, Kubernetes, or ECS service"
	}
	var secrets secretsOut
	if err := r.Client.Post(ctx, "/agent/v1/commands/"+cmd.ID+"/secrets", map[string]any{}, &secrets); err != nil {
		return false, "cannot fetch the deploy's registry login and env values: " + err.Error()
	}
	env := map[string]string{}
	for _, e := range secrets.Env {
		env[e.Name] = e.Value
	}
	port, _ := strconv.Atoi(cmd.Params["port"])
	var healthy func(context.Context) error
	if s.Health != nil {
		healthy = func(ctx context.Context) error {
			return runtime.HealthCheck(ctx, s.Health.URL, s.Health.Timeout)
		}
	}
	res, err := d.Deploy(ctx, s, runtime.DeployRequest{
		Image: cmd.Params["image"], Port: port, Env: env, Login: secrets.Registry,
		HealthPath: cmd.Params["health_path"], Healthy: healthy,
	})
	if err != nil {
		return false, err.Error()
	}
	if res.Created && res.HostPort == 0 {
		return true, fmt.Sprintf("created %s from %s; it is running", s.Target(), cmd.Params["image"])
	}
	if res.Created {
		// "published on host port N": Groundcrew reads the port back from this sentence.
		taken := ""
		if res.HostPort != port {
			taken = fmt.Sprintf(" (host port %d was in use)", port)
		}
		return true, fmt.Sprintf("created the container from %s, app port %d published on host port %d%s; it is running",
			cmd.Params["image"], port, res.HostPort, taken)
	}
	if s.Runtime == config.Kubernetes || s.Runtime == config.ECS {
		return true, fmt.Sprintf("rolled out %s on %s; it is running", cmd.Params["image"], s.Target())
	}
	return true, "recreated the container from " + cmd.Params["image"] + "; it is running"
}
