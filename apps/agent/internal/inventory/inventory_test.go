package inventory

import (
	"context"
	"testing"

	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/config"
)

type stub map[string]Service

func (s stub) Inspect(_ context.Context, svc config.Service) Service { return s[svc.Name] }

func TestCollectMergesWhatTheConfigurationSays(t *testing.T) {
	cfg, err := config.Parse([]byte(`
groundcrew: {url: https://api.groundkrew.com}
services:
  - {name: flask, runtime: docker, container: flask-app, health: {url: "http://127.0.0.1:8000/health"}, allow: [restart]}
  - {name: shop, runtime: compose, compose: {project: shop, service: web}}
  - {name: nginx, runtime: systemd, unit: nginx.service}
`))
	if err != nil {
		t.Fatal(err)
	}
	docker := stub{
		"flask": {Found: true, Image: "ghcr.io/acme/flask-app:af37", Ports: []string{"9090/tcp", "8000/tcp"}},
		"shop":  {Problem: "no container for Compose service web in project shop"},
	}
	systemd := stub{"nginx": {Found: true}}
	inv := Collect(context.Background(), cfg, func(s config.Service) Inspector {
		if s.Runtime == config.Systemd {
			return systemd
		}
		return docker
	})
	flask, shop, nginx := inv.Services[0], inv.Services[1], inv.Services[2]
	if flask.Name != "flask" || flask.Target != "flask-app" || flask.Ports[0] != "8000/tcp" ||
		flask.HealthURL != "http://127.0.0.1:8000/health" || flask.Allow[0] != "restart" {
		t.Fatalf("flask %+v", flask)
	}
	if shop.Found || shop.Target != "shop/web" || shop.Allow == nil || shop.Ports == nil {
		t.Fatalf("shop %+v", shop)
	}
	if !nginx.Found || nginx.Target != "nginx.service" || nginx.Runtime != "systemd" {
		t.Fatalf("nginx %+v", nginx)
	}
	if inv.Host.Hostname == "" || inv.Host.OS != "linux" || len(inv.Hash()) != 64 {
		t.Fatalf("host %+v", inv.Host)
	}
}
