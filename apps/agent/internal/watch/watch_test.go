package watch

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"strings"
	"testing"
	"time"

	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/config"
	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/inventory"
	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/runtime"
)

type fakeRuntime struct {
	state runtime.State
	logs  []runtime.LogLine
}

func (f *fakeRuntime) Inspect(context.Context, config.Service) inventory.Service {
	return inventory.Service{Found: true}
}
func (f *fakeRuntime) State(_ context.Context, s config.Service) runtime.State {
	st := f.state
	st.Name = s.Name
	return st
}
func (f *fakeRuntime) Logs(_ context.Context, _ config.Service, since time.Time) ([]runtime.LogLine, error) {
	var out []runtime.LogLine
	for _, l := range f.logs {
		if l.At.After(since) {
			out = append(out, l)
		}
	}
	return out, nil
}
func (f *fakeRuntime) HasImage(context.Context, string) bool         { return true }
func (f *fakeRuntime) Restart(context.Context, config.Service) error { return nil }

type poster struct {
	reports []report
	raw     []string
	fail    bool
}

func (p *poster) Post(_ context.Context, _ string, in, _ any) error {
	if p.fail {
		return errors.New("offline")
	}
	b, _ := json.Marshal(in)
	p.raw = append(p.raw, string(b))
	var r report
	_ = json.Unmarshal(b, &r)
	p.reports = append(p.reports, r)
	return nil
}

func TestReportsCarryStateChangesHealthAndRedactedErrorLines(t *testing.T) {
	cfg, err := config.Parse([]byte("groundcrew: {url: https://api.groundkrew.com}\nservices:\n  - {name: web, runtime: docker, container: web, health: {url: 'http://127.0.0.1:8000/health'}}\n"))
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	rt := &fakeRuntime{
		state: runtime.State{Found: true, State: "running", Restarts: 1, Image: "web:1", ImageID: "sha256:aaa"},
		logs: []runtime.LogLine{
			{At: now.Add(-10 * time.Second), Text: "GET /orders 200"},
			{At: now.Add(-5 * time.Second), Text: "ERROR db: postgres://app:hunter2@db/app refused"},
		},
	}
	p := &poster{}
	failing := true
	w := &Watcher{
		Config: cfg, Runtimes: runtime.Set{Docker: rt, Systemd: rt}, Client: p,
		Log: slog.New(slog.NewTextHandler(io.Discard, nil)),
		Probe: func(context.Context, string, time.Duration) error {
			if failing {
				return errors.New("HTTP 502")
			}
			return nil
		},
	}
	ctx := context.Background()
	w.init()
	w.probeDue(ctx, now)
	if err := w.Report(ctx); err != nil {
		t.Fatal(err)
	}
	first := p.reports[0]
	if s := first.States[0]; !s.Running() || s.HealthFailures != 1 || s.HealthDetail != "HTTP 502" {
		t.Fatalf("state %+v", s)
	}
	var kinds []string
	for _, e := range first.Events {
		kinds = append(kinds, e.Kind+":"+e.Message)
	}
	joined := strings.Join(kinds, " | ")
	if !strings.Contains(joined, "health:failed: HTTP 502") || !strings.Contains(joined, "log:ERROR db: postgres://app:[REDACTED]@db/app refused") ||
		strings.Contains(joined, "GET /orders") || strings.Contains(joined, "hunter2") {
		t.Fatalf("events %s", joined)
	}

	// The container dies with a new image digest; the check passes again.
	rt.state = runtime.State{Found: true, State: "exited", Restarts: 2, ExitCode: ptr(137), OOMKilled: true, Image: "web:2", ImageID: "sha256:bbb"}
	failing = false
	w.probeDue(ctx, now.Add(time.Minute))
	if err := w.Report(ctx); err != nil {
		t.Fatal(err)
	}
	second := p.reports[1]
	var msgs []string
	for _, e := range second.Events {
		msgs = append(msgs, e.Kind+":"+e.Message)
	}
	got := strings.Join(msgs, " | ")
	for _, want := range []string{
		"health:recovered after 1 failed checks",
		"state:exited (exit code 137, OOM killed)",
		"restart:restarted (2 restarts so far)",
		"deploy:image changed from web:1@aaa to web:2@bbb",
	} {
		if !strings.Contains(got, want) {
			t.Errorf("missing %q in %s", want, got)
		}
	}
	if strings.Contains(got, "ERROR db") {
		t.Errorf("a log line was sent twice: %s", got)
	}
	if s := second.States[0]; s.HealthFailures != 0 || s.State != "exited" ||
		s.PreviousImageID != "sha256:aaa" || s.PreviousImage != "web:1" {
		t.Fatalf("state %+v", s)
	}
	if first.States[0].PreviousImageID != "" {
		t.Fatalf("no previous image before the first change: %+v", first.States[0])
	}
}

func TestEventsAreKeptWhileGroundcrewIsUnreachable(t *testing.T) {
	cfg, _ := config.Parse([]byte("groundcrew: {url: https://api.groundkrew.com}\nservices:\n  - {name: web, runtime: docker, container: web}\n"))
	rt := &fakeRuntime{state: runtime.State{Found: true, State: "running"}}
	p := &poster{}
	w := &Watcher{Config: cfg, Runtimes: runtime.Set{Docker: rt, Systemd: rt}, Client: p, Log: slog.New(slog.NewTextHandler(io.Discard, nil))}
	ctx := context.Background()
	_ = w.Report(ctx)
	if !strings.Contains(p.raw[0], `"events":[]`) {
		t.Fatalf("a report with no events must send a list: %s", p.raw[0])
	}
	rt.state = runtime.State{Found: true, State: "exited", ExitCode: ptr(1)}
	p.fail = true
	if err := w.Report(ctx); err == nil {
		t.Fatal("want an error while offline")
	}
	p.fail = false
	if err := w.Report(ctx); err != nil {
		t.Fatal(err)
	}
	last := p.reports[len(p.reports)-1]
	if len(last.Events) != 1 || last.Events[0].Message != "exited (exit code 1)" {
		t.Fatalf("events %+v", last.Events)
	}
}

func ptr(i int) *int { return &i }
