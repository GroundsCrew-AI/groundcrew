package runtime

import (
	"bufio"
	"bytes"
	"context"
	"fmt"
	"os/exec"
	"strconv"
	"strings"
	"time"

	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/config"
	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/inventory"
)

// Systemd reads units with `systemctl show` and `journalctl`, and restarts
// them with `systemctl restart` (which needs a polkit or sudo rule for the
// agent's user; without one the restart is refused and reported).
type Systemd struct {
	// Run executes a command and returns its output (replaced in tests).
	Run func(ctx context.Context, name string, args ...string) ([]byte, error)
}

func NewSystemd() *Systemd {
	return &Systemd{Run: func(ctx context.Context, name string, args ...string) ([]byte, error) {
		ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
		defer cancel()
		out, err := exec.CommandContext(ctx, name, args...).Output()
		if ee, ok := err.(*exec.ExitError); ok && len(ee.Stderr) > 0 {
			return out, fmt.Errorf("%w: %s", err, strings.TrimSpace(string(ee.Stderr)))
		}
		return out, err
	}}
}

func (s *Systemd) show(ctx context.Context, unit string) (map[string]string, error) {
	out, err := s.Run(ctx, "systemctl", "show", unit, "--no-pager",
		"--property=LoadState,ActiveState,SubState,NRestarts,ExecMainStatus,ExecMainStartTimestamp")
	if err != nil {
		return nil, fmt.Errorf("systemctl show %s failed: %v", unit, err)
	}
	props := map[string]string{}
	sc := bufio.NewScanner(bytes.NewReader(out))
	for sc.Scan() {
		if k, v, ok := strings.Cut(sc.Text(), "="); ok {
			props[k] = v
		}
	}
	if props["LoadState"] != "loaded" {
		return nil, fmt.Errorf("unit %s is not loaded (%s)", unit, props["LoadState"])
	}
	return props, nil
}

func (s *Systemd) Inspect(ctx context.Context, svc config.Service) inventory.Service {
	if _, err := s.show(ctx, svc.Unit); err != nil {
		return inventory.Service{Problem: err.Error()}
	}
	return inventory.Service{Found: true, Ports: []string{}}
}

func (s *Systemd) State(ctx context.Context, svc config.Service) State {
	props, err := s.show(ctx, svc.Unit)
	if err != nil {
		return State{Name: svc.Name, Problem: err.Error()}
	}
	st := State{Name: svc.Name, Found: true, State: props["ActiveState"]}
	st.Restarts, _ = strconv.Atoi(props["NRestarts"])
	if st.State != "active" {
		if code, err := strconv.Atoi(props["ExecMainStatus"]); err == nil {
			st.ExitCode = &code
		}
	}
	return st
}

// Logs reads the unit's journal since `since` (the agent's user needs the
// systemd-journal group).
func (s *Systemd) Logs(ctx context.Context, svc config.Service, since time.Time) ([]LogLine, error) {
	out, err := s.Run(ctx, "journalctl", "-u", svc.Unit, "--since", "@"+strconv.FormatInt(since.Unix(), 10),
		"-o", "short-iso-precise", "--no-pager", "-n", "500")
	if err != nil {
		return nil, fmt.Errorf("journalctl -u %s failed: %v", svc.Unit, err)
	}
	var lines []LogLine
	sc := bufio.NewScanner(bytes.NewReader(out))
	for sc.Scan() {
		stamp, rest, ok := strings.Cut(sc.Text(), " ")
		if !ok {
			continue
		}
		at, err := time.Parse("2006-01-02T15:04:05.999999-0700", stamp)
		if err != nil || !at.After(since) {
			continue
		}
		// "host unit[pid]: message"
		if _, msg, ok := strings.Cut(rest, ": "); ok {
			rest = msg
		}
		lines = append(lines, LogLine{At: at, Text: rest})
	}
	return lines, nil
}

func (s *Systemd) Restart(ctx context.Context, svc config.Service) error {
	if _, err := s.Run(ctx, "systemctl", "restart", "--no-ask-password", svc.Unit); err != nil {
		return fmt.Errorf("systemctl restart %s failed (the agent's user needs permission to restart it): %v", svc.Unit, err)
	}
	return nil
}

// HasImage: units run no images.
func (s *Systemd) HasImage(context.Context, string) bool { return false }
