package runtime

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/config"
)

// fakeKube is a tiny Kubernetes API: Deployments with their ReplicaSet history, one pod each,
// Services and Secrets. A template whose image contains "bad" never becomes available and its
// pod waits in CrashLoopBackOff, like a crashing app.
type fakeKube struct {
	mu          sync.Mutex
	deployments map[string]map[string]any // ns/name -> object
	history     map[string][]map[string]any // ns/name -> pod template per revision
	secrets     map[string]map[string]any
	services    map[string]bool
	requests    []string
}

func newFakeKube() *fakeKube {
	return &fakeKube{
		deployments: map[string]map[string]any{},
		history:     map[string][]map[string]any{},
		secrets:     map[string]map[string]any{},
		services:    map[string]bool{},
	}
}

func (f *fakeKube) add(ns, name, image string) {
	f.deployments[ns+"/"+name] = map[string]any{
		"metadata": map[string]any{"name": name, "generation": float64(1), "uid": "uid-" + name},
		"spec": map[string]any{
			"replicas": float64(1),
			"selector": map[string]any{"matchLabels": map[string]any{"app": name}},
			"template": map[string]any{"spec": map[string]any{"containers": []any{
				map[string]any{"name": name, "image": image, "ports": []any{map[string]any{"containerPort": float64(8000)}}},
			}}},
		},
	}
	f.history[ns+"/"+name] = []map[string]any{template(f.deployments[ns+"/"+name])}
}

// template is a copy of the Deployment's pod template, as its ReplicaSet keeps it.
func template(d map[string]any) map[string]any {
	b, _ := json.Marshal(d["spec"].(map[string]any)["template"])
	var out map[string]any
	_ = json.Unmarshal(b, &out)
	return out
}

func templateImage(t map[string]any) string {
	return t["spec"].(map[string]any)["containers"].([]any)[0].(map[string]any)["image"].(string)
}

func appContainer(d map[string]any) map[string]any {
	spec := d["spec"].(map[string]any)["template"].(map[string]any)["spec"].(map[string]any)
	return spec["containers"].([]any)[0].(map[string]any)
}

func (f *fakeKube) image(ns, name string) string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return appContainer(f.deployments[ns+"/"+name])["image"].(string)
}

// withStatus fills status as the controller would once the rollout settled.
func (f *fakeKube) withStatus(d map[string]any) map[string]any {
	gen := d["metadata"].(map[string]any)["generation"]
	avail := 1
	if strings.Contains(appContainer(d)["image"].(string), "bad") {
		avail = 0
	}
	d["status"] = map[string]any{
		"observedGeneration": gen, "replicas": 1, "updatedReplicas": 1,
		"readyReplicas": avail, "availableReplicas": avail,
	}
	return d
}

func (f *fakeKube) changed(key string, d map[string]any) {
	meta := d["metadata"].(map[string]any)
	meta["generation"] = meta["generation"].(float64) + 1
	f.history[key] = append(f.history[key], template(d))
}

func write(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func (f *fakeKube) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if r.Header.Get("Authorization") != "Bearer sa-token" {
		write(w, 401, map[string]string{"message": "Unauthorized"})
		return
	}
	f.requests = append(f.requests, r.Method+" "+r.URL.Path)
	parts := strings.Split(strings.Trim(r.URL.Path, "/"), "/")
	var body map[string]any
	if r.Body != nil {
		b, _ := io.ReadAll(r.Body)
		_ = json.Unmarshal(b, &body)
	}
	// /apis/apps/v1/namespaces/{ns}/deployments[/{name}]
	if len(parts) >= 6 && parts[1] == "apps" && parts[5] == "deployments" {
		ns := parts[4]
		if len(parts) == 6 && r.Method == http.MethodPost {
			name := body["metadata"].(map[string]any)["name"].(string)
			body["metadata"].(map[string]any)["generation"] = float64(1)
			body["metadata"].(map[string]any)["uid"] = "uid-" + name
			f.deployments[ns+"/"+name] = body
			f.history[ns+"/"+name] = []map[string]any{template(body)}
			write(w, 201, body)
			return
		}
		key := ns + "/" + parts[6]
		d, ok := f.deployments[key]
		if !ok {
			write(w, 404, map[string]string{"message": "not found"})
			return
		}
		switch r.Method {
		case http.MethodGet:
			write(w, 200, f.withStatus(d))
		case http.MethodDelete:
			delete(f.deployments, key)
			write(w, 200, map[string]any{})
		case http.MethodPatch:
			tmpl := body["spec"].(map[string]any)["template"].(map[string]any)
			if r.Header.Get("Content-Type") == "application/merge-patch+json" {
				d["spec"].(map[string]any)["template"] = tmpl // the whole template back
			} else if spec, ok := tmpl["spec"].(map[string]any); ok {
				c := appContainer(d)
				patch := spec["containers"].([]any)[0].(map[string]any)
				c["image"] = patch["image"]
				if env, ok := patch["env"]; ok {
					c["env"] = env
				}
				if s, ok := spec["imagePullSecrets"]; ok {
					d["spec"].(map[string]any)["template"].(map[string]any)["spec"].(map[string]any)["imagePullSecrets"] = s
				}
			}
			f.changed(key, d)
			write(w, 200, f.withStatus(d))
		}
		return
	}
	// /apis/apps/v1/namespaces/{ns}/replicasets
	if len(parts) == 6 && parts[5] == "replicasets" {
		_, name, _ := strings.Cut(r.URL.Query().Get("labelSelector"), "=")
		var items []any
		revisions := f.history[parts[4]+"/"+name]
		for i, tmpl := range revisions {
			image := templateImage(tmpl)
			// The newest ReplicaSet runs the pod (available unless its image is bad); the
			// older ones are scaled to zero.
			status := map[string]any{"replicas": 0, "availableReplicas": 0}
			if i == len(revisions)-1 {
				avail := 1
				if strings.Contains(image, "bad") {
					avail = 0
				}
				status = map[string]any{"replicas": 1, "availableReplicas": avail}
			}
			items = append(items, map[string]any{
				"status": status,
				"metadata": map[string]any{
					"annotations":     map[string]any{"deployment.kubernetes.io/revision": fmt.Sprint(i + 1)},
					"ownerReferences": []any{map[string]any{"uid": "uid-" + name}},
				},
				"spec": map[string]any{"template": tmpl},
			})
		}
		write(w, 200, map[string]any{"items": items})
		return
	}
	// /api/v1/namespaces/{ns}/pods[/{pod}/log]
	if len(parts) >= 5 && parts[4] == "pods" {
		if len(parts) == 7 {
			fmt.Fprintf(w, "%s ready on 8000\n%s boom: cannot reach db password=hunter2\n",
				time.Now().UTC().Format(time.RFC3339Nano), time.Now().UTC().Format(time.RFC3339Nano))
			return
		}
		_, name, _ := strings.Cut(r.URL.Query().Get("labelSelector"), "=")
		d, ok := f.deployments[parts[3]+"/"+name]
		if !ok {
			write(w, 200, map[string]any{"items": []any{}})
			return
		}
		status := map[string]any{"name": name, "restartCount": 2, "imageID": "reg/x@sha256:abc",
			"state":     map[string]any{"running": map[string]any{"startedAt": "2026-10-23T10:00:00Z"}},
			"lastState": map[string]any{"terminated": map[string]any{"exitCode": 137, "reason": "OOMKilled"}}}
		if strings.Contains(appContainer(d)["image"].(string), "bad") {
			status["state"] = map[string]any{"waiting": map[string]any{"reason": "CrashLoopBackOff", "message": "back-off"}}
		}
		write(w, 200, map[string]any{"items": []any{map[string]any{
			"metadata": map[string]any{"name": name + "-pod"},
			"status":   map[string]any{"containerStatuses": []any{status}},
		}}})
		return
	}
	// /api/v1/namespaces/{ns}/secrets[/{name}], services
	if len(parts) >= 5 && (parts[4] == "secrets" || parts[4] == "services") {
		name, _ := body["metadata"].(map[string]any)["name"].(string)
		key := parts[3] + "/" + name
		if parts[4] == "services" {
			f.services[key] = true
			write(w, 201, body)
			return
		}
		if _, exists := f.secrets[key]; exists && r.Method == http.MethodPost {
			write(w, 409, map[string]string{"message": "already exists"})
			return
		}
		f.secrets[key] = body
		write(w, 201, body)
		return
	}
	write(w, 404, map[string]string{"message": "no route " + r.URL.Path})
}

func kubeFixture(t *testing.T) (*fakeKube, *Kubernetes) {
	t.Helper()
	f := newFakeKube()
	srv := httptest.NewServer(f)
	t.Cleanup(srv.Close)
	k := &Kubernetes{
		API:     srv.URL,
		Token:   func() (string, error) { return "sa-token", nil },
		HTTP:    srv.Client(),
		Rollout: 3 * time.Second,
		Poll:    10 * time.Millisecond,
	}
	return f, k
}

func kubeService(name string) config.Service {
	return config.Service{Name: name, Runtime: config.Kubernetes,
		Kubernetes: &config.KubernetesRef{Namespace: "shop", Deployment: name}}
}

func TestKubernetesReadsADeployment(t *testing.T) {
	f, k := kubeFixture(t)
	f.add("shop", "api", "reg/api:1")
	older := template(f.deployments["shop/api"])
	older["spec"].(map[string]any)["containers"].([]any)[0].(map[string]any)["image"] = "reg/api:0"
	f.history["shop/api"] = []map[string]any{older, f.history["shop/api"][0]}
	ctx := context.Background()
	s := kubeService("api")

	st := k.State(ctx, s)
	if !st.Running() || st.Image != "reg/api:1" || st.PreviousImage != "reg/api:0" ||
		st.Restarts != 2 || !st.OOMKilled || *st.ExitCode != 137 || st.ImageDigest != "sha256:abc" {
		t.Fatalf("state %+v", st)
	}
	if inv := k.Inspect(ctx, s); !inv.Found || inv.Ports[0] != "8000/tcp" {
		t.Fatalf("inspect %+v", inv)
	}
	if lines, err := k.Logs(ctx, s, time.Now().Add(-time.Minute)); err != nil || len(lines) != 2 {
		t.Fatalf("logs %v %v", lines, err)
	}
	missing := k.State(ctx, kubeService("nope"))
	if missing.Found || !strings.Contains(missing.Problem, "no deployment nope") {
		t.Fatalf("missing %+v", missing)
	}
}

func TestKubernetesDeploysAndPutsTheOldVersionBackOnFailure(t *testing.T) {
	f, k := kubeFixture(t)
	f.add("shop", "api", "reg/api:1")
	ctx := context.Background()
	s := kubeService("api")
	login := &RegistryLogin{Server: "api.groundkrew.com", Username: "u", Password: "p"}

	res, err := k.Deploy(ctx, s, DeployRequest{Image: "reg/api:2", Env: map[string]string{"DATABASE_URL": "x"}, Login: login})
	if err != nil || res.Created {
		t.Fatalf("deploy: %+v %v", res, err)
	}
	if f.image("shop", "api") != "reg/api:2" || f.secrets["shop/groundcrew-pull-api"] == nil {
		t.Fatalf("not deployed: %s %v", f.image("shop", "api"), f.secrets)
	}
	// The pull secret exists now: a second deploy replaces it (409, then PUT).
	_, err = k.Deploy(ctx, s, DeployRequest{Image: "reg/api:bad", Login: login})
	if err == nil || !strings.Contains(err.Error(), "CrashLoopBackOff") || !strings.Contains(err.Error(), "previous version was put back") {
		t.Fatalf("bad deploy: %v", err)
	}
	if !strings.Contains(err.Error(), "password=[REDACTED]") {
		t.Fatalf("no redacted pod logs in %v", err)
	}
	if f.image("shop", "api") != "reg/api:2" {
		t.Fatalf("not put back: %s", f.image("shop", "api"))
	}
	if err := k.Rollback(ctx, s, "reg/api:1", "reg/api:1", nil); err != nil || f.image("shop", "api") != "reg/api:1" {
		t.Fatalf("rollback: %v %s", err, f.image("shop", "api"))
	}
	if err := k.Restart(ctx, s); err != nil {
		t.Fatalf("restart: %v", err)
	}
}

func TestKubernetesCreatesAMissingDeployment(t *testing.T) {
	f, k := kubeFixture(t)
	ctx := context.Background()

	res, err := k.Deploy(ctx, kubeService("web"), DeployRequest{Image: "reg/web:1", Port: 3000})
	if err != nil || !res.Created || f.image("shop", "web") != "reg/web:1" || !f.services["shop/web"] {
		t.Fatalf("create: %+v %v %v", res, err, f.services)
	}
	// One that never starts is removed again, with its reason.
	_, err = k.Deploy(ctx, kubeService("broken"), DeployRequest{Image: "reg/broken:bad", Port: 3000})
	if err == nil || !strings.Contains(err.Error(), "it was removed") {
		t.Fatalf("broken create: %v", err)
	}
	f.mu.Lock()
	_, kept := f.deployments["shop/broken"]
	f.mu.Unlock()
	if kept {
		t.Fatal("a failed new deployment was kept")
	}
}
