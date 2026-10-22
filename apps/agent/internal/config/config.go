// Package config reads and checks the agent's configuration file
// (/etc/groundcrew-agent/agent.yaml): where Groundcrew is, and which services
// the agent watches and may act on. Unknown keys are errors, so a typo never
// silently turns a setting off.
package config

import (
	"bytes"
	"errors"
	"fmt"
	"net/url"
	"os"
	"regexp"
	"strings"
	"time"

	"gopkg.in/yaml.v3"
)

// DefaultPath is where the installer puts the file.
const DefaultPath = "/etc/groundcrew-agent/agent.yaml"

// Runtimes the agent can watch.
const (
	Docker  = "docker"
	Compose = "compose"
	Systemd = "systemd"
)

// Actions a service may allow. Nothing is allowed unless listed.
const (
	Restart  = "restart"
	Rollback = "rollback"
	Deploy   = "deploy"
)

var namePattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$`)

type Config struct {
	Groundcrew Groundcrew `yaml:"groundcrew"`
	// Where the agent keeps its key and enrollment (default /var/lib/groundcrew-agent).
	StateDir string    `yaml:"state_dir"`
	Docker   DockerAPI `yaml:"docker"`
	Services []Service `yaml:"services"`
}

type Groundcrew struct {
	URL string `yaml:"url"`
}

type DockerAPI struct {
	// The Docker Engine socket (default /var/run/docker.sock).
	Socket string `yaml:"socket"`
}

type Service struct {
	Name    string `yaml:"name"`
	Runtime string `yaml:"runtime"`
	// docker: the container name.
	Container string `yaml:"container"`
	// compose: the project and service.
	Compose *ComposeRef `yaml:"compose"`
	// systemd: the unit.
	Unit   string        `yaml:"unit"`
	Health *Health       `yaml:"health"`
	Logs   string        `yaml:"logs"`
	Deploy *DeploySource `yaml:"deploy"`
	Allow  []string      `yaml:"allow"`
}

type ComposeRef struct {
	Project string `yaml:"project"`
	Service string `yaml:"service"`
}

type Health struct {
	URL      string        `yaml:"url"`
	Interval time.Duration `yaml:"interval"`
	Timeout  time.Duration `yaml:"timeout"`
}

type DeploySource struct {
	// Optional: the only image repository deploys may come from (no tag). Unset, any image
	// Groundcrew sends with an approved deploy is accepted.
	Image string `yaml:"image"`
}

// Target names what the service is in its runtime: the container, project/service, or unit.
func (s Service) Target() string {
	switch s.Runtime {
	case Docker:
		return s.Container
	case Compose:
		if s.Compose != nil {
			return s.Compose.Project + "/" + s.Compose.Service
		}
	case Systemd:
		return s.Unit
	}
	return ""
}

// Allows reports whether the configuration lets the agent run action on this service.
func (s Service) Allows(action string) bool {
	for _, a := range s.Allow {
		if a == action {
			return true
		}
	}
	return false
}

// Load reads and checks the file at path.
func Load(path string) (*Config, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	return Parse(raw)
}

// Parse reads a configuration, fills defaults, and checks it.
func Parse(raw []byte) (*Config, error) {
	var c Config
	dec := yaml.NewDecoder(bytes.NewReader(raw))
	dec.KnownFields(true)
	if err := dec.Decode(&c); err != nil {
		return nil, fmt.Errorf("reading the configuration: %w", err)
	}
	c.defaults()
	if err := c.Validate(); err != nil {
		return nil, err
	}
	return &c, nil
}

func (c *Config) defaults() {
	if c.StateDir == "" {
		c.StateDir = "/var/lib/groundcrew-agent"
	}
	if c.Docker.Socket == "" {
		c.Docker.Socket = "/var/run/docker.sock"
	}
	c.Groundcrew.URL = strings.TrimRight(c.Groundcrew.URL, "/")
	for i := range c.Services {
		h := c.Services[i].Health
		if h == nil {
			continue
		}
		if h.Interval == 0 {
			h.Interval = 30 * time.Second
		}
		if h.Timeout == 0 {
			h.Timeout = 5 * time.Second
		}
	}
}

// Validate returns every problem in the file at once, one per line.
func (c *Config) Validate() error {
	var problems []string
	add := func(format string, args ...any) { problems = append(problems, fmt.Sprintf(format, args...)) }

	if c.Groundcrew.URL == "" {
		add("groundcrew.url is required (for example https://api.groundkrew.com)")
	} else if u, err := url.Parse(c.Groundcrew.URL); err != nil || u.Host == "" {
		add("groundcrew.url %q is not a URL", c.Groundcrew.URL)
	} else if u.Scheme != "https" && !local(u.Hostname()) {
		add("groundcrew.url must use https (http only for localhost)")
	}
	seen := map[string]bool{}
	for i, s := range c.Services {
		where := fmt.Sprintf("services[%d]", i)
		if s.Name != "" {
			where = fmt.Sprintf("service %q", s.Name)
		}
		if !namePattern.MatchString(s.Name) {
			add("%s: name must be 1-100 letters, digits, '.', '_' or '-', starting with a letter or digit", where)
		}
		if seen[s.Name] {
			add("%s: the name is used twice", where)
		}
		seen[s.Name] = true
		switch s.Runtime {
		case Docker:
			if s.Container == "" {
				add("%s: a docker service needs container", where)
			}
		case Compose:
			if s.Compose == nil || s.Compose.Project == "" || s.Compose.Service == "" {
				add("%s: a compose service needs compose.project and compose.service", where)
			}
		case Systemd:
			if s.Unit == "" {
				add("%s: a systemd service needs unit", where)
			}
		default:
			add("%s: runtime must be docker, compose, or systemd", where)
		}
		if s.Runtime != Docker && s.Container != "" || s.Runtime != Compose && s.Compose != nil || s.Runtime != Systemd && s.Unit != "" {
			add("%s: container, compose, and unit belong to the docker, compose, and systemd runtimes", where)
		}
		if h := s.Health; h != nil {
			if u, err := url.Parse(h.URL); err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
				add("%s: health.url must be an http(s) URL", where)
			}
			if h.Timeout >= h.Interval {
				add("%s: health.timeout must be shorter than health.interval", where)
			}
		}
		for _, a := range s.Allow {
			switch a {
			case Restart:
			case Rollback, Deploy:
				if s.Runtime == Systemd {
					add("%s: a systemd service can allow restart only", where)
				}
			default:
				add("%s: allow may list restart, rollback, and deploy, not %q", where, a)
			}
		}
		if s.Deploy != nil && strings.ContainsAny(s.Deploy.Image, "@") {
			add("%s: deploy.image is a repository without a tag or digest", where)
		}
	}
	if len(problems) > 0 {
		return errors.New(strings.Join(problems, "\n"))
	}
	return nil
}

func local(host string) bool {
	return host == "localhost" || host == "127.0.0.1" || host == "::1" || strings.HasSuffix(host, ".localhost")
}
