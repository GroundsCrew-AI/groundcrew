// Package watch reports the services' live state to Groundcrew every 10
// seconds (or what Groundcrew asks for): whether each runs, its restarts and
// last exit, consecutive failed health checks, and what changed since the
// last report as events (state changes, failed and recovered health checks,
// restarts, image changes, error log lines redacted on the host).
// Groundcrew's detector rules run over these reports
// (apps/api/app/remote_agents/signals.py).
package watch

import (
	"context"
	"fmt"
	"log/slog"
	"net/http"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/config"
	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/redact"
	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/runtime"
)

const (
	DefaultEvery = 10 * time.Second
	// Error lines sent per service per report, and what is kept while Groundcrew is unreachable.
	maxLogLines   = 20
	maxPending    = 1000
	maxMessageLen = 500
	// On start, read this much of each service's recent log.
	logBackfill = time.Minute
)

// Error lines: the words loggers use for errors, or an error level field.
var errorLine = regexp.MustCompile(`(?i)\b(error|exception|traceback|fatal|panic|critical)\b|level=(error|fatal)|"level":\s*"(error|fatal|critical)"`)

type Event struct {
	Service string    `json:"service"`
	At      time.Time `json:"at"`
	Kind    string    `json:"kind"`
	Message string    `json:"message"`
}

type report struct {
	States []runtime.State `json:"states"`
	Events []Event         `json:"events"`
}

// Poster sends a report (client.Client).
type Poster interface {
	Post(ctx context.Context, path string, in, out any) error
}

type health struct {
	failures int
	detail   string
	next     time.Time
}

type Watcher struct {
	Config  *config.Config
	Docker  runtime.Runtime
	Systemd runtime.Runtime
	Client  Poster
	Log     *slog.Logger
	// Probe runs health checks (replaced in tests).
	Probe func(ctx context.Context, url string, timeout time.Duration) error

	mu       sync.Mutex
	every    time.Duration
	last     map[string]runtime.State
	health   map[string]*health
	logSince map[string]time.Time
	pending  []Event
	kick     chan struct{}
}

func (w *Watcher) init() {
	if w.every == 0 {
		w.every = DefaultEvery
	}
	if w.last == nil {
		w.last = map[string]runtime.State{}
		w.health = map[string]*health{}
		w.logSince = map[string]time.Time{}
		w.kick = make(chan struct{}, 1)
	}
	if w.Probe == nil {
		w.Probe = probe
	}
}

// SetEvery changes the report interval (Groundcrew's events_seconds).
func (w *Watcher) SetEvery(d time.Duration) {
	w.mu.Lock()
	defer w.mu.Unlock()
	if d >= time.Second {
		w.every = d
	}
}

// Kick asks for a report now (after a command ran).
func (w *Watcher) Kick() {
	w.mu.Lock()
	w.init()
	w.mu.Unlock()
	select {
	case w.kick <- struct{}{}:
	default:
	}
}

// Run reports until ctx ends.
func (w *Watcher) Run(ctx context.Context) {
	w.mu.Lock()
	w.init()
	w.mu.Unlock()
	tick := time.NewTicker(time.Second)
	defer tick.Stop()
	var lastReport time.Time
	for {
		now := time.Now()
		w.probeDue(ctx, now)
		w.mu.Lock()
		every := w.every
		w.mu.Unlock()
		if now.Sub(lastReport) >= every {
			if err := w.Report(ctx); err != nil {
				w.Log.Warn("cannot send the service report; keeping its events", "error", err)
			}
			lastReport = now
		}
		select {
		case <-ctx.Done():
			return
		case <-tick.C:
		case <-w.kick:
			lastReport = time.Time{}
		}
	}
}

func (w *Watcher) probeDue(ctx context.Context, now time.Time) {
	for _, s := range w.Config.Services {
		if s.Health == nil {
			continue
		}
		w.mu.Lock()
		h := w.health[s.Name]
		if h == nil {
			h = &health{}
			w.health[s.Name] = h
		}
		due := !now.Before(h.next)
		w.mu.Unlock()
		if !due {
			continue
		}
		err := w.Probe(ctx, s.Health.URL, s.Health.Timeout)
		w.mu.Lock()
		h.next = now.Add(s.Health.Interval)
		if err != nil {
			h.failures++
			h.detail = trim(err.Error())
			w.add(Event{Service: s.Name, At: now, Kind: "health", Message: "failed: " + h.detail})
		} else {
			if h.failures > 0 {
				w.add(Event{Service: s.Name, At: now, Kind: "health", Message: fmt.Sprintf("recovered after %d failed checks", h.failures)})
			}
			h.failures, h.detail = 0, ""
		}
		w.mu.Unlock()
	}
}

// Report reads every service now and sends its state with the events since
// the last report. On failure the events are kept for the next one.
func (w *Watcher) Report(ctx context.Context) error {
	w.mu.Lock()
	w.init()
	w.mu.Unlock()
	now := time.Now()
	states := make([]runtime.State, 0, len(w.Config.Services))
	for _, s := range w.Config.Services {
		rt := runtime.For(s, w.Docker, w.Systemd)
		st := rt.State(ctx, s)
		w.mu.Lock()
		if h := w.health[s.Name]; h != nil {
			st.HealthFailures, st.HealthDetail = h.failures, h.detail
		}
		w.changes(s.Name, st, now)
		w.last[s.Name] = st
		since, seen := w.logSince[s.Name]
		w.mu.Unlock()
		if !seen {
			since = now.Add(-logBackfill)
		}
		if st.Found {
			w.readLogs(ctx, rt, s, since, now)
		}
		states = append(states, st)
	}
	w.mu.Lock()
	events := append([]Event{}, w.pending...) // [] not null: the API wants a list
	w.mu.Unlock()
	if err := w.Client.Post(ctx, "/agent/v1/events", report{States: states, Events: events}, nil); err != nil {
		return err
	}
	w.mu.Lock()
	w.pending = w.pending[min(len(events), len(w.pending)):]
	w.mu.Unlock()
	return nil
}

// changes records what changed since the last report (w.mu held).
func (w *Watcher) changes(name string, st runtime.State, now time.Time) {
	before, known := w.last[name]
	if !known {
		return
	}
	if st.Found != before.Found || st.State != before.State {
		w.add(Event{Service: name, At: now, Kind: "state", Message: describe(st)})
	}
	if st.Found && before.Found && st.Restarts > before.Restarts {
		w.add(Event{Service: name, At: now, Kind: "restart", Message: fmt.Sprintf("restarted (%d restarts so far)", st.Restarts)})
	}
	if st.ImageDigest != "" && before.ImageDigest != "" && st.ImageDigest != before.ImageDigest {
		w.add(Event{Service: name, At: now, Kind: "deploy", Message: "image changed from " + before.ImageDigest + " to " + st.ImageDigest})
	}
}

func (w *Watcher) readLogs(ctx context.Context, rt runtime.Runtime, s config.Service, since, now time.Time) {
	lines, err := rt.Logs(ctx, s, since)
	if err != nil {
		w.Log.Debug("cannot read logs", "service", s.Name, "error", err)
		return
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	w.logSince[s.Name] = now
	sent := 0
	for _, l := range lines {
		if l.At.After(w.logSince[s.Name]) {
			w.logSince[s.Name] = l.At
		}
		if !errorLine.MatchString(l.Text) || sent >= maxLogLines {
			continue
		}
		w.add(Event{Service: s.Name, At: l.At, Kind: "log", Message: trim(redact.Line(l.Text))})
		sent++
	}
}

// add queues an event (w.mu held), dropping the oldest past maxPending.
func (w *Watcher) add(e Event) {
	w.pending = append(w.pending, e)
	if len(w.pending) > maxPending {
		w.pending = w.pending[len(w.pending)-maxPending:]
	}
}

func describe(st runtime.State) string {
	if !st.Found {
		return "not found: " + st.Problem
	}
	text := st.State
	if !st.Running() && st.ExitCode != nil {
		text += fmt.Sprintf(" (exit code %d", *st.ExitCode)
		if st.OOMKilled {
			text += ", OOM killed"
		}
		text += ")"
	}
	return text
}

func trim(s string) string {
	s = strings.Join(strings.Fields(s), " ")
	if len(s) > maxMessageLen {
		s = s[:maxMessageLen-3] + "..."
	}
	return s
}

var probeClient = &http.Client{
	// A health check judges the service, not where it redirects.
	CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
}

func probe(ctx context.Context, url string, timeout time.Duration) error {
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return err
	}
	req.Header.Set("User-Agent", "groundcrew-agent health check")
	resp, err := probeClient.Do(req)
	if err != nil {
		return err
	}
	resp.Body.Close()
	if resp.StatusCode >= 400 {
		return fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	return nil
}
