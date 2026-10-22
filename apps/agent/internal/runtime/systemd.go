package inventory

import (
	"bufio"
	"bytes"
	"context"
	"fmt"
	"os/exec"
	"strings"
	"time"

	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/config"
)

// Systemd reads units with `systemctl show` (read-only).
type Systemd struct {
	// Run executes systemctl with args and returns its output (replaced in tests).
	Run func(ctx context.Context, args ...string) ([]byte, error)
}

func NewSystemd() *Systemd {
	return &Systemd{Run: func(ctx context.Context, args ...string) ([]byte, error) {
		ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
		defer cancel()
		return exec.CommandContext(ctx, "systemctl", args...).Output()
	}}
}

func (s *Systemd) Inspect(ctx context.Context, svc config.Service) Service {
	out, err := s.Run(ctx, "show", svc.Unit, "--no-pager",
		"--property=LoadState,ActiveState,SubState,ExecMainPID")
	if err != nil {
		return Service{Problem: fmt.Sprintf("systemctl show %s failed: %v", svc.Unit, err)}
	}
	props := parseShow(out)
	if props["LoadState"] != "loaded" {
		return Service{Problem: fmt.Sprintf("unit %s is not loaded (%s)", svc.Unit, props["LoadState"])}
	}
	return Service{Found: true, Ports: []string{}}
}

func parseShow(out []byte) map[string]string {
	props := map[string]string{}
	sc := bufio.NewScanner(bytes.NewReader(out))
	for sc.Scan() {
		if k, v, ok := strings.Cut(sc.Text(), "="); ok {
			props[k] = v
		}
	}
	return props
}
