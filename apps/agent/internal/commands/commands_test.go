package commands

import (
	"context"
	"crypto/ed25519"
	"encoding/base64"
	"io"
	"log/slog"
	"strings"
	"testing"
	"time"

	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/config"
	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/inventory"
	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/runtime"
)

// A command signed by the API (apps/api/tests/test_remote_agents.py checks the
// same vector), so the Go and Python sides cannot drift apart.
const (
	vectorKey       = "HdkFbZVKrBHI6ZrCwo0gvC3OW/ZX29fv4XvxlD2xBtk="
	vectorID        = "b2da42cc-f3ca-46e4-b3d5-c3fe5d13393d"
	vectorAgent     = "11111111-1111-1111-1111-111111111111"
	vectorPayload   = `{"v":1,"id":"b2da42cc-f3ca-46e4-b3d5-c3fe5d13393d","agent_id":"11111111-1111-1111-1111-111111111111","action":"restart","service":"web","plan_hash":"abababababababababababababababababababababababababababababababab","issued_at":"2026-10-22T10:00:00+00:00","expires_at":"2026-10-22T10:05:00+00:00"}`
	vectorSignature = "AC7PoHkUzDG56JNALmPtpm71WYFMEC2iEmf6x611sxSK/uMF7cL9r/Vmoz0z3EyEi4xdvHMLRaf30KDmVaMFAg=="
)

type fakeRuntime struct{ restarts int }

func (f *fakeRuntime) Inspect(context.Context, config.Service) inventory.Service {
	return inventory.Service{Found: true}
}
func (f *fakeRuntime) State(_ context.Context, s config.Service) runtime.State {
	return runtime.State{Name: s.Name, Found: true, State: "running"}
}
func (f *fakeRuntime) Logs(context.Context, config.Service, time.Time) ([]runtime.LogLine, error) {
	return nil, nil
}
func (f *fakeRuntime) HasImage(context.Context, string) bool         { return true }
func (f *fakeRuntime) Restart(context.Context, config.Service) error { f.restarts++; return nil }

func runner(t *testing.T, allow string) (*Runner, *fakeRuntime) {
	t.Helper()
	cfg, err := config.Parse([]byte("groundcrew: {url: https://api.groundkrew.com}\nservices:\n  - {name: web, runtime: docker, container: web, allow: [" + allow + "]}\n"))
	if err != nil {
		t.Fatal(err)
	}
	key, _ := base64.StdEncoding.DecodeString(vectorKey)
	rt := &fakeRuntime{}
	return &Runner{
		Config:  cfg,
		AgentID: vectorAgent,
		Docker:  rt,
		Systemd: rt,
		Key:     func() ed25519.PublicKey { return key },
		Log:     slog.New(slog.NewTextHandler(io.Discard, nil)),
		now:     func() time.Time { return time.Date(2026, 10, 22, 10, 1, 0, 0, time.UTC) },
	}, rt
}

func vector() signed {
	return signed{ID: vectorID, Payload: vectorPayload, Signature: vectorSignature}
}

func TestASignedAllowedCommandRunsOnce(t *testing.T) {
	r, rt := runner(t, "restart")
	cmd, svc, err := r.check(vector())
	if err != nil {
		t.Fatal(err)
	}
	if cmd.Action != "restart" || svc.Name != "web" {
		t.Fatalf("%+v %+v", cmd, svc)
	}
	ok, msg := r.run(context.Background(), cmd, svc)
	if !ok || rt.restarts != 1 || msg != "restarted; it is running" {
		t.Fatalf("run %v %q", ok, msg)
	}
	if _, _, err := r.check(vector()); err == nil || !strings.Contains(err.Error(), "already ran") {
		t.Fatalf("replay: %v", err)
	}
}

func TestCommandsAreRefusedUnlessEverythingChecksOut(t *testing.T) {
	cases := map[string]func(r *Runner, c *signed){
		"does not allow restart": func(r *Runner, c *signed) { r.Config.Services[0].Allow = nil },
		"signature does not match": func(r *Runner, c *signed) {
			c.Payload = strings.Replace(c.Payload, `"service":"web"`, `"service":"db"`, 1)
		},
		"another agent": func(r *Runner, c *signed) { r.AgentID = "someone-else" },
		"expired": func(r *Runner, c *signed) {
			r.now = func() time.Time { return time.Date(2026, 10, 22, 10, 6, 0, 0, time.UTC) }
		},
		"no Groundcrew command key":         func(r *Runner, c *signed) { r.Key = func() ed25519.PublicKey { return nil } },
		"not in this agent's configuration": func(r *Runner, c *signed) { r.Config.Services[0].Name = "other" },
	}
	for want, change := range cases {
		r, rt := runner(t, "restart")
		c := vector()
		change(r, &c)
		if _, _, err := r.check(c); err == nil || !strings.Contains(err.Error(), want) {
			t.Errorf("%s: got %v", want, err)
		}
		if rt.restarts != 0 {
			t.Errorf("%s: restarted anyway", want)
		}
	}
}
