package runtime

import (
	"bufio"
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/config"
	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/inventory"
	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/redact"
)

// Kubernetes reads and acts on Deployments through the cluster's API, from inside the cluster
// (the agent's ServiceAccount), in plain REST: no client library, so the binary stays small.
// An image "id" here is its reference: the registry, not the node, holds the image.
type Kubernetes struct {
	// API is the API server's base URL; Token returns the bearer token (re-read each call:
	// projected tokens rotate).
	API   string
	Token func() (string, error)
	HTTP  *http.Client
	// Rollout is how long a rollout may take before it counts as failed; Poll how often it is
	// checked (default 2 s).
	Rollout time.Duration
	Poll    time.Duration
}

const serviceAccount = "/var/run/secrets/kubernetes.io/serviceaccount"

// NewKubernetesInCluster is the client of the cluster the agent runs in, or an error outside one.
func NewKubernetesInCluster() (*Kubernetes, error) {
	host, port := os.Getenv("KUBERNETES_SERVICE_HOST"), os.Getenv("KUBERNETES_SERVICE_PORT")
	if host == "" || port == "" {
		return nil, errors.New("not running inside a Kubernetes cluster")
	}
	ca, err := os.ReadFile(serviceAccount + "/ca.crt")
	if err != nil {
		return nil, fmt.Errorf("reading the cluster CA: %w", err)
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(ca) {
		return nil, errors.New("the cluster CA is not a PEM certificate")
	}
	return &Kubernetes{
		API: "https://" + net.JoinHostPort(host, port),
		Token: func() (string, error) {
			b, err := os.ReadFile(serviceAccount + "/token")
			return strings.TrimSpace(string(b)), err
		},
		HTTP: &http.Client{
			Timeout:   30 * time.Second,
			Transport: &http.Transport{TLSClientConfig: &tls.Config{RootCAs: pool, MinVersion: tls.VersionTLS12}},
		},
		Rollout: 5 * time.Minute,
	}, nil
}

type kubeError struct {
	status int
	msg    string
}

func (e kubeError) Error() string { return fmt.Sprintf("Kubernetes answered %d: %s", e.status, e.msg) }

func isNotFound(err error) bool {
	var ke kubeError
	return errors.As(err, &ke) && ke.status == http.StatusNotFound
}

func (k *Kubernetes) do(ctx context.Context, method, path, contentType string, body any) ([]byte, error) {
	var reader io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return nil, err
		}
		reader = bytes.NewReader(b)
	}
	req, err := http.NewRequestWithContext(ctx, method, k.API+path, reader)
	if err != nil {
		return nil, err
	}
	token, err := k.Token()
	if err != nil {
		return nil, fmt.Errorf("reading the ServiceAccount token: %w", err)
	}
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Accept", "application/json")
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}
	resp, err := k.HTTP.Do(req)
	if err != nil {
		return nil, fmt.Errorf("cannot reach the Kubernetes API: %w", err)
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	if err != nil {
		return nil, err
	}
	if resp.StatusCode >= 300 {
		var status struct {
			Message string `json:"message"`
		}
		msg := strings.TrimSpace(string(data))
		if json.Unmarshal(data, &status) == nil && status.Message != "" {
			msg = status.Message
		}
		return nil, kubeError{resp.StatusCode, msg}
	}
	return data, nil
}

func (k *Kubernetes) get(ctx context.Context, path string, out any) error {
	data, err := k.do(ctx, http.MethodGet, path, "", nil)
	if err != nil {
		return err
	}
	return json.Unmarshal(data, out)
}

// --- the objects read ----------------------------------------------------------------------

type kubeContainer struct {
	Name  string `json:"name"`
	Image string `json:"image"`
	Env   []struct {
		Name  string `json:"name"`
		Value string `json:"value"`
	} `json:"env"`
	Ports []struct {
		ContainerPort int    `json:"containerPort"`
		Protocol      string `json:"protocol"`
	} `json:"ports"`
}

type kubeDeployment struct {
	Metadata struct {
		Name       string            `json:"name"`
		Generation int64             `json:"generation"`
		UID        string            `json:"uid"`
		Labels     map[string]string `json:"labels"`
	} `json:"metadata"`
	Spec struct {
		Replicas *int `json:"replicas"`
		Selector struct {
			MatchLabels map[string]string `json:"matchLabels"`
		} `json:"selector"`
		Template kubeTemplate `json:"template"`
	} `json:"spec"`
	Status struct {
		ObservedGeneration int64 `json:"observedGeneration"`
		Replicas           int   `json:"replicas"`
		UpdatedReplicas    int   `json:"updatedReplicas"`
		ReadyReplicas      int   `json:"readyReplicas"`
		AvailableReplicas  int   `json:"availableReplicas"`
	} `json:"status"`
}

func (d kubeDeployment) wanted() int {
	if d.Spec.Replicas == nil {
		return 1
	}
	return *d.Spec.Replicas
}

type kubeTemplate struct {
	Metadata struct {
		Annotations map[string]string `json:"annotations"`
	} `json:"metadata"`
	Spec struct {
		Containers []kubeContainer `json:"containers"`
	} `json:"spec"`
}

// same: the parts of a pod template a deploy, rollback, or restart changes are equal.
func (t kubeTemplate) same(o kubeTemplate) bool {
	if len(t.Spec.Containers) != len(o.Spec.Containers) || len(t.Metadata.Annotations) != len(o.Metadata.Annotations) {
		return false
	}
	for k, v := range t.Metadata.Annotations {
		if o.Metadata.Annotations[k] != v {
			return false
		}
	}
	for i, c := range t.Spec.Containers {
		oc := o.Spec.Containers[i]
		if c.Name != oc.Name || c.Image != oc.Image || len(c.Env) != len(oc.Env) {
			return false
		}
		for j, e := range c.Env {
			if oc.Env[j] != e {
				return false
			}
		}
	}
	return true
}

type kubePod struct {
	Metadata struct {
		Name string `json:"name"`
	} `json:"metadata"`
	Status struct {
		Phase             string `json:"phase"`
		ContainerStatuses []struct {
			Name         string `json:"name"`
			Ready        bool   `json:"ready"`
			RestartCount int    `json:"restartCount"`
			ImageID      string `json:"imageID"`
			State        struct {
				Running *struct {
					StartedAt time.Time `json:"startedAt"`
				} `json:"running"`
				Waiting *struct {
					Reason  string `json:"reason"`
					Message string `json:"message"`
				} `json:"waiting"`
			} `json:"state"`
			LastState struct {
				Terminated *struct {
					ExitCode int    `json:"exitCode"`
					Reason   string `json:"reason"`
				} `json:"terminated"`
			} `json:"lastState"`
		} `json:"containerStatuses"`
	} `json:"status"`
}

func kubeRef(s config.Service) (*config.KubernetesRef, error) {
	if s.Kubernetes == nil {
		return nil, errors.New("not a kubernetes service")
	}
	return s.Kubernetes, nil
}

func deploymentPath(k *config.KubernetesRef) string {
	return "/apis/apps/v1/namespaces/" + url.PathEscape(k.Namespace) + "/deployments/" + url.PathEscape(k.Deployment)
}

func (k *Kubernetes) deployment(ctx context.Context, ref *config.KubernetesRef) (kubeDeployment, error) {
	var d kubeDeployment
	err := k.get(ctx, deploymentPath(ref), &d)
	if isNotFound(err) {
		return d, fmt.Errorf("no deployment %s in namespace %s", ref.Deployment, ref.Namespace)
	}
	return d, err
}

// container is the app's container: the one named, else the first.
func (d kubeDeployment) container(name string) (kubeContainer, int, error) {
	cs := d.Spec.Template.Spec.Containers
	for i, c := range cs {
		if name == "" || c.Name == name {
			return c, i, nil
		}
	}
	return kubeContainer{}, -1, fmt.Errorf("deployment %s has no container %q", d.Metadata.Name, name)
}

func selector(labels map[string]string) string {
	keys := make([]string, 0, len(labels))
	for k := range labels {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	parts := make([]string, len(keys))
	for i, k := range keys {
		parts[i] = k + "=" + labels[k]
	}
	return strings.Join(parts, ",")
}

func (k *Kubernetes) pods(ctx context.Context, ref *config.KubernetesRef, d kubeDeployment) ([]kubePod, error) {
	var list struct {
		Items []kubePod `json:"items"`
	}
	q := url.Values{"labelSelector": {selector(d.Spec.Selector.MatchLabels)}}
	err := k.get(ctx, "/api/v1/namespaces/"+url.PathEscape(ref.Namespace)+"/pods?"+q.Encode(), &list)
	return list.Items, err
}

// previousImage is the app container's image in the ReplicaSet before the current one.
func (k *Kubernetes) previousImage(ctx context.Context, ref *config.KubernetesRef, d kubeDeployment, container, current string) string {
	var list struct {
		Items []struct {
			Metadata struct {
				Annotations     map[string]string `json:"annotations"`
				OwnerReferences []struct {
					UID string `json:"uid"`
				} `json:"ownerReferences"`
			} `json:"metadata"`
			Spec struct {
				Template struct {
					Spec struct {
						Containers []kubeContainer `json:"containers"`
					} `json:"spec"`
				} `json:"template"`
			} `json:"spec"`
		} `json:"items"`
	}
	q := url.Values{"labelSelector": {selector(d.Spec.Selector.MatchLabels)}}
	if k.get(ctx, "/apis/apps/v1/namespaces/"+url.PathEscape(ref.Namespace)+"/replicasets?"+q.Encode(), &list) != nil {
		return ""
	}
	type rev struct {
		n     int
		image string
	}
	var revs []rev
	for _, rs := range list.Items {
		owned := false
		for _, o := range rs.Metadata.OwnerReferences {
			owned = owned || o.UID == d.Metadata.UID
		}
		n, err := strconv.Atoi(rs.Metadata.Annotations["deployment.kubernetes.io/revision"])
		if !owned || err != nil {
			continue
		}
		for _, c := range rs.Spec.Template.Spec.Containers {
			if container == "" || c.Name == container {
				revs = append(revs, rev{n, c.Image})
				break
			}
		}
	}
	sort.Slice(revs, func(i, j int) bool { return revs[i].n > revs[j].n })
	for _, r := range revs {
		if r.image != current {
			return r.image
		}
	}
	return ""
}

// --- Runtime -------------------------------------------------------------------------------

func (k *Kubernetes) Inspect(ctx context.Context, s config.Service) inventory.Service {
	ref, err := kubeRef(s)
	if err != nil {
		return inventory.Service{Problem: err.Error()}
	}
	d, err := k.deployment(ctx, ref)
	if err != nil {
		return inventory.Service{Problem: err.Error()}
	}
	c, _, err := d.container(ref.Container)
	if err != nil {
		return inventory.Service{Problem: err.Error()}
	}
	out := inventory.Service{Found: true, Image: c.Image}
	for _, p := range c.Ports {
		proto := strings.ToLower(p.Protocol)
		if proto == "" {
			proto = "tcp"
		}
		out.Ports = append(out.Ports, fmt.Sprintf("%d/%s", p.ContainerPort, proto))
	}
	return out
}

func (k *Kubernetes) State(ctx context.Context, s config.Service) State {
	st := State{Name: s.Name}
	ref, err := kubeRef(s)
	if err != nil {
		st.Problem = err.Error()
		return st
	}
	d, err := k.deployment(ctx, ref)
	if err != nil {
		st.State, st.Problem = "missing", err.Error()
		return st
	}
	c, _, err := d.container(ref.Container)
	if err != nil {
		st.State, st.Problem = "missing", err.Error()
		return st
	}
	st.Found, st.Image, st.ImageID = true, c.Image, c.Image
	switch {
	case d.wanted() == 0:
		st.State = "scaled to zero"
	case d.Status.AvailableReplicas == 0:
		st.State = "unavailable"
	default:
		st.State = "running"
	}
	if pods, err := k.pods(ctx, ref, d); err == nil {
		for _, p := range pods {
			for _, cs := range p.Status.ContainerStatuses {
				if cs.Name != c.Name {
					continue
				}
				st.Restarts += cs.RestartCount
				if t := cs.LastState.Terminated; t != nil {
					code := t.ExitCode
					st.ExitCode = &code
					st.OOMKilled = st.OOMKilled || t.Reason == "OOMKilled"
				}
				if r := cs.State.Running; r != nil && (st.StartedAt == nil || r.StartedAt.After(*st.StartedAt)) {
					at := r.StartedAt
					st.StartedAt = &at
				}
				if _, digest, ok := strings.Cut(cs.ImageID, "@"); ok {
					st.ImageDigest = digest
				}
			}
		}
	}
	if prev := k.previousImage(ctx, ref, d, c.Name, c.Image); prev != "" {
		st.PreviousImage, st.PreviousImageID = prev, prev
	}
	return st
}

// Logs are the app container's log lines since `since` across the Deployment's pods (at most
// 500 per pod, three pods).
func (k *Kubernetes) Logs(ctx context.Context, s config.Service, since time.Time) ([]LogLine, error) {
	ref, err := kubeRef(s)
	if err != nil {
		return nil, err
	}
	d, err := k.deployment(ctx, ref)
	if err != nil {
		return nil, err
	}
	c, _, err := d.container(ref.Container)
	if err != nil {
		return nil, err
	}
	pods, err := k.pods(ctx, ref, d)
	if err != nil {
		return nil, err
	}
	var out []LogLine
	for i, p := range pods {
		if i == 3 {
			break
		}
		q := url.Values{
			"container": {c.Name}, "timestamps": {"true"}, "tailLines": {"500"},
			"sinceTime": {since.UTC().Format(time.RFC3339)},
		}
		data, err := k.do(ctx, http.MethodGet, "/api/v1/namespaces/"+url.PathEscape(ref.Namespace)+
			"/pods/"+url.PathEscape(p.Metadata.Name)+"/log?"+q.Encode(), "", nil)
		if err != nil {
			continue // a pod that is starting has no log yet
		}
		sc := bufio.NewScanner(bytes.NewReader(data))
		sc.Buffer(make([]byte, 64<<10), 1<<20)
		for sc.Scan() {
			stamp, text, ok := strings.Cut(sc.Text(), " ")
			if !ok {
				continue
			}
			at, err := time.Parse(time.RFC3339Nano, stamp)
			if err != nil || !at.After(since) {
				continue
			}
			out = append(out, LogLine{At: at, Text: text})
		}
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].At.Before(out[j].At) })
	return out, nil
}

const strategicMerge = "application/strategic-merge-patch+json"

// Restart is `kubectl rollout restart`: a new pod template annotation, then the rollout.
func (k *Kubernetes) Restart(ctx context.Context, s config.Service) error {
	ref, err := kubeRef(s)
	if err != nil {
		return err
	}
	patch := map[string]any{"spec": map[string]any{"template": map[string]any{"metadata": map[string]any{
		"annotations": map[string]string{"kubectl.kubernetes.io/restartedAt": time.Now().UTC().Format(time.RFC3339)},
	}}}}
	if _, err := k.do(ctx, http.MethodPatch, deploymentPath(ref), strategicMerge, patch); err != nil {
		return fmt.Errorf("restarting: %w", err)
	}
	return k.waitRollout(ctx, ref, nil)
}

// HasImage: a registry image can always be pulled again.
func (k *Kubernetes) HasImage(_ context.Context, id string) bool { return id != "" }

// Rollback sets the app container back to an earlier image and waits for the rollout; if it
// does not become healthy, the image before the rollback is put back.
func (k *Kubernetes) Rollback(ctx context.Context, s config.Service, image, ref string, healthy func(context.Context) error) error {
	target := ref
	if target == "" {
		target = image
	}
	kref, err := kubeRef(s)
	if err != nil {
		return err
	}
	d, err := k.deployment(ctx, kref)
	if err != nil {
		return err
	}
	c, _, err := d.container(kref.Container)
	if err != nil {
		return err
	}
	if err := k.setImage(ctx, kref, c.Name, target); err != nil {
		return err
	}
	if err := k.waitRollout(ctx, kref, healthy); err != nil {
		if berr := k.setImage(ctx, kref, c.Name, c.Image); berr == nil {
			_ = k.waitRollout(ctx, kref, nil)
		}
		return fmt.Errorf("%w; the image before the rollback was put back", err)
	}
	return nil
}

func (k *Kubernetes) setImage(ctx context.Context, ref *config.KubernetesRef, container, image string) error {
	patch := map[string]any{"spec": map[string]any{"template": map[string]any{"spec": map[string]any{
		"containers": []map[string]any{{"name": container, "image": image}},
	}}}}
	if _, err := k.do(ctx, http.MethodPatch, deploymentPath(ref), strategicMerge, patch); err != nil {
		return fmt.Errorf("setting the image: %w", err)
	}
	return nil
}

// waitRollout waits until the Deployment runs its current template on every replica, steadily
// (stableChecks checks in a row with no new restarts: a crashing container with no readiness
// probe looks available between crashes), then for `healthy`. A container that cannot start
// (crash loop, image pull failure) or keeps restarting fails at once.
func (k *Kubernetes) waitRollout(ctx context.Context, ref *config.KubernetesRef, healthy func(context.Context) error) error {
	limit, poll := k.Rollout, k.Poll
	if limit == 0 {
		limit = 5 * time.Minute
	}
	if poll == 0 {
		poll = 2 * time.Second
	}
	deadline := time.Now().Add(limit)
	last := errors.New("the rollout did not finish in time")
	baseline, steady := -1, 0
	for time.Now().Before(deadline) {
		d, err := k.deployment(ctx, ref)
		if err != nil {
			return err
		}
		pods, err := k.pods(ctx, ref, d)
		if err != nil {
			return err
		}
		restarts := 0
		for _, p := range pods {
			for _, cs := range p.Status.ContainerStatuses {
				if w := cs.State.Waiting; w != nil && stuck(w.Reason) {
					return fmt.Errorf("pod %s cannot start: %s %s", p.Metadata.Name, w.Reason, w.Message)
				}
				restarts += cs.RestartCount
			}
		}
		if baseline < 0 {
			baseline = restarts
		} else if restarts > baseline {
			return fmt.Errorf("a new pod keeps restarting (%d restarts)", restarts-baseline)
		}
		done := false
		if d.Status.ObservedGeneration >= d.Metadata.Generation {
			// The Deployment's own counts can look finished for a moment while the old pod
			// is the available one; the ReplicaSets say what really runs.
			if done, err = k.rolledOut(ctx, ref, d); err != nil {
				return err
			}
		}
		if !done {
			steady = 0
		} else if steady++; steady >= stableChecks {
			if healthy == nil {
				return nil
			}
			if last = healthy(ctx); last == nil {
				return nil
			}
			last = fmt.Errorf("not healthy (%v)", last)
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(poll):
		}
	}
	return last
}

type kubeReplicaSet struct {
	Metadata struct {
		Annotations     map[string]string `json:"annotations"`
		OwnerReferences []struct {
			UID string `json:"uid"`
		} `json:"ownerReferences"`
	} `json:"metadata"`
	Spec struct {
		Template kubeTemplate `json:"template"`
	} `json:"spec"`
	Status struct {
		Replicas          int `json:"replicas"`
		AvailableReplicas int `json:"availableReplicas"`
	} `json:"status"`
}

// rolledOut: the newest ReplicaSet of the Deployment runs its current pod template (a reused
// older ReplicaSet gets its new revision a moment after the change), has every wanted pod
// available, and every older ReplicaSet has none left.
func (k *Kubernetes) rolledOut(ctx context.Context, ref *config.KubernetesRef, d kubeDeployment) (bool, error) {
	var list struct {
		Items []kubeReplicaSet `json:"items"`
	}
	q := url.Values{"labelSelector": {selector(d.Spec.Selector.MatchLabels)}}
	if err := k.get(ctx, "/apis/apps/v1/namespaces/"+url.PathEscape(ref.Namespace)+"/replicasets?"+q.Encode(), &list); err != nil {
		return false, err
	}
	newest, revision := -1, -1
	for i, rs := range list.Items {
		owned := false
		for _, o := range rs.Metadata.OwnerReferences {
			owned = owned || o.UID == d.Metadata.UID
		}
		n, err := strconv.Atoi(rs.Metadata.Annotations["deployment.kubernetes.io/revision"])
		if owned && err == nil && n > revision {
			newest, revision = i, n
		}
	}
	if newest < 0 || !list.Items[newest].Spec.Template.same(d.Spec.Template) {
		return false, nil
	}
	for i, rs := range list.Items {
		owned := false
		for _, o := range rs.Metadata.OwnerReferences {
			owned = owned || o.UID == d.Metadata.UID
		}
		if owned && i != newest && rs.Status.Replicas > 0 {
			return false, nil
		}
	}
	return list.Items[newest].Status.AvailableReplicas >= d.wanted() && d.Status.AvailableReplicas >= d.wanted(), nil
}

func stuck(reason string) bool {
	switch reason {
	case "CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "InvalidImageName", "CreateContainerConfigError":
		return true
	}
	return false
}

// Deploy puts the image in place: the app container's image and env values set on the
// Deployment (with a pull secret made from the one-time registry login), then the rollout and
// health; on failure the pod template before the deploy is put back exactly. A missing
// Deployment is created (one replica, the app's port, a ClusterIP Service of the same name).
func (k *Kubernetes) Deploy(ctx context.Context, s config.Service, req DeployRequest) (DeployResult, error) {
	ref, err := kubeRef(s)
	if err != nil {
		return DeployResult{}, err
	}
	pullSecret, err := k.pullSecret(ctx, ref, req.Login)
	if err != nil {
		return DeployResult{}, err
	}
	var raw struct {
		Spec struct {
			Template json.RawMessage `json:"template"`
		} `json:"spec"`
	}
	data, err := k.do(ctx, http.MethodGet, deploymentPath(ref), "", nil)
	if isNotFound(err) {
		return DeployResult{Created: true}, k.create(ctx, ref, req, pullSecret)
	}
	if err != nil {
		return DeployResult{}, err
	}
	if err := json.Unmarshal(data, &raw); err != nil {
		return DeployResult{}, err
	}
	var d kubeDeployment
	if err := json.Unmarshal(data, &d); err != nil {
		return DeployResult{}, err
	}
	c, _, err := d.container(ref.Container)
	if err != nil {
		return DeployResult{}, err
	}
	container := map[string]any{"name": c.Name, "image": req.Image}
	if env := kubeEnv(req.Env); len(env) > 0 {
		container["env"] = env // merged by name: other values stay
	}
	podSpec := map[string]any{"containers": []map[string]any{container}}
	if pullSecret != "" {
		podSpec["imagePullSecrets"] = []map[string]string{{"name": pullSecret}}
	}
	patch := map[string]any{"spec": map[string]any{"template": map[string]any{"spec": podSpec}}}
	if _, err := k.do(ctx, http.MethodPatch, deploymentPath(ref), strategicMerge, patch); err != nil {
		return DeployResult{}, fmt.Errorf("updating the deployment: %w", err)
	}
	if err := k.waitRollout(ctx, ref, req.Healthy); err != nil {
		err = fmt.Errorf("%w%s", err, k.lastPodLines(ctx, ref))
		back := map[string]any{"spec": map[string]any{"template": raw.Spec.Template}}
		if _, berr := k.do(ctx, http.MethodPatch, deploymentPath(ref), "application/merge-patch+json", back); berr != nil {
			return DeployResult{}, fmt.Errorf("%w; putting the previous version back failed too: %v", err, berr)
		}
		if berr := k.waitRollout(ctx, ref, nil); berr != nil {
			return DeployResult{}, fmt.Errorf("%w; the previous version was put back but is not running yet: %v", err, berr)
		}
		return DeployResult{}, fmt.Errorf("%w; the previous version was put back", err)
	}
	return DeployResult{}, nil
}

func kubeEnv(env map[string]string) []map[string]string {
	names := make([]string, 0, len(env))
	for n := range env {
		names = append(names, n)
	}
	sort.Strings(names)
	out := make([]map[string]string, len(names))
	for i, n := range names {
		out[i] = map[string]string{"name": n, "value": env[n]}
	}
	return out
}

// pullSecret writes the registry login as a docker-registry Secret in the service's namespace
// (created or replaced), and returns its name ("" without a login).
func (k *Kubernetes) pullSecret(ctx context.Context, ref *config.KubernetesRef, login *RegistryLogin) (string, error) {
	if login == nil || login.Server == "" {
		return "", nil
	}
	name := "groundcrew-pull-" + ref.Deployment
	if len(name) > 253 {
		name = name[:253]
	}
	auth := base64.StdEncoding.EncodeToString([]byte(login.Username + ":" + login.Password))
	cfg, _ := json.Marshal(map[string]any{"auths": map[string]any{login.Server: map[string]string{
		"username": login.Username, "password": login.Password, "auth": auth,
	}}})
	secret := map[string]any{
		"apiVersion": "v1", "kind": "Secret", "type": "kubernetes.io/dockerconfigjson",
		"metadata": map[string]any{"name": name, "labels": map[string]string{"app.kubernetes.io/managed-by": "groundcrew"}},
		"data":     map[string]string{".dockerconfigjson": base64.StdEncoding.EncodeToString(cfg)},
	}
	base := "/api/v1/namespaces/" + url.PathEscape(ref.Namespace) + "/secrets"
	_, err := k.do(ctx, http.MethodPost, base, "application/json", secret)
	var ke kubeError
	if errors.As(err, &ke) && ke.status == http.StatusConflict {
		_, err = k.do(ctx, http.MethodPut, base+"/"+url.PathEscape(name), "application/json", secret)
	}
	if err != nil {
		return "", fmt.Errorf("writing the image pull secret: %w", err)
	}
	return name, nil
}

func (k *Kubernetes) create(ctx context.Context, ref *config.KubernetesRef, req DeployRequest, pullSecret string) error {
	name := ref.Container
	if name == "" {
		name = ref.Deployment
	}
	labels := map[string]string{"app.kubernetes.io/name": ref.Deployment, "app.kubernetes.io/managed-by": "groundcrew"}
	container := map[string]any{"name": name, "image": req.Image, "env": kubeEnv(req.Env)}
	if req.Port > 0 {
		container["ports"] = []map[string]any{{"containerPort": req.Port, "name": "app"}}
	}
	podSpec := map[string]any{"containers": []any{container}}
	if pullSecret != "" {
		podSpec["imagePullSecrets"] = []map[string]string{{"name": pullSecret}}
	}
	ns := url.PathEscape(ref.Namespace)
	deployment := map[string]any{
		"apiVersion": "apps/v1", "kind": "Deployment",
		"metadata": map[string]any{"name": ref.Deployment, "labels": labels},
		"spec": map[string]any{
			"replicas": 1,
			"selector": map[string]any{"matchLabels": map[string]string{"app.kubernetes.io/name": ref.Deployment}},
			"template": map[string]any{"metadata": map[string]any{"labels": labels}, "spec": podSpec},
		},
	}
	if _, err := k.do(ctx, http.MethodPost, "/apis/apps/v1/namespaces/"+ns+"/deployments", "application/json", deployment); err != nil {
		return fmt.Errorf("creating the deployment: %w", err)
	}
	if req.Port > 0 {
		service := map[string]any{
			"apiVersion": "v1", "kind": "Service",
			"metadata": map[string]any{"name": ref.Deployment, "labels": labels},
			"spec": map[string]any{
				"selector": map[string]string{"app.kubernetes.io/name": ref.Deployment},
				"ports":    []map[string]any{{"port": req.Port, "targetPort": "app", "name": "app"}},
			},
		}
		_, err := k.do(ctx, http.MethodPost, "/api/v1/namespaces/"+ns+"/services", "application/json", service)
		var ke kubeError
		if err != nil && !(errors.As(err, &ke) && ke.status == http.StatusConflict) {
			return fmt.Errorf("creating the service: %w", err)
		}
	}
	healthy := req.Healthy
	if healthy == nil && req.HealthPath != "" && req.Port > 0 {
		u := fmt.Sprintf("http://%s.%s.svc:%d%s", ref.Deployment, ref.Namespace, req.Port, req.HealthPath)
		healthy = func(ctx context.Context) error { return HealthCheck(ctx, u, 5*time.Second) }
	}
	if err := k.waitRollout(ctx, ref, healthy); err != nil {
		lines := k.lastPodLines(ctx, ref)
		_, _ = k.do(ctx, http.MethodDelete, deploymentPath(ref), "", nil)
		return fmt.Errorf("the new deployment is not healthy (%w)%s; it was removed", err, lines)
	}
	return nil
}

// lastPodLines is the new pods' last log lines, for the error (they go with the deployment).
func (k *Kubernetes) lastPodLines(ctx context.Context, ref *config.KubernetesRef) string {
	lines, err := k.Logs(ctx, config.Service{Kubernetes: ref}, time.Now().Add(-k.Rollout-time.Minute))
	if err != nil || len(lines) == 0 {
		return ""
	}
	if len(lines) > FailedLogLines {
		lines = lines[len(lines)-FailedLogLines:]
	}
	var b strings.Builder
	b.WriteString("; its last log lines:")
	for _, l := range lines {
		b.WriteString("\n" + redact.Line(strings.TrimSpace(l.Text)))
	}
	return b.String()
}
