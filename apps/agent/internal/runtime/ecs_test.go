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

// fakeAWS is ECS, CloudWatch Logs, and Secrets Manager for one service: task definition
// revisions, the service's current one, and secrets. A revision whose image contains "bad"
// gets failing tasks, like ECS with a crashing container.
type fakeAWS struct {
	mu        sync.Mutex
	revisions []map[string]any // index = revision - 1
	current   int              // revision the service runs
	secrets   map[string]string
	calls     []string
}

type staticCreds struct{}

func (staticCreds) get(context.Context) (awsCredentials, error) { return exampleCreds, nil }

func newFakeAWS(image string) *fakeAWS {
	f := &fakeAWS{secrets: map[string]string{}}
	f.revisions = append(f.revisions, map[string]any{
		"family": "api", "taskDefinitionArn": "arn:aws:ecs:us-east-2:1:task-definition/api:1", "revision": 1,
		"status": "ACTIVE", "networkMode": "awsvpc", "executionRoleArn": "arn:aws:iam::1:role/exec",
		"containerDefinitions": []any{map[string]any{
			"name": "api", "image": image, "essential": true,
			"environment":  []any{map[string]any{"name": "KEEP", "value": "1"}, map[string]any{"name": "GONE", "value": "x"}},
			"portMappings": []any{map[string]any{"containerPort": float64(8000), "protocol": "tcp"}},
			"logConfiguration": map[string]any{"logDriver": "awslogs", "options": map[string]any{
				"awslogs-group": "/ecs/api", "awslogs-stream-prefix": "ecs"}},
		}},
	})
	f.current = 1
	return f
}

func (f *fakeAWS) arn(rev int) string {
	return fmt.Sprintf("arn:aws:ecs:us-east-2:1:task-definition/api:%d", rev)
}

func (f *fakeAWS) image(rev int) string {
	c := f.revisions[rev-1]["containerDefinitions"].([]any)[0].(map[string]any)
	return c["image"].(string)
}

func (f *fakeAWS) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if !strings.HasPrefix(r.Header.Get("Authorization"), "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/") {
		w.WriteHeader(403)
		return
	}
	target := r.Header.Get("X-Amz-Target")
	op := target[strings.LastIndex(target, ".")+1:]
	f.calls = append(f.calls, op)
	var in map[string]any
	b, _ := io.ReadAll(r.Body)
	_ = json.Unmarshal(b, &in)
	reply := func(v any) { _ = json.NewEncoder(w).Encode(v) }
	fail := func(kind, msg string) {
		w.WriteHeader(400)
		reply(map[string]string{"__type": "com.amazonaws#" + kind, "message": msg})
	}
	switch op {
	case "DescribeServices":
		if in["services"].([]any)[0] != "api" {
			reply(map[string]any{"services": []any{}, "failures": []any{map[string]any{"reason": "MISSING"}}})
			return
		}
		dep := map[string]any{"status": "PRIMARY", "taskDefinition": f.arn(f.current), "desiredCount": 1,
			"runningCount": 1, "failedTasks": 0, "rolloutState": "COMPLETED"}
		running := 1
		if strings.Contains(f.image(f.current), "bad") {
			dep["runningCount"], dep["failedTasks"], dep["rolloutState"] = 0, 2, "IN_PROGRESS"
			running = 0
		}
		reply(map[string]any{"services": []any{map[string]any{"serviceName": "api", "status": "ACTIVE",
			"desiredCount": 1, "runningCount": running, "taskDefinition": f.arn(f.current),
			"deployments": []any{dep}}}})
	case "DescribeTaskDefinition":
		ref := in["taskDefinition"].(string)
		var rev int
		fmt.Sscanf(ref[strings.LastIndex(ref, ":")+1:], "%d", &rev)
		if rev < 1 || rev > len(f.revisions) {
			fail("ClientException", "Unable to describe task definition.")
			return
		}
		reply(map[string]any{"taskDefinition": f.revisions[rev-1], "tags": []any{}})
	case "RegisterTaskDefinition":
		for _, k := range []string{"taskDefinitionArn", "revision", "status"} {
			if _, ok := in[k]; ok {
				fail("ClientException", "unexpected key "+k)
				return
			}
		}
		rev := len(f.revisions) + 1
		in["taskDefinitionArn"], in["revision"] = f.arn(rev), rev
		f.revisions = append(f.revisions, in)
		reply(map[string]any{"taskDefinition": in})
	case "UpdateService":
		ref := in["taskDefinition"].(string)
		fmt.Sscanf(ref[strings.LastIndex(ref, ":")+1:], "%d", &f.current)
		reply(map[string]any{})
	case "ListTasks":
		reply(map[string]any{"taskArns": []string{"arn:task/1"}})
	case "DescribeTasks":
		code := 3
		reply(map[string]any{"tasks": []any{map[string]any{"taskArn": "arn:task/1",
			"stoppedReason": "Essential container in task exited",
			"containers":    []any{map[string]any{"name": "api", "exitCode": code}}}}})
	case "FilterLogEvents":
		reply(map[string]any{"events": []any{
			map[string]any{"timestamp": time.Now().UnixMilli(), "message": "boom: no db token=abc123\n"}}})
	case "DescribeSecret":
		if _, ok := f.secrets[in["SecretId"].(string)]; !ok {
			fail("ResourceNotFoundException", "not found")
			return
		}
		reply(map[string]any{"ARN": "arn:secret:" + in["SecretId"].(string)})
	case "CreateSecret":
		f.secrets[in["Name"].(string)] = in["SecretString"].(string)
		reply(map[string]any{"ARN": "arn:secret:" + in["Name"].(string)})
	case "PutSecretValue":
		f.secrets[strings.TrimPrefix(in["SecretId"].(string), "arn:secret:")] = in["SecretString"].(string)
		reply(map[string]any{})
	default:
		fail("UnknownOperationException", op)
	}
}

func ecsFixture(t *testing.T, image string) (*fakeAWS, *ECS) {
	t.Helper()
	f := newFakeAWS(image)
	srv := httptest.NewServer(f)
	t.Cleanup(srv.Close)
	e := &ECS{
		aws: &awsClient{region: "us-east-2", creds: staticCreds{}, http: srv.Client(),
			endpoint: func(string) string { return srv.URL + "/" }},
		Rollout: 2 * time.Second, Poll: 5 * time.Millisecond,
	}
	return f, e
}

func ecsSvc(name string) config.Service {
	return config.Service{Name: name, Runtime: config.ECS, ECS: &config.ECSRef{Cluster: "checkout", Service: name}}
}

func TestECSReadsAService(t *testing.T) {
	_, e := ecsFixture(t, "reg/api:1")
	ctx := context.Background()
	st := e.State(ctx, ecsSvc("api"))
	if !st.Running() || st.Image != "reg/api:1" || *st.ExitCode != 3 {
		t.Fatalf("state %+v", st)
	}
	if inv := e.Inspect(ctx, ecsSvc("api")); !inv.Found || inv.Ports[0] != "8000/tcp" {
		t.Fatalf("inspect %+v", inv)
	}
	if lines, err := e.Logs(ctx, ecsSvc("api"), time.Now().Add(-time.Minute)); err != nil || len(lines) != 1 {
		t.Fatalf("logs %v %v", lines, err)
	}
	if missing := e.State(ctx, ecsSvc("nope")); missing.Found {
		t.Fatalf("missing %+v", missing)
	}
}

func TestECSDeploysAndPutsTheRevisionBackOnFailure(t *testing.T) {
	f, e := ecsFixture(t, "reg/api:1")
	ctx := context.Background()
	s := ecsSvc("api")
	login := &RegistryLogin{Server: "api.groundkrew.com", Username: "u", Password: "p1"}

	if _, err := e.Deploy(ctx, s, DeployRequest{Image: "reg/api:2", Env: map[string]string{"NEW": "x"}, Unset: []string{"GONE"}, Login: login}); err != nil {
		t.Fatalf("deploy: %v", err)
	}
	if f.current != 2 || f.image(2) != "reg/api:2" {
		t.Fatalf("not deployed: rev %d", f.current)
	}
	c := f.revisions[1]["containerDefinitions"].([]any)[0].(map[string]any)
	env, _ := json.Marshal(c["environment"])
	if !strings.Contains(string(env), `"KEEP"`) || !strings.Contains(string(env), `"NEW"`) || strings.Contains(string(env), `"GONE"`) {
		t.Fatalf("env %s", env)
	}
	if c["repositoryCredentials"].(map[string]any)["credentialsParameter"] != "arn:secret:groundcrew/pull/checkout/api" {
		t.Fatalf("pull credentials %v", c["repositoryCredentials"])
	}
	if st := e.State(ctx, s); st.PreviousImage != "reg/api:1" {
		t.Fatalf("previous %+v", st)
	}

	_, err := e.Deploy(ctx, s, DeployRequest{Image: "reg/api:bad"})
	if err == nil || !strings.Contains(err.Error(), "keep failing") || !strings.Contains(err.Error(), "previous revision was put back") ||
		!strings.Contains(err.Error(), "token=[REDACTED]") {
		t.Fatalf("bad deploy: %v", err)
	}
	if f.current != 2 {
		t.Fatalf("not put back: rev %d", f.current)
	}
	if err := e.Rollback(ctx, s, "reg/api:1", "reg/api:1", nil); err != nil || f.image(f.current) != "reg/api:1" {
		t.Fatalf("rollback: %v rev %d", err, f.current)
	}
	if err := e.Restart(ctx, s); err != nil {
		t.Fatalf("restart: %v", err)
	}
	// The pull login is renewed in place; ECR needs none.
	if renewed, err := e.RefreshPull(ctx, s, &RegistryLogin{Server: "api.groundkrew.com", Username: "u", Password: "p2"}); err != nil || !renewed ||
		!strings.Contains(f.secrets["groundcrew/pull/checkout/api"], "p2") {
		t.Fatalf("renew: %v %v %v", renewed, err, f.secrets)
	}
	if renewed, _ := e.RefreshPull(ctx, s, &RegistryLogin{Server: "123456789012.dkr.ecr.us-east-2.amazonaws.com"}); renewed {
		t.Fatal("renewed an ECR login")
	}
	if _, err := e.Deploy(ctx, ecsSvc("nope"), DeployRequest{Image: "x"}); err == nil || !strings.Contains(err.Error(), "create the service first") {
		t.Fatalf("missing service: %v", err)
	}
}
