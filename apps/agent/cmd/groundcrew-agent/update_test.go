package main

import (
	"bytes"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// A release server with a signed SHA256SUMS for this platform's binary.
func releaseServer(t *testing.T, priv ed25519.PrivateKey, bin []byte, served []byte) *httptest.Server {
	t.Helper()
	name := fmt.Sprintf("groundcrew-agent-%s-%s", runtime.GOOS, runtime.GOARCH)
	sum := sha256.Sum256(bin)
	sums := []byte(hex.EncodeToString(sum[:]) + "  " + name + "\n")
	files := map[string][]byte{
		"/SHA256SUMS":     sums,
		"/SHA256SUMS.sig": ed25519.Sign(priv, sums),
		"/" + name:        served,
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if b, ok := files[r.URL.Path]; ok {
			_, _ = w.Write(b)
			return
		}
		http.NotFound(w, r)
	}))
	t.Cleanup(srv.Close)
	return srv
}

func TestUpdateInstallsOnlyASignedMatchingRelease(t *testing.T) {
	pub, priv, _ := ed25519.GenerateKey(rand.Reader)
	newBin := []byte("#!/bin/sh\necho new\n")
	self := filepath.Join(t.TempDir(), "groundcrew-agent")
	if err := os.WriteFile(self, []byte("old"), 0o755); err != nil {
		t.Fatal(err)
	}
	restarted := 0
	restart := func() error { restarted++; return nil }
	var out bytes.Buffer

	// The served binary is not the signed one: refused, nothing replaced.
	bad := releaseServer(t, priv, newBin, []byte("tampered"))
	if err := install(pub, bad.URL, self, restart, &out); err == nil || !strings.Contains(err.Error(), "does not match") {
		t.Fatalf("tampered binary: %v", err)
	}
	// Signed with another key: refused before any download of the binary.
	_, other, _ := ed25519.GenerateKey(rand.Reader)
	forged := releaseServer(t, other, newBin, newBin)
	if err := install(pub, forged.URL, self, restart, &out); err == nil || !strings.Contains(err.Error(), "not signed") {
		t.Fatalf("forged release: %v", err)
	}
	if got, _ := os.ReadFile(self); string(got) != "old" || restarted != 0 {
		t.Fatalf("replaced after a refusal: %q, %d restarts", got, restarted)
	}
	// The real one: installed, executable, restarted; a second run has nothing to do.
	good := releaseServer(t, priv, newBin, newBin)
	if err := install(pub, good.URL, self, restart, &out); err != nil {
		t.Fatal(err)
	}
	got, _ := os.ReadFile(self)
	info, _ := os.Stat(self)
	if !bytes.Equal(got, newBin) || info.Mode().Perm() != 0o755 || restarted != 1 {
		t.Fatalf("not installed: %q %v %d", got, info.Mode(), restarted)
	}
	if err := install(pub, good.URL, self, restart, &out); err != nil || !strings.Contains(out.String(), "Already the latest") {
		t.Fatalf("second run: %v %q", err, out.String())
	}
	leftovers, _ := filepath.Glob(filepath.Join(filepath.Dir(self), ".groundcrew-agent-update-*"))
	if len(leftovers) != 0 {
		t.Fatalf("temp files left: %v", leftovers)
	}
}
