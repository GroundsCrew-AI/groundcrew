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

// Against the real Docker engine, opt in: GC_DOCKER_TEST=1 (needs the docker CLI
// and nginx:1.27-alpine pullable). It starts throwaway containers named gc-rb-*.
func TestRollbackOnARealDockerEngine(t *testing.T) {
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
	name := fmt.Sprintf("gc-rb-%d", time.Now().UnixNano())
	bad := name + "-bad:1"
	t.Cleanup(func() {
		_ = exec.Command("docker", "rm", "-f", name).Run()
		out, _ := exec.Command("docker", "ps", "-aq", "--filter", "name="+name+"-gc-previous").Output()
		for _, id := range strings.Fields(string(out)) {
			_ = exec.Command("docker", "rm", "-f", id).Run()
		}
		_ = exec.Command("docker", "rmi", "-f", bad).Run()
	})
	good := docker("pull", "-q", "nginx:1.27-alpine")
	goodID := docker("image", "inspect", "-f", "{{.Id}}", good)
	// A "deploy" of an image that exits at once.
	build := exec.Command("docker", "build", "-q", "-t", bad, "-")
	build.Stdin = strings.NewReader("FROM nginx:1.27-alpine\nCMD [\"sh\", \"-c\", \"exit 3\"]\n")
	if out, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build: %v %s", err, out)
	}
	// The bad image runs with an anonymous volume holding data, a label, a port, and an env var.
	docker("run", "-d", "--name", name, "-v", "/data", "-l", "gc.test=1", "-e", "KEEP=yes",
		"-p", "127.0.0.1::80", bad)
	docker("run", "--rm", "--volumes-from", name, "nginx:1.27-alpine", "sh", "-c", "echo kept > /data/f")

	d := NewDocker("/var/run/docker.sock")
	s := config.Service{Name: "web", Runtime: config.Docker, Container: name}
	ctx := context.Background()
	if st := d.State(ctx, s); st.Running() {
		t.Fatalf("the bad image should have exited: %+v", st)
	}
	if err := d.Rollback(ctx, s, goodID, "nginx:1.27-alpine", nil); err != nil {
		t.Fatal(err)
	}
	st := d.State(ctx, s)
	if !st.Running() || st.ImageID != goodID || st.Image != "nginx:1.27-alpine" {
		t.Fatalf("after rollback %+v", st)
	}
	if got := docker("exec", name, "cat", "/data/f"); got != "kept" {
		t.Fatalf("the anonymous volume was not reused: %q", got)
	}
	if got := docker("inspect", "-f", `{{index .Config.Labels "gc.test"}} {{range .Config.Env}}{{.}} {{end}}`, name); !strings.Contains(got, "1 ") || !strings.Contains(got, "KEEP=yes") {
		t.Fatalf("labels or env lost: %q", got)
	}
	if got := docker("port", name, "80"); !strings.HasPrefix(got, "127.0.0.1:") {
		t.Fatalf("port lost: %q", got)
	}
	if left := docker("ps", "-aq", "--filter", "name="+name+"-gc-previous"); left != "" {
		t.Fatalf("the old container was not removed: %s", left)
	}

	// Rolling back to an image that fails keeps the running one.
	badID := docker("image", "inspect", "-f", "{{.Id}}", bad)
	err := d.Rollback(ctx, s, badID, "", nil)
	if err == nil || !strings.Contains(err.Error(), "previous container was started again") {
		t.Fatalf("want a failed rollback that restores the container, got %v", err)
	}
	if st := d.State(ctx, s); !st.Running() || st.ImageID != goodID {
		t.Fatalf("not restored: %+v", st)
	}
}
