// Package inventory describes the host and the configured services the way
// Groundcrew's /agent/v1/inventory expects them (apps/api/app/remote_agents/inventory.py).
// It reads only metadata: container and unit names, images, ports, states.
// Environment values never leave the host.
package inventory

import (
	"bufio"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"runtime"
	"sort"
	"strings"

	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/config"
)

type Host struct {
	Hostname     string `json:"hostname"`
	OS           string `json:"os"`
	Arch         string `json:"arch"`
	Kernel       string `json:"kernel,omitempty"`
	Distribution string `json:"distribution,omitempty"`
}

type Service struct {
	Name        string   `json:"name"`
	Runtime     string   `json:"runtime"`
	Target      string   `json:"target"`
	Image       string   `json:"image,omitempty"`
	ImageDigest string   `json:"image_digest,omitempty"`
	Ports       []string `json:"ports"`
	HealthURL   string   `json:"health_url,omitempty"`
	Logs        string   `json:"logs,omitempty"`
	DeployImage string   `json:"deploy_image,omitempty"`
	Allow       []string `json:"allow"`
	Found       bool     `json:"found"`
	Problem     string   `json:"problem,omitempty"`
}

type Inventory struct {
	Host     Host      `json:"host"`
	Services []Service `json:"services"`
}

// Hash identifies an inventory, to send it only when it changed.
func (inv Inventory) Hash() string {
	b, _ := json.Marshal(inv)
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

// Inspector reads one service from its runtime. Missing is not an error: the
// service is reported with Found false and the reason in Problem.
type Inspector interface {
	Inspect(ctx context.Context, s config.Service) Service
}

// Collect describes the host and every configured service.
func Collect(ctx context.Context, cfg *config.Config, docker, systemd Inspector) Inventory {
	inv := Inventory{Host: HostFacts(), Services: []Service{}}
	for _, s := range cfg.Services {
		var found Service
		switch s.Runtime {
		case config.Docker, config.Compose:
			found = docker.Inspect(ctx, s)
		case config.Systemd:
			found = systemd.Inspect(ctx, s)
		}
		inv.Services = append(inv.Services, merge(s, found))
	}
	return inv
}

// merge fills what the configuration says (names, health check, what is allowed).
func merge(s config.Service, found Service) Service {
	found.Name, found.Runtime, found.Target = s.Name, s.Runtime, s.Target()
	found.Logs = s.Logs
	if s.Health != nil {
		found.HealthURL = s.Health.URL
	}
	if s.Deploy != nil {
		found.DeployImage = s.Deploy.Image
	}
	found.Allow = append([]string{}, s.Allow...)
	if found.Ports == nil {
		found.Ports = []string{}
	}
	sort.Strings(found.Ports)
	return found
}

// HostFacts are the host's name, OS, architecture, kernel, and distribution.
func HostFacts() Host {
	name, _ := os.Hostname()
	h := Host{Hostname: name, OS: runtime.GOOS, Arch: runtime.GOARCH}
	if b, err := os.ReadFile("/proc/sys/kernel/osrelease"); err == nil {
		h.Kernel = strings.TrimSpace(string(b))
	}
	h.Distribution = osRelease("/etc/os-release")
	return h
}

func osRelease(path string) string {
	f, err := os.Open(path)
	if err != nil {
		return ""
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		if v, ok := strings.CutPrefix(sc.Text(), "PRETTY_NAME="); ok {
			return strings.Trim(v, `"'`)
		}
	}
	return ""
}
