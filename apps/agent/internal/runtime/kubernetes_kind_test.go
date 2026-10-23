package runtime

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"net/http"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/config"
)

// Against a live cluster with the agent's own RBAC (the manifest the install dialog writes),
// opt in: GC_KIND_TEST=1 GC_KUBE_API=https://127.0.0.1:<port> GC_KUBE_TOKEN_FILE=... GC_KUBE_CA=...
// GC_KUBE_IMAGE=<registry>/gc-live/web (tags good, good2, bad). Namespace gc-agent-live holds
// Deployment web on :good; the agent may act there and nowhere else.
func TestKubernetesOnALiveCluster(t *testing.T) {
	if os.Getenv("GC_KIND_TEST") != "1" {
		t.Skip("set GC_KIND_TEST=1 to run against a live cluster")
	}
	ca, err := os.ReadFile(os.Getenv("GC_KUBE_CA"))
	if err != nil {
		t.Fatal(err)
	}
	pool := x509.NewCertPool()
	pool.AppendCertsFromPEM(ca)
	k := &Kubernetes{
		API: os.Getenv("GC_KUBE_API"),
		Token: func() (string, error) {
			b, err := os.ReadFile(os.Getenv("GC_KUBE_TOKEN_FILE"))
			return strings.TrimSpace(string(b)), err
		},
		HTTP:    &http.Client{Timeout: 30 * time.Second, Transport: &http.Transport{TLSClientConfig: &tls.Config{RootCAs: pool}}},
		Rollout: 90 * time.Second,
	}
	img := os.Getenv("GC_KUBE_IMAGE")
	ctx := context.Background()
	web := config.Service{Name: "web", Runtime: config.Kubernetes,
		Kubernetes: &config.KubernetesRef{Namespace: "gc-agent-live", Deployment: "web"}}

	if st := k.State(ctx, web); !st.Running() || st.Image != img+":good" {
		t.Fatalf("state %+v", st)
	}
	if _, err := k.Deploy(ctx, web, DeployRequest{Image: img + ":good2", Env: map[string]string{"A": "1"}}); err != nil {
		t.Fatalf("deploy: %v", err)
	}
	if st := k.State(ctx, web); st.Image != img+":good2" || st.PreviousImage != img+":good" {
		t.Fatalf("after deploy %+v", st)
	}
	_, err = k.Deploy(ctx, web, DeployRequest{Image: img + ":bad"})
	if err == nil || !strings.Contains(err.Error(), "previous version was put back") {
		t.Fatalf("bad deploy: %v", err)
	}
	t.Logf("bad deploy said: %v", err)
	if st := k.State(ctx, web); st.Image != img+":good2" || !st.Running() {
		t.Fatalf("not put back %+v", st)
	}
	if err := k.Rollback(ctx, web, img+":good", img+":good", nil); err != nil || k.State(ctx, web).Image != img+":good" {
		t.Fatalf("rollback: %v", err)
	}
	if err := k.Restart(ctx, web); err != nil {
		t.Fatalf("restart: %v", err)
	}
	if lines, err := k.Logs(ctx, web, time.Now().Add(-10*time.Minute)); err != nil {
		t.Fatalf("logs: %v (%d lines)", err, len(lines))
	}
	fresh := config.Service{Name: "fresh", Runtime: config.Kubernetes,
		Kubernetes: &config.KubernetesRef{Namespace: "gc-agent-live", Deployment: "fresh"}}
	// No HealthPath: it is checked at <service>.<namespace>.svc, which resolves inside the cluster
	// (where the agent runs), not on the machine running this test.
	if res, err := k.Deploy(ctx, fresh, DeployRequest{Image: img + ":good", Port: 80}); err != nil || !res.Created {
		t.Fatalf("create: %+v %v", res, err)
	}
	// Its RBAC reaches gc-agent-live only.
	other := config.Service{Name: "x", Runtime: config.Kubernetes,
		Kubernetes: &config.KubernetesRef{Namespace: "kube-system", Deployment: "coredns"}}
	if err := k.Restart(ctx, other); err == nil || !strings.Contains(err.Error(), "403") {
		t.Fatalf("restart outside its namespaces: %v", err)
	}
	if st := k.State(ctx, other); st.Found || !strings.Contains(st.Problem, "403") {
		t.Fatalf("read outside its namespaces: %+v", st)
	}
}
