package runtime

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"strings"
	"testing"
	"time"

	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/config"
)

// Against the real Docker engine, opt in: GC_DOCKER_TEST=1. A throwaway registry
// on 127.0.0.1 holds a good and a bad image; deploys create, update, and refuse.
func TestDeployOnARealDockerEngine(t *testing.T) {
	if os.Getenv("GC_DOCKER_TEST") != "1" {
		t.Skip("set GC_DOCKER_TEST=1 to run against the local Docker engine")
	}
	docker := func(args ...string) string {
		t.Helper()
		out, err := exec.Command("docker", args...).CombinedOutput()
		if err != nil {
			t.Fatalf("docker %v: %v\n%s", args, err, out)
		}
		return strings.TrimSpace(string(out))
	}
	n := time.Now().UnixNano()
	reg, app := fmt.Sprintf("gc-dep-reg-%d", n), fmt.Sprintf("gc-dep-app-%d", n)
	t.Cleanup(func() {
		for _, c := range []string{reg, app} {
			_ = exec.Command("docker", "rm", "-f", c).Run()
		}
		out, _ := exec.Command("docker", "ps", "-aq", "--filter", "name="+app+"-gc-previous").Output()
		for _, id := range strings.Fields(string(out)) {
			_ = exec.Command("docker", "rm", "-f", id).Run()
		}
	})
	docker("run", "-d", "--name", reg, "-p", "127.0.0.1::5000", "registry:3.0.0")
	port := docker("port", reg, "5000")
	host := "127.0.0.1:" + port[strings.LastIndex(port, ":")+1:]
	time.Sleep(time.Second)
	docker("pull", "-q", "nginx:1.27-alpine")
	good := host + "/w0/web:good"
	docker("tag", "nginx:1.27-alpine", good)
	docker("push", "-q", good)
	build := exec.Command("docker", "build", "-q", "-t", host+"/w0/web:bad", "-")
	build.Stdin = strings.NewReader("FROM nginx:1.27-alpine\nCMD [\"sh\", \"-c\", \"exit 3\"]\n")
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build: %v %s", err, out)
	}
	docker("push", "-q", host+"/w0/web:bad")
	docker("rmi", good, host+"/w0/web:bad") // the agent pulls them

	d := NewDocker("/var/run/docker.sock")
	s := config.Service{Name: "web", Runtime: config.Docker, Container: app}
	ctx := context.Background()
	hostPort := 18000 + int(n%1000)

	created, err := d.Deploy(ctx, s, DeployRequest{Image: good, Port: hostPort, Env: map[string]string{"A": "1"}})
	if err != nil || !created {
		t.Fatalf("first deploy: created=%v %v", created, err)
	}
	if st := d.State(ctx, s); !st.Running() || st.Image != good {
		t.Fatalf("after create %+v", st)
	}
	// A second deploy updates it in place, keeping its port and setting a new env value.
	created, err = d.Deploy(ctx, s, DeployRequest{Image: good, Port: hostPort, Env: map[string]string{"A": "2"}})
	if err != nil || created {
		t.Fatalf("second deploy: created=%v %v", created, err)
	}
	if env := docker("inspect", "-f", "{{range .Config.Env}}{{.}} {{end}}", app); !strings.Contains(env, "A=2") || strings.Contains(env, "A=1") {
		t.Fatalf("env %q", env)
	}
	// The app's port stays published (the same port on the host).
	if got := docker("port", app, fmt.Sprint(hostPort)); !strings.HasSuffix(got, fmt.Sprintf(":%d", hostPort)) {
		t.Fatalf("port lost: %q", got)
	}
	// A bad image does not replace it.
	_, err = d.Deploy(ctx, s, DeployRequest{Image: host + "/w0/web:bad", Port: hostPort})
	if err == nil || !strings.Contains(err.Error(), "previous container was started again") {
		t.Fatalf("bad deploy: %v", err)
	}
	if st := d.State(ctx, s); !st.Running() || st.Image != good {
		t.Fatalf("not kept: %+v", st)
	}
	// An image that does not exist fails at the pull, before anything is touched.
	if _, err := d.Deploy(ctx, s, DeployRequest{Image: host + "/w0/web:missing"}); err == nil || !strings.Contains(err.Error(), "pulling") {
		t.Fatalf("missing image: %v", err)
	}
}
