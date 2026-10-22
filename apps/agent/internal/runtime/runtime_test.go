package runtime

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"net"
	"net/http"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/config"
)

// fakeDocker serves a few Engine API answers on a Unix socket.
type fakeDocker struct {
	mu        sync.Mutex
	status    string
	restarted int
}

func (f *fakeDocker) serve(t *testing.T) string {
	t.Helper()
	socket := filepath.Join(t.TempDir(), "docker.sock")
	ln, err := net.Listen("unix", socket)
	if err != nil {
		t.Fatal(err)
	}
	mux := http.NewServeMux()
	inspect := func(w http.ResponseWriter, _ *http.Request) {
		f.mu.Lock()
		defer f.mu.Unlock()
		_ = json.NewEncoder(w).Encode(map[string]any{
			"Id": "abc", "Name": "/flask-app", "Image": "sha256:img1", "RestartCount": 2,
			"State": map[string]any{
				"Status": f.status, "ExitCode": 137, "OOMKilled": f.status == "exited",
				"StartedAt": "2026-10-22T10:00:00Z",
			},
			"Config":          map[string]any{"Image": "ghcr.io/acme/flask-app:af37", "Tty": false},
			"NetworkSettings": map[string]any{"Ports": map[string]any{"8000/tcp": nil, "9090/tcp": nil}},
		})
	}
	mux.HandleFunc("/containers/flask-app/json", inspect)
	mux.HandleFunc("/containers/web123/json", inspect)
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
	mux.HandleFunc("/containers/abc/logs", func(w http.ResponseWriter, _ *http.Request) {
		// Two frames, as Docker multiplexes stdout and stderr without a TTY.
		for _, l := range []string{
			"2026-10-22T10:00:01.000000000Z GET / 200\n",
			"2026-10-22T10:00:02.000000000Z ERROR database unreachable\n",
		} {
			header := make([]byte, 8)
			header[0] = 1
			binary.BigEndian.PutUint32(header[4:], uint32(len(l)))
			_, _ = w.Write(append(header, l...))
		}
	})
	mux.HandleFunc("/containers/abc/restart", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		f.mu.Lock()
		f.status, f.restarted = "running", f.restarted+1
		f.mu.Unlock()
		w.WriteHeader(http.StatusNoContent)
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

var (
	flask = config.Service{Name: "flask", Runtime: config.Docker, Container: "flask-app"}
	shop  = config.Service{Name: "shop", Runtime: config.Compose, Compose: &config.ComposeRef{Project: "shop", Service: "web"}}
	gone  = config.Service{Name: "gone", Runtime: config.Docker, Container: "missing"}
)

func TestDockerInspectsStatesLogsAndRestarts(t *testing.T) {
	f := &fakeDocker{status: "exited"}
	d := NewDocker(f.serve(t))
	ctx := context.Background()

	facts := d.Inspect(ctx, flask)
	if !facts.Found || facts.ImageDigest != "sha256:own" || len(facts.Ports) != 2 {
		t.Fatalf("inspect %+v", facts)
	}
	if got := d.Inspect(ctx, shop); !got.Found {
		t.Fatalf("compose %+v", got)
	}
	if got := d.Inspect(ctx, gone); got.Found || got.Problem != "no container named missing" {
		t.Fatalf("missing %+v", got)
	}

	st := d.State(ctx, flask)
	if st.Running() || st.State != "exited" || st.ExitCode == nil || *st.ExitCode != 137 || !st.OOMKilled || st.Restarts != 2 {
		t.Fatalf("state %+v", st)
	}
	lines, err := d.Logs(ctx, flask, time.Date(2026, 10, 22, 10, 0, 1, 500, time.UTC))
	if err != nil || len(lines) != 1 || lines[0].Text != "ERROR database unreachable" {
		t.Fatalf("logs %+v %v", lines, err)
	}

	if err := d.Restart(ctx, flask); err != nil {
		t.Fatal(err)
	}
	if st := d.State(ctx, flask); !st.Running() || st.ExitCode != nil || f.restarted != 1 {
		t.Fatalf("after restart %+v", st)
	}
	if err := d.Restart(ctx, gone); err == nil {
		t.Fatal("restarting a missing container must fail")
	}
}

func TestADockerThatIsDownIsAProblemNotACrash(t *testing.T) {
	d := NewDocker(filepath.Join(t.TempDir(), "none.sock"))
	got := d.Inspect(context.Background(), flask)
	if got.Found || !strings.Contains(got.Problem, "cannot reach Docker") {
		t.Fatalf("%+v", got)
	}
}

func TestSystemdStatesLogsAndRestarts(t *testing.T) {
	var ran [][]string
	s := &Systemd{Run: func(_ context.Context, name string, args ...string) ([]byte, error) {
		ran = append(ran, append([]string{name}, args...))
		switch {
		case name == "systemctl" && args[0] == "show" && args[1] == "nginx.service":
			return []byte("LoadState=loaded\nActiveState=failed\nNRestarts=4\nExecMainStatus=1\n"), nil
		case name == "systemctl" && args[0] == "show":
			return []byte("LoadState=not-found\n"), nil
		case name == "journalctl":
			return []byte("2026-10-22T10:00:02.000000+0000 host nginx[12]: [emerg] bind() failed\n"), nil
		case name == "systemctl" && args[0] == "restart":
			return nil, errors.New("exit status 1: Interactive authentication required.")
		}
		return nil, errors.New("unexpected")
	}}
	nginx := config.Service{Name: "nginx", Runtime: config.Systemd, Unit: "nginx.service"}
	ctx := context.Background()
	st := s.State(ctx, nginx)
	if st.Running() || st.State != "failed" || st.Restarts != 4 || st.ExitCode == nil || *st.ExitCode != 1 {
		t.Fatalf("state %+v", st)
	}
	if got := s.State(ctx, config.Service{Name: "x", Runtime: config.Systemd, Unit: "ghost.service"}); got.Found || !strings.Contains(got.Problem, "not loaded (not-found)") {
		t.Fatalf("ghost %+v", got)
	}
	lines, err := s.Logs(ctx, nginx, time.Date(2026, 10, 22, 10, 0, 0, 0, time.UTC))
	if err != nil || len(lines) != 1 || lines[0].Text != "[emerg] bind() failed" {
		t.Fatalf("logs %+v %v", lines, err)
	}
	err = s.Restart(ctx, nginx)
	if err == nil || !strings.Contains(err.Error(), "needs permission") {
		t.Fatalf("restart %v", err)
	}
	if last := ran[len(ran)-1]; strings.Join(last, " ") != "systemctl restart --no-ask-password nginx.service" {
		t.Fatalf("ran %v", last)
	}
}
