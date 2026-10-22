// Command groundcrew-agent watches the services listed in its configuration
// file and reports them to Groundcrew. It only connects out; nothing connects
// to it.
//
//	groundcrew-agent check  [--config FILE]   check the file and find each service
//	groundcrew-agent enroll --token TOKEN     join the workspace (once, with the token from Groundcrew)
//	groundcrew-agent run    [--config FILE]   report until stopped (the systemd service runs this)
//	groundcrew-agent version
package main

import (
	"context"
	"crypto/ed25519"
	"encoding/base64"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"os"
	"os/signal"
	"runtime"
	"syscall"
	"time"

	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/agent"
	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/client"
	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/commands"
	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/config"
	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/inventory"
	agentrt "github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/runtime"
	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/state"
	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/watch"
)

// version is set at build time: -ldflags "-X main.version=0.1.0".
var version = "dev"

func main() {
	if err := run(os.Args[1:], os.Stdout, os.Stderr); err != nil {
		fmt.Fprintln(os.Stderr, "groundcrew-agent:", err)
		os.Exit(1)
	}
}

func usage(w io.Writer) {
	fmt.Fprint(w, `Usage:
  groundcrew-agent check  [--config FILE]
  groundcrew-agent enroll --token TOKEN [--config FILE]
  groundcrew-agent run    [--config FILE]
  groundcrew-agent version
`)
}

func run(args []string, stdout, stderr io.Writer) error {
	if len(args) == 0 {
		usage(stderr)
		return errors.New("a command is required")
	}
	cmd, rest := args[0], args[1:]
	fs := flag.NewFlagSet(cmd, flag.ContinueOnError)
	fs.SetOutput(stderr)
	path := fs.String("config", config.DefaultPath, "configuration file")
	token := fs.String("token", "", "join token from the Connections page (enroll only)")
	switch cmd {
	case "version", "--version", "-v":
		fmt.Fprintf(stdout, "groundcrew-agent %s (%s/%s)\n", version, runtime.GOOS, runtime.GOARCH)
		return nil
	case "help", "--help", "-h":
		usage(stdout)
		return nil
	case "check", "enroll", "run":
	default:
		usage(stderr)
		return fmt.Errorf("unknown command %q", cmd)
	}
	if err := fs.Parse(rest); err != nil {
		return err
	}
	cfg, err := config.Load(*path)
	if err != nil {
		return fmt.Errorf("%s:\n%w", *path, err)
	}
	switch cmd {
	case "check":
		return check(cfg, stdout)
	case "enroll":
		return enroll(cfg, *token, stdout)
	default:
		return serve(cfg)
	}
}

func runtimes(cfg *config.Config) (agentrt.Runtime, agentrt.Runtime) {
	return agentrt.NewDocker(cfg.Docker.Socket), agentrt.NewSystemd()
}

func check(cfg *config.Config, w io.Writer) error {
	docker, systemd := runtimes(cfg)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	inv := inventory.Collect(ctx, cfg, docker, systemd)
	fmt.Fprintf(w, "Configuration is valid. Groundcrew: %s\nHost: %s (%s/%s)\n",
		cfg.Groundcrew.URL, inv.Host.Hostname, inv.Host.OS, inv.Host.Arch)
	missing := 0
	for _, s := range inv.Services {
		allowed := "watch only"
		if len(s.Allow) > 0 {
			allowed = fmt.Sprint("may ", s.Allow)
		}
		if s.Found {
			fmt.Fprintf(w, "  ok       %s (%s %s) %s\n", s.Name, s.Runtime, s.Target, allowed)
		} else {
			missing++
			fmt.Fprintf(w, "  missing  %s (%s %s): %s\n", s.Name, s.Runtime, s.Target, s.Problem)
		}
	}
	if len(inv.Services) == 0 {
		fmt.Fprintln(w, "  No services listed yet: add them under services.")
	}
	if missing > 0 {
		return fmt.Errorf("%d of %d services were not found", missing, len(inv.Services))
	}
	return nil
}

type enrollIn struct {
	Token     string `json:"token"`
	PublicKey string `json:"public_key"`
	Hostname  string `json:"hostname"`
	OS        string `json:"os"`
	Arch      string `json:"arch"`
	Version   string `json:"version"`
}

type enrollOut struct {
	AgentID      string `json:"agent_id"`
	ConnectionID string `json:"connection_id"`
	Name         string `json:"name"`
}

func enroll(cfg *config.Config, token string, w io.Writer) error {
	if token == "" {
		return errors.New("--token is required: copy it from Connections > Agent in Groundcrew")
	}
	dir := state.Dir{Path: cfg.StateDir}
	key, err := state.NewKey()
	if err != nil {
		return err
	}
	host := inventory.HostFacts()
	c := client.New(cfg.Groundcrew.URL, "", key, userAgent())
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	var out enrollOut
	err = c.Post(ctx, "/agent/v1/enroll", enrollIn{
		Token:     token,
		PublicKey: base64.StdEncoding.EncodeToString(key.Public().(ed25519.PublicKey)),
		Hostname:  host.Hostname,
		OS:        host.OS,
		Arch:      host.Arch,
		Version:   version,
	}, &out)
	if err != nil {
		return fmt.Errorf("enrollment failed: %w", err)
	}
	// Stored only now: a refused token leaves the working key and enrollment as they were.
	// A new enrollment trusts the command key its first heartbeat brings.
	if err := dir.ClearCommandKey(); err != nil {
		return err
	}
	if err := dir.SaveKey(key); err != nil {
		return fmt.Errorf("enrolled, but cannot write the key to %s: %w", cfg.StateDir, err)
	}
	if err := dir.SaveEnrollment(state.Enrollment{
		AgentID: out.AgentID, ConnectionID: out.ConnectionID, Name: out.Name, URL: cfg.Groundcrew.URL,
	}); err != nil {
		return err
	}
	fmt.Fprintf(w, "Enrolled as %q. Start the agent: sudo systemctl enable --now groundcrew-agent\n", out.Name)
	return nil
}

func serve(cfg *config.Config) error {
	log := slog.New(slog.NewJSONHandler(os.Stderr, nil))
	dir := state.Dir{Path: cfg.StateDir}
	e, err := dir.Enrollment()
	if err != nil {
		return err
	}
	key, err := dir.Key()
	if err != nil {
		return err
	}
	if e.URL != cfg.Groundcrew.URL {
		return fmt.Errorf("enrolled with %s, but the configuration says %s: enroll again with a new token", e.URL, cfg.Groundcrew.URL)
	}
	docker, systemd := runtimes(cfg)
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	c := client.New(cfg.Groundcrew.URL, e.AgentID, key, userAgent())
	w := &watch.Watcher{Config: cfg, Docker: docker, Systemd: systemd, Client: c, Log: log}
	runner := &commands.Runner{
		Config:  cfg,
		AgentID: e.AgentID,
		Client:  c,
		Docker:  docker,
		Systemd: systemd,
		Log:     log,
		After:   w.Kick,
		Key: func() ed25519.PublicKey {
			k, err := dir.CommandKey()
			if err != nil {
				log.Error("cannot read the pinned command key", "error", err)
			}
			return k
		},
	}
	a := &agent.Agent{
		Config:  cfg,
		Client:  c,
		Docker:  docker,
		Systemd: systemd,
		Log:     log,
		Version: version,
		State:   &dir,
		Watcher: w,
	}
	go w.Run(ctx)
	go runner.Run(ctx)
	return a.Run(ctx)
}

func userAgent() string {
	return fmt.Sprintf("groundcrew-agent/%s (%s/%s)", version, runtime.GOOS, runtime.GOARCH)
}
