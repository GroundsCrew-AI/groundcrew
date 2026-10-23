package main

import (
	"bytes"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"time"

	"github.com/GroundsCrew-AI/groundcrew/apps/agent/internal/release"
)

// DefaultDownloads is where Groundcrew serves agent releases.
const DefaultDownloads = "https://groundkrew.com/agent"

// update installs the latest release over this binary, only if SHA256SUMS is signed with the
// release key this binary was built with and the binary matches it, then restarts the service.
func update(downloads string, w io.Writer) error {
	pub, err := release.Key()
	if err != nil {
		return err
	}
	self, err := os.Executable()
	if err != nil {
		return err
	}
	if self, err = filepath.EvalSymlinks(self); err != nil {
		return err
	}
	restart := func() error { return exec.Command("systemctl", "restart", "groundcrew-agent").Run() }
	return install(pub, downloads, self, restart, w)
}

// install puts the signed release for this platform at path (see update).
func install(pub ed25519.PublicKey, downloads, self string, restart func() error, w io.Writer) error {
	name := fmt.Sprintf("groundcrew-agent-%s-%s", runtime.GOOS, runtime.GOARCH)
	client := &http.Client{Timeout: 5 * time.Minute}
	get := func(file string) ([]byte, error) {
		resp, err := client.Get(downloads + "/" + file)
		if err != nil {
			return nil, fmt.Errorf("downloading %s: %w", file, err)
		}
		defer resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			return nil, fmt.Errorf("downloading %s: HTTP %d", file, resp.StatusCode)
		}
		return io.ReadAll(io.LimitReader(resp.Body, 200<<20))
	}
	sums, err := get("SHA256SUMS")
	if err != nil {
		return err
	}
	sig, err := get("SHA256SUMS.sig")
	if err != nil {
		return err
	}
	if err := release.Verify(pub, sums, sig); err != nil {
		return err
	}
	want, err := release.Sum(sums, name)
	if err != nil {
		return err
	}
	if current, err := os.ReadFile(self); err == nil {
		if sum := sha256.Sum256(current); hex.EncodeToString(sum[:]) == want {
			fmt.Fprintln(w, "Already the latest release.")
			return nil
		}
	}
	bin, err := get(name)
	if err != nil {
		return err
	}
	if err := release.Check(pub, sums, sig, name, bin); err != nil {
		return err
	}
	// Next to the binary, so the rename is atomic; it replaces the old one only when complete.
	tmp, err := os.CreateTemp(filepath.Dir(self), ".groundcrew-agent-update-*")
	if err != nil {
		return fmt.Errorf("writing next to %s (run as root): %w", self, err)
	}
	defer os.Remove(tmp.Name())
	if _, err := io.Copy(tmp, bytes.NewReader(bin)); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Chmod(0o755); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Rename(tmp.Name(), self); err != nil {
		return fmt.Errorf("replacing %s (run as root): %w", self, err)
	}
	fmt.Fprintf(w, "Installed the release with checksum %s (signature verified).\n", want[:12])
	if err := restart(); err != nil {
		fmt.Fprintln(w, "Restart the service to use it: systemctl restart groundcrew-agent")
		return nil
	}
	fmt.Fprintln(w, "Restarted groundcrew-agent.")
	return nil
}
