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
	"sync"
	"time"

	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/client"
	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/config"
	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/runtime"
)

// Command is what Groundcrew signed (apps/api/app/remote_agents/commands.py).
type Command struct {
	V         int       `json:"v"`
	ID        string    `json:"id"`
	AgentID   string    `json:"agent_id"`
	Action    string    `json:"action"`
	Service   string    `json:"service"`
	PlanHash  string    `json:"plan_hash"`
	IssuedAt  time.Time `json:"issued_at"`
	ExpiresAt time.Time `json:"expires_at"`
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
	Config  *config.Config
	AgentID string
	Client  *client.Client
	Docker  runtime.Runtime
	Systemd runtime.Runtime
	Key     Keys
	Log     *slog.Logger
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
			return cmd, s, nil
		}
	}
	return cmd, config.Service{}, fmt.Errorf("%s is not in this agent's configuration", cmd.Service)
}

func (r *Runner) run(ctx context.Context, cmd Command, s config.Service) (bool, string) {
	rt := runtime.For(s, r.Docker, r.Systemd)
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
