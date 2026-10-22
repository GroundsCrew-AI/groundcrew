package agent

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/client"
	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/config"
	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/inventory"
)

type stub struct{}

func (stub) Inspect(context.Context, config.Service) inventory.Service {
	return inventory.Service{Found: true}
}

func TestRunSendsTheInventoryAndHeartbeats(t *testing.T) {
	var mu sync.Mutex
	calls := map[string]int{}
	var sent inventory.Inventory
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		mu.Lock()
		defer mu.Unlock()
		calls[r.URL.Path]++
		switch r.URL.Path {
		case "/agent/v1/inventory":
			_ = json.Unmarshal(body, &sent)
			_, _ = w.Write([]byte(`{"changed": true, "discovery_run_id": "r1"}`))
		case "/agent/v1/heartbeat":
			_, _ = w.Write([]byte(`{"heartbeat_seconds": 1, "inventory_hash": "h"}`))
		}
	}))
	defer srv.Close()

	cfg, err := config.Parse([]byte("groundcrew: {url: http://localhost}\nservices:\n  - {name: web, runtime: docker, container: web}\n"))
	if err != nil {
		t.Fatal(err)
	}
	_, key, _ := ed25519.GenerateKey(rand.Reader)
	a := &Agent{
		Config:  cfg,
		Client:  client.New(srv.URL, "agent-1", key, "test"),
		Docker:  stub{},
		Systemd: stub{},
		Log:     slog.New(slog.NewTextHandler(io.Discard, nil)),
		Version: "test",
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2500*time.Millisecond)
	defer cancel()
	if err := a.Run(ctx); err != nil {
		t.Fatal(err)
	}
	mu.Lock()
	defer mu.Unlock()
	// Inventory once (unchanged afterwards), heartbeats every second as asked.
	if calls["/agent/v1/inventory"] != 1 || calls["/agent/v1/heartbeat"] < 2 {
		t.Fatalf("calls %v", calls)
	}
	if len(sent.Services) != 1 || sent.Services[0].Name != "web" || sent.Services[0].Target != "web" {
		t.Fatalf("inventory %+v", sent)
	}
}
