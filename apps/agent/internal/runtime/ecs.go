package runtime

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"regexp"
	"slices"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/config"
	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/inventory"
	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/redact"
)

// ECS reads and acts on ECS services from inside the account, with the task role's credentials
// (docs/agent-plan.md "A5 plan"). A deploy or rollback registers a task definition revision and
// updates the service; on failure the revision before it is put back. Like Kubernetes, an image
// "id" is its reference.
type ECS struct {
	aws *awsClient
	// Rollout is how long a deployment may take before it counts as failed; Poll how often it
	// is checked.
	Rollout time.Duration
	Poll    time.Duration
}

// NewECSInTask is the ECS client of the task the agent runs in, or an error outside one.
func NewECSInTask() (*ECS, error) {
	region := os.Getenv("AWS_REGION")
	if region == "" {
		region = os.Getenv("AWS_DEFAULT_REGION")
	}
	if region == "" {
		return nil, errors.New("no AWS region: not running in an ECS task")
	}
	creds, err := newAWSCredentialSource()
	if err != nil {
		return nil, err
	}
	return &ECS{
		aws:     &awsClient{region: region, creds: creds, http: &http.Client{Timeout: 30 * time.Second}},
		Rollout: 10 * time.Minute,
	}, nil
}

const ecsTarget = "AmazonEC2ContainerServiceV20141113."

func (e *ECS) ecs(ctx context.Context, op string, in, out any) error {
	return e.aws.call(ctx, "ecs", ecsTarget+op, in, out)
}

type ecsDeployment struct {
	Status             string `json:"status"`
	TaskDefinition     string `json:"taskDefinition"`
	DesiredCount       int    `json:"desiredCount"`
	RunningCount       int    `json:"runningCount"`
	FailedTasks        int    `json:"failedTasks"`
	RolloutState       string `json:"rolloutState"`
	RolloutStateReason string `json:"rolloutStateReason"`
}

type ecsService struct {
	ServiceName    string          `json:"serviceName"`
	Status         string          `json:"status"`
	DesiredCount   int             `json:"desiredCount"`
	RunningCount   int             `json:"runningCount"`
	TaskDefinition string          `json:"taskDefinition"`
	Deployments    []ecsDeployment `json:"deployments"`
	Events         []struct {
		Message string `json:"message"`
	} `json:"events"`
}

func (s ecsService) primary() ecsDeployment {
	for _, d := range s.Deployments {
		if d.Status == "PRIMARY" {
			return d
		}
	}
	return ecsDeployment{}
}

func ecsRef(s config.Service) (*config.ECSRef, error) {
	if s.ECS == nil {
		return nil, errors.New("not an ecs service")
	}
	return s.ECS, nil
}

func (e *ECS) service(ctx context.Context, ref *config.ECSRef) (ecsService, error) {
	var out struct {
		Services []ecsService `json:"services"`
		Failures []struct {
			Reason string `json:"reason"`
		} `json:"failures"`
	}
	if err := e.ecs(ctx, "DescribeServices", map[string]any{"cluster": ref.Cluster, "services": []string{ref.Service}}, &out); err != nil {
		return ecsService{}, err
	}
	if len(out.Services) == 0 || out.Services[0].Status == "INACTIVE" {
		return ecsService{}, fmt.Errorf("no ECS service %s in cluster %s", ref.Service, ref.Cluster)
	}
	return out.Services[0], nil
}

// taskDefinition is a task definition as a map (so a new revision keeps every field it had) and
// its tags.
func (e *ECS) taskDefinition(ctx context.Context, arn string) (map[string]any, []any, error) {
	var out struct {
		TaskDefinition map[string]any `json:"taskDefinition"`
		Tags           []any          `json:"tags"`
	}
	err := e.ecs(ctx, "DescribeTaskDefinition", map[string]any{"taskDefinition": arn, "include": []string{"TAGS"}}, &out)
	return out.TaskDefinition, out.Tags, err
}

// ecsAppContainer is the container named, else the first essential one, else the first.
func ecsAppContainer(td map[string]any, name string) (map[string]any, error) {
	defs, _ := td["containerDefinitions"].([]any)
	var first map[string]any
	for _, d := range defs {
		c, _ := d.(map[string]any)
		if c == nil {
			continue
		}
		if name != "" && c["name"] == name {
			return c, nil
		}
		if first == nil && (c["essential"] == nil || c["essential"] == true) {
			first = c
		}
	}
	if name != "" || first == nil {
		return nil, fmt.Errorf("the task definition has no container %q", name)
	}
	return first, nil
}

func str(v any) string {
	s, _ := v.(string)
	return s
}

var revisionSuffix = regexp.MustCompile(`:(\d+)$`)

// previousImage is the app container's image in the revision before the current one.
func (e *ECS) previousImage(ctx context.Context, s ecsService, container, current string) string {
	for _, d := range s.Deployments {
		if d.Status != "PRIMARY" && d.TaskDefinition != "" {
			if td, _, err := e.taskDefinition(ctx, d.TaskDefinition); err == nil {
				if c, err := ecsAppContainer(td, container); err == nil && str(c["image"]) != current {
					return str(c["image"])
				}
			}
		}
	}
	m := revisionSuffix.FindStringSubmatch(s.TaskDefinition)
	if m == nil {
		return ""
	}
	n, _ := strconv.Atoi(m[1])
	for rev := n - 1; rev >= 1 && rev >= n-5; rev-- {
		td, _, err := e.taskDefinition(ctx, revisionSuffix.ReplaceAllString(s.TaskDefinition, ":"+strconv.Itoa(rev)))
		if err != nil {
			continue
		}
		if c, err := ecsAppContainer(td, container); err == nil && str(c["image"]) != current {
			return str(c["image"])
		}
	}
	return ""
}

// --- Runtime -------------------------------------------------------------------------------

func (e *ECS) Inspect(ctx context.Context, s config.Service) inventory.Service {
	ref, err := ecsRef(s)
	if err != nil {
		return inventory.Service{Problem: err.Error()}
	}
	svc, err := e.service(ctx, ref)
	if err != nil {
		return inventory.Service{Problem: err.Error()}
	}
	td, _, err := e.taskDefinition(ctx, svc.TaskDefinition)
	if err != nil {
		return inventory.Service{Problem: err.Error()}
	}
	c, err := ecsAppContainer(td, ref.Container)
	if err != nil {
		return inventory.Service{Problem: err.Error()}
	}
	out := inventory.Service{Found: true, Image: str(c["image"])}
	mappings, _ := c["portMappings"].([]any)
	for _, m := range mappings {
		pm, _ := m.(map[string]any)
		proto := strings.ToLower(str(pm["protocol"]))
		if proto == "" {
			proto = "tcp"
		}
		if port, ok := pm["containerPort"].(float64); ok {
			out.Ports = append(out.Ports, fmt.Sprintf("%d/%s", int(port), proto))
		}
	}
	return out
}

func (e *ECS) State(ctx context.Context, s config.Service) State {
	st := State{Name: s.Name}
	ref, err := ecsRef(s)
	if err != nil {
		st.Problem = err.Error()
		return st
	}
	svc, err := e.service(ctx, ref)
	if err != nil {
		st.State, st.Problem = "missing", err.Error()
		return st
	}
	td, _, err := e.taskDefinition(ctx, svc.TaskDefinition)
	if err != nil {
		st.State, st.Problem = "missing", err.Error()
		return st
	}
	c, err := ecsAppContainer(td, ref.Container)
	if err != nil {
		st.State, st.Problem = "missing", err.Error()
		return st
	}
	st.Found, st.Image, st.ImageID = true, str(c["image"]), str(c["image"])
	switch {
	case svc.DesiredCount == 0:
		st.State = "scaled to zero"
	case svc.RunningCount == 0:
		st.State = "unavailable"
	default:
		st.State = "running"
	}
	st.Restarts = svc.primary().FailedTasks
	if stopped, err := e.stoppedTasks(ctx, ref, 5); err == nil {
		for _, t := range stopped {
			for _, tc := range t.Containers {
				if tc.Name != str(c["name"]) {
					continue
				}
				if tc.ExitCode != nil && st.ExitCode == nil {
					code := *tc.ExitCode
					st.ExitCode = &code
				}
				st.OOMKilled = st.OOMKilled || strings.Contains(tc.Reason, "OutOfMemory")
			}
		}
	}
	if prev := e.previousImage(ctx, svc, str(c["name"]), st.Image); prev != "" {
		st.PreviousImage, st.PreviousImageID = prev, prev
	}
	return st
}

type ecsTask struct {
	TaskArn       string `json:"taskArn"`
	StoppedReason string `json:"stoppedReason"`
	Containers    []struct {
		Name     string `json:"name"`
		ExitCode *int   `json:"exitCode"`
		Reason   string `json:"reason"`
	} `json:"containers"`
}

func (e *ECS) stoppedTasks(ctx context.Context, ref *config.ECSRef, max int) ([]ecsTask, error) {
	var list struct {
		TaskArns []string `json:"taskArns"`
	}
	if err := e.ecs(ctx, "ListTasks", map[string]any{
		"cluster": ref.Cluster, "serviceName": ref.Service, "desiredStatus": "STOPPED", "maxResults": max,
	}, &list); err != nil || len(list.TaskArns) == 0 {
		return nil, err
	}
	var out struct {
		Tasks []ecsTask `json:"tasks"`
	}
	err := e.ecs(ctx, "DescribeTasks", map[string]any{"cluster": ref.Cluster, "tasks": list.TaskArns}, &out)
	return out.Tasks, err
}

// Logs are the app container's CloudWatch log lines since `since` (awslogs driver), at most 500.
func (e *ECS) Logs(ctx context.Context, s config.Service, since time.Time) ([]LogLine, error) {
	ref, err := ecsRef(s)
	if err != nil {
		return nil, err
	}
	svc, err := e.service(ctx, ref)
	if err != nil {
		return nil, err
	}
	td, _, err := e.taskDefinition(ctx, svc.TaskDefinition)
	if err != nil {
		return nil, err
	}
	c, err := ecsAppContainer(td, ref.Container)
	if err != nil {
		return nil, err
	}
	lc, _ := c["logConfiguration"].(map[string]any)
	opts, _ := lc["options"].(map[string]any)
	group := str(opts["awslogs-group"])
	if str(lc["logDriver"]) != "awslogs" || group == "" {
		return nil, errors.New("the container does not log to CloudWatch (awslogs)")
	}
	in := map[string]any{"logGroupName": group, "startTime": since.UnixMilli() + 1, "limit": 500}
	if prefix := str(opts["awslogs-stream-prefix"]); prefix != "" {
		in["logStreamNamePrefix"] = prefix + "/" + str(c["name"]) + "/"
	}
	var out struct {
		Events []struct {
			Timestamp int64  `json:"timestamp"`
			Message   string `json:"message"`
		} `json:"events"`
	}
	if err := e.aws.call(ctx, "logs", "Logs_20140328.FilterLogEvents", in, &out); err != nil {
		return nil, err
	}
	lines := make([]LogLine, 0, len(out.Events))
	for _, ev := range out.Events {
		lines = append(lines, LogLine{At: time.UnixMilli(ev.Timestamp), Text: strings.TrimRight(ev.Message, "\n")})
	}
	sort.SliceStable(lines, func(i, j int) bool { return lines[i].At.Before(lines[j].At) })
	return lines, nil
}

// Restart forces a new deployment of the same revision and waits for it.
func (e *ECS) Restart(ctx context.Context, s config.Service) error {
	ref, err := ecsRef(s)
	if err != nil {
		return err
	}
	svc, err := e.service(ctx, ref)
	if err != nil {
		return err
	}
	if err := e.update(ctx, ref, svc.TaskDefinition, true); err != nil {
		return err
	}
	return e.waitSteady(ctx, ref, svc.TaskDefinition, nil)
}

func (e *ECS) HasImage(_ context.Context, id string) bool { return id != "" }

func (e *ECS) update(ctx context.Context, ref *config.ECSRef, td string, force bool) error {
	in := map[string]any{"cluster": ref.Cluster, "service": ref.Service, "taskDefinition": td}
	if force {
		in["forceNewDeployment"] = true
	}
	if err := e.ecs(ctx, "UpdateService", in, nil); err != nil {
		return fmt.Errorf("updating the service: %w", err)
	}
	return nil
}

// register makes a new revision of td with `change` applied to the app container.
func (e *ECS) register(ctx context.Context, td map[string]any, tags []any, container string, change func(map[string]any)) (string, error) {
	next := map[string]any{}
	for k, v := range td {
		switch k {
		case "taskDefinitionArn", "revision", "status", "requiresAttributes", "compatibilities",
			"registeredAt", "registeredBy", "deregisteredAt":
		default:
			next[k] = v
		}
	}
	c, err := ecsAppContainer(next, container)
	if err != nil {
		return "", err
	}
	change(c)
	if len(tags) > 0 {
		next["tags"] = tags
	}
	var out struct {
		TaskDefinition struct {
			Arn string `json:"taskDefinitionArn"`
		} `json:"taskDefinition"`
	}
	if err := e.ecs(ctx, "RegisterTaskDefinition", next, &out); err != nil {
		return "", fmt.Errorf("registering the task definition: %w", err)
	}
	return out.TaskDefinition.Arn, nil
}

// waitSteady waits until the service's primary deployment runs `td` on every wanted task,
// steadily, then for `healthy`. A deployment ECS marks FAILED, or tasks that keep failing,
// fail at once with ECS's reason.
func (e *ECS) waitSteady(ctx context.Context, ref *config.ECSRef, td string, healthy func(context.Context) error) error {
	limit, poll := e.Rollout, e.Poll
	if limit == 0 {
		limit = 10 * time.Minute
	}
	if poll == 0 {
		poll = 5 * time.Second
	}
	deadline := time.Now().Add(limit)
	last := errors.New("the deployment did not finish in time")
	steady := 0
	for time.Now().Before(deadline) {
		svc, err := e.service(ctx, ref)
		if err != nil {
			return err
		}
		p := svc.primary()
		if p.TaskDefinition == td && p.RolloutState == "FAILED" {
			return fmt.Errorf("ECS stopped the deployment: %s", p.RolloutStateReason)
		}
		if p.TaskDefinition == td && p.FailedTasks > 0 {
			return fmt.Errorf("new tasks keep failing (%d failed)%s", p.FailedTasks, e.stopReasons(ctx, ref))
		}
		done := p.TaskDefinition == td && len(svc.Deployments) == 1 && p.RunningCount >= p.DesiredCount &&
			(p.RolloutState == "" || p.RolloutState == "COMPLETED")
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

// stopReasons is why the service's last tasks stopped, for an error message.
func (e *ECS) stopReasons(ctx context.Context, ref *config.ECSRef) string {
	tasks, err := e.stoppedTasks(ctx, ref, 3)
	if err != nil || len(tasks) == 0 {
		return ""
	}
	var reasons []string
	for _, t := range tasks {
		r := t.StoppedReason
		for _, c := range t.Containers {
			if c.Reason != "" {
				r += "; " + c.Name + ": " + c.Reason
			} else if c.ExitCode != nil {
				r += fmt.Sprintf("; %s exited with %d", c.Name, *c.ExitCode)
			}
		}
		reasons = append(reasons, redact.Line(r))
	}
	return ": " + strings.Join(reasons, " | ")
}

// switchTo puts `next` in place and waits; on failure `previous` goes back and is waited for.
func (e *ECS) switchTo(ctx context.Context, s config.Service, ref *config.ECSRef, previous, next string, healthy func(context.Context) error) error {
	if err := e.update(ctx, ref, next, false); err != nil {
		return err
	}
	err := e.waitSteady(ctx, ref, next, healthy)
	if err == nil {
		return nil
	}
	if lines := e.lastLines(ctx, s); lines != "" {
		err = fmt.Errorf("%w%s", err, lines)
	}
	if berr := e.update(ctx, ref, previous, false); berr != nil {
		return fmt.Errorf("%w; putting the previous revision back failed too: %v", err, berr)
	}
	if berr := e.waitSteady(ctx, ref, previous, nil); berr != nil {
		return fmt.Errorf("%w; the previous revision was put back but is not running yet: %v", err, berr)
	}
	return fmt.Errorf("%w; the previous revision was put back", err)
}

func (e *ECS) lastLines(ctx context.Context, s config.Service) string {
	lines, err := e.Logs(ctx, s, time.Now().Add(-e.Rollout-time.Minute))
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

// Rollback registers a revision of the current task definition with the earlier image and
// deploys it; on failure the current revision goes back.
func (e *ECS) Rollback(ctx context.Context, s config.Service, image, ref string, healthy func(context.Context) error) error {
	target := ref
	if target == "" {
		target = image
	}
	eref, err := ecsRef(s)
	if err != nil {
		return err
	}
	svc, err := e.service(ctx, eref)
	if err != nil {
		return err
	}
	td, tags, err := e.taskDefinition(ctx, svc.TaskDefinition)
	if err != nil {
		return err
	}
	next, err := e.register(ctx, td, tags, eref.Container, func(c map[string]any) { c["image"] = target })
	if err != nil {
		return err
	}
	return e.switchTo(ctx, s, eref, svc.TaskDefinition, next, healthy)
}

// Deploy registers a revision of the service's task definition with the new image and env
// values (and the pull credentials' secret for a private registry other than ECR) and deploys
// it; on failure the current revision goes back. The service must exist.
func (e *ECS) Deploy(ctx context.Context, s config.Service, req DeployRequest) (DeployResult, error) {
	ref, err := ecsRef(s)
	if err != nil {
		return DeployResult{}, err
	}
	svc, err := e.service(ctx, ref)
	if err != nil {
		return DeployResult{}, fmt.Errorf("%w (Groundcrew deploys to existing ECS services: create the service first)", err)
	}
	td, tags, err := e.taskDefinition(ctx, svc.TaskDefinition)
	if err != nil {
		return DeployResult{}, err
	}
	creds := ""
	if req.Login != nil && req.Login.Server != "" && !isECR(req.Login.Server) {
		if creds, err = e.pullSecret(ctx, ref, req.Login, true); err != nil {
			return DeployResult{}, err
		}
	}
	next, err := e.register(ctx, td, tags, ref.Container, func(c map[string]any) {
		c["image"] = req.Image
		c["environment"] = ecsEnv(c["environment"], req.Env, req.Unset)
		if creds != "" {
			c["repositoryCredentials"] = map[string]any{"credentialsParameter": creds}
		}
	})
	if err != nil {
		return DeployResult{}, err
	}
	return DeployResult{}, e.switchTo(ctx, s, ref, svc.TaskDefinition, next, req.Healthy)
}

// ecsEnv sets `env` on a task definition's environment list, keeping the other values.
func ecsEnv(list any, env map[string]string, unset []string) []any {
	out := []any{}
	have, _ := list.([]any)
	for _, item := range have {
		m, _ := item.(map[string]any)
		if _, replaced := env[str(m["name"])]; !replaced && !slices.Contains(unset, str(m["name"])) {
			out = append(out, item)
		}
	}
	names := make([]string, 0, len(env))
	for n := range env {
		names = append(names, n)
	}
	sort.Strings(names)
	for _, n := range names {
		out = append(out, map[string]any{"name": n, "value": env[n]})
	}
	return out
}

var ecrHost = regexp.MustCompile(`^\d{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com(\.cn)?$`)

// isECR: the task execution role pulls from ECR itself; no credentials secret is needed.
func isECR(server string) bool { return ecrHost.MatchString(server) }

func pullSecretName(ref *config.ECSRef) string {
	return "groundcrew/pull/" + ref.Cluster + "/" + ref.Service
}

// pullSecret writes the registry login to Secrets Manager (the task's repositoryCredentials)
// and returns its ARN. create=false only updates one that exists ("" if none).
func (e *ECS) pullSecret(ctx context.Context, ref *config.ECSRef, login *RegistryLogin, create bool) (string, error) {
	value, _ := json.Marshal(map[string]string{"username": login.Username, "password": login.Password})
	name := pullSecretName(ref)
	var described struct {
		ARN string `json:"ARN"`
	}
	err := e.aws.call(ctx, "secretsmanager", "secretsmanager.DescribeSecret", map[string]any{"SecretId": name}, &described)
	var ae awsError
	switch {
	case err == nil:
		if err := e.aws.call(ctx, "secretsmanager", "secretsmanager.PutSecretValue",
			map[string]any{"SecretId": described.ARN, "SecretString": string(value)}, nil); err != nil {
			return "", fmt.Errorf("renewing the image pull secret: %w", err)
		}
		return described.ARN, nil
	case errors.As(err, &ae) && ae.Type == "ResourceNotFoundException":
		if !create {
			return "", nil
		}
		var created struct {
			ARN string `json:"ARN"`
		}
		if err := e.aws.call(ctx, "secretsmanager", "secretsmanager.CreateSecret", map[string]any{
			"Name": name, "SecretString": string(value),
			"Description": "Image pull login for " + ref.Cluster + "/" + ref.Service + ", renewed by the Groundcrew agent",
			"Tags":        []map[string]string{{"Key": "groundcrew:managed", "Value": "true"}},
		}, &created); err != nil {
			return "", fmt.Errorf("writing the image pull secret: %w", err)
		}
		return created.ARN, nil
	default:
		return "", fmt.Errorf("reading the image pull secret: %w", err)
	}
}

// RefreshPull renews the service's pull secret with a fresh login, if a deploy made one.
func (e *ECS) RefreshPull(ctx context.Context, s config.Service, login *RegistryLogin) (bool, error) {
	ref, err := ecsRef(s)
	if err != nil || login == nil || isECR(login.Server) {
		return false, err
	}
	arn, err := e.pullSecret(ctx, ref, login, false)
	return arn != "", err
}
