package inventory

import (
	"context"
	"encoding/json"
	"errors"
	"net"
	"net/http"
	"path/filepath"
	"strings"
	"testing"

	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/config"
)

// fakeDocker serves a few Engine API answers on a Unix socket.
func fakeDocker(t *testing.T) string {
	t.Helper()
	socket := filepath.Join(t.TempDir(), "docker.sock")
	ln, err := net.Listen("unix", socket)
	if err != nil {
		t.Fatal(err)
	}
	mux := http.NewServeMux()
	flask := map[string]any{
		"Id": "abc", "Name": "/flask-app", "Image": "sha256:img1",
		"State":           map[string]any{"Status": "running"},
		"Config":          map[string]any{"Image": "ghcr.io/acme/flask-app:af37"},
		"NetworkSettings": map[string]any{"Ports": map[string]any{"8000/tcp": nil, "9090/tcp": nil}},
	}
	mux.HandleFunc("/containers/flask-app/json", func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(flask)
	})
	mux.HandleFunc("/containers/web123/json", func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(flask)
	})
	mux.HandleFunc("/containers/json", func(w http.ResponseWriter, r *http.Request) {
		if !strings.Contains(r.URL.Query().Get("filters"), "com.docker.compose.service=web") {
			_ = json.NewEncoder(w).Encode([]any{})
			return
		}
		_ = json.NewEncoder(w).Encode([]map[string]string{{"Id": "old", "State": "exited"}, {"Id": "web123", "State": "running"}})
	})
	mux.HandleFunc("/images/sha256:img1/json", func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"RepoDigests": []string{
			"mirror.example/flask@sha256:mirror", "ghcr.io/acme/flask-app@sha256:own",
		}})
	})
	mux.HandleFunc("/", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNotFound)
		_, _ = w.Write([]byte(`{"message":"No such container"}`))
	})
	srv := &http.Server{Handler: mux}
	go func() { _ = srv.Serve(ln) }()
	t.Cleanup(func() { _ = srv.Close() })
	return socket
}

func TestCollectDescribesDockerComposeAndSystemdServices(t *testing.T) {
	cfg, err := config.Parse([]byte(`
groundcrew: {url: https://api.groundkrew.com}
services:
  - {name: flask, runtime: docker, container: flask-app, health: {url: "http://127.0.0.1:8000/health"}, allow: [restart]}
  - {name: shop, runtime: compose, compose: {project: shop, service: web}}
  - {name: gone, runtime: docker, container: missing}
  - {name: nginx, runtime: systemd, unit: nginx.service}
  - {name: ghost, runtime: systemd, unit: ghost.service}
`))
	if err != nil {
		t.Fatal(err)
	}
	systemd := &Systemd{Run: func(_ context.Context, args ...string) ([]byte, error) {
		if args[1] == "nginx.service" {
			return []byte("LoadState=loaded\nActiveState=active\n"), nil
		}
		if args[1] == "ghost.service" {
			return []byte("LoadState=not-found\n"), nil
		}
		return nil, errors.New("unexpected")
	}}
	inv := Collect(context.Background(), cfg, NewDocker(fakeDocker(t)), systemd)
	byName := map[string]Service{}
	for _, s := range inv.Services {
		byName[s.Name] = s
	}
	flask := byName["flask"]
	if !flask.Found || flask.Image != "ghcr.io/acme/flask-app:af37" || flask.ImageDigest != "sha256:own" {
		t.Fatalf("flask %+v", flask)
	}
	if strings.Join(flask.Ports, ",") != "8000/tcp,9090/tcp" || flask.HealthURL != "http://127.0.0.1:8000/health" || flask.Allow[0] != "restart" {
		t.Fatalf("flask %+v", flask)
	}
	if shop := byName["shop"]; !shop.Found || shop.Target != "shop/web" || shop.Allow == nil {
		t.Fatalf("shop %+v", shop)
	}
	if gone := byName["gone"]; gone.Found || gone.Problem != "no container named missing" {
		t.Fatalf("gone %+v", gone)
	}
	if nginx := byName["nginx"]; !nginx.Found || nginx.Target != "nginx.service" {
		t.Fatalf("nginx %+v", nginx)
	}
	if ghost := byName["ghost"]; ghost.Found || !strings.Contains(ghost.Problem, "not loaded (not-found)") {
		t.Fatalf("ghost %+v", ghost)
	}
	if inv.Host.Hostname == "" || inv.Host.OS != "linux" {
		t.Fatalf("host %+v", inv.Host)
	}
	if inv.Hash() != inv.Hash() || len(inv.Hash()) != 64 {
		t.Fatal("hash")
	}
}

func TestADockerThatIsDownIsAProblemNotACrash(t *testing.T) {
	d := NewDocker(filepath.Join(t.TempDir(), "none.sock"))
	got := d.Inspect(context.Background(), config.Service{Name: "x", Runtime: config.Docker, Container: "x"})
	if got.Found || !strings.Contains(got.Problem, "cannot reach Docker") {
		t.Fatalf("%+v", got)
	}
}
