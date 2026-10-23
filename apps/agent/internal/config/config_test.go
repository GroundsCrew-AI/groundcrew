package config

import (
	"os"
	"regexp"
	"strings"
	"testing"
	"time"
)

const good = `
groundcrew:
  url: https://api.groundkrew.com/
services:
  - name: legacy-flask-app
    runtime: docker
    container: flask-app
    health:
      url: http://127.0.0.1:8000/health
    logs: container
    deploy:
      image: ghcr.io/acme/flask-app
    allow: [restart, rollback, deploy]
  - name: shop
    runtime: compose
    compose: {project: shop, service: web}
  - name: nginx
    runtime: systemd
    unit: nginx.service
    allow: [restart]
`

func TestParseFillsDefaults(t *testing.T) {
	c, err := Parse([]byte(good))
	if err != nil {
		t.Fatal(err)
	}
	if c.Groundcrew.URL != "https://api.groundkrew.com" || c.StateDir != "/var/lib/groundcrew-agent" {
		t.Fatalf("defaults: %+v", c)
	}
	h := c.Services[0].Health
	if h.Interval != 30*time.Second || h.Timeout != 5*time.Second {
		t.Fatalf("health defaults: %+v", h)
	}
	if got := []string{c.Services[0].Target(), c.Services[1].Target(), c.Services[2].Target()}; strings.Join(got, ",") != "flask-app,shop/web,nginx.service" {
		t.Fatalf("targets: %v", got)
	}
	if !c.Services[2].Allows(Restart) || c.Services[1].Allows(Restart) {
		t.Fatal("allow")
	}
}

func TestUnknownKeysAreErrors(t *testing.T) {
	_, err := Parse([]byte(good + "    alow: [restart]\n"))
	if err == nil || !strings.Contains(err.Error(), "alow") {
		t.Fatalf("want an error naming the unknown key, got %v", err)
	}
}

func TestEveryProblemIsReportedAtOnce(t *testing.T) {
	_, err := Parse([]byte(`
groundcrew:
  url: http://groundcrew.example.com
services:
  - name: a
    runtime: docker
  - name: a
    runtime: systemd
    unit: x.service
    allow: [deploy, reboot]
  - name: "bad name"
    runtime: lxc
`))
	if err == nil {
		t.Fatal("want errors")
	}
	for _, want := range []string{
		"must use https",
		`service "a": a docker service needs container`,
		"the name is used twice",
		"a systemd service can allow restart only",
		`not "reboot"`,
		`service "bad name": name must be`,
		"runtime must be docker, compose, systemd, kubernetes, or ecs",
	} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("missing %q in:\n%v", want, err)
		}
	}
}

func TestHTTPIsAllowedForLocalhostOnly(t *testing.T) {
	if _, err := Parse([]byte("groundcrew:\n  url: http://localhost:8000\n")); err != nil {
		t.Fatal(err)
	}
}

func TestClusterServices(t *testing.T) {
	cfg, err := Parse([]byte(`
groundcrew: {url: https://api.groundkrew.com}
services:
  - name: api
    runtime: kubernetes
    kubernetes: {namespace: shop, deployment: api, container: api}
    allow: [restart, rollback, deploy]
  - name: worker
    runtime: ecs
    ecs: {cluster: checkout, service: worker}
    allow: [restart]
`))
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Services[0].Target() != "shop/api" || cfg.Services[1].Target() != "checkout/worker" {
		t.Fatalf("targets %q %q", cfg.Services[0].Target(), cfg.Services[1].Target())
	}
	for _, bad := range []string{
		"{name: a, runtime: kubernetes, kubernetes: {namespace: Shop, deployment: api}}",
		"{name: a, runtime: kubernetes}",
		"{name: a, runtime: ecs, ecs: {cluster: c}}",
		"{name: a, runtime: docker, container: x, ecs: {cluster: c, service: s}}",
		"{name: a, runtime: nomad}",
	} {
		if _, err := Parse([]byte("groundcrew: {url: https://api.groundkrew.com}\nservices: [" + bad + "]")); err == nil {
			t.Errorf("accepted %s", bad)
		}
	}
}

func TestTheConfigurationCanComeFromTheEnvironment(t *testing.T) {
	t.Setenv(ConfigEnv, "groundcrew: {url: https://api.groundkrew.com}\nservices: [{name: api, runtime: ecs, ecs: {cluster: c, service: api}}]\n")
	cfg, err := Load("/does/not/exist")
	if err != nil || cfg.Services[0].Target() != "c/api" {
		t.Fatalf("%+v %v", cfg, err)
	}
}

// The configuration the ECS template writes (packaging/ecs-agent.yaml) parses as the agent's.
func TestTheECSTemplatesConfigurationParses(t *testing.T) {
	raw, err := os.ReadFile("../../packaging/ecs-agent.yaml")
	if err != nil {
		t.Fatal(err)
	}
	m := regexp.MustCompile(`(?s)Name: GROUNDCREW_AGENT_CONFIG\s+Value: !Sub \|\n(.*?)\n          Secrets:`).FindSubmatch(raw)
	if m == nil {
		t.Fatal("no GROUNDCREW_AGENT_CONFIG in the template")
	}
	var lines []string
	for _, l := range strings.Split(string(m[1]), "\n") {
		lines = append(lines, strings.TrimPrefix(l, "                "))
	}
	text := strings.NewReplacer(
		"${GroundcrewApiUrl}", "https://api.groundkrew.com",
		"${Services}", "[{name: api, runtime: ecs, ecs: {cluster: checkout, service: api}, allow: [restart, deploy]}]",
	).Replace(strings.Join(lines, "\n"))
	cfg, err := Parse([]byte(text))
	if err != nil {
		t.Fatalf("%v\n%s", err, text)
	}
	if cfg.StateDir != "/var/lib/groundcrew-agent" || cfg.Services[0].Target() != "checkout/api" {
		t.Fatalf("%+v", cfg)
	}
}
