package client

import (
	"context"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

// The same vector is checked by apps/api/tests/test_remote_agents.py, so the
// Go and Python signatures cannot drift apart.
const (
	vectorPublicKey = "A6EHv/POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg="
	vectorSignature = "ze/AbEhegKTHPcNFp74SbEHNdPREcXDOjHtKa+25RLJ+wc5ccTOsyCek7T2we7uC5iEDLTB2JWMK0dz8yvE5BQ=="
)

func vectorKey() ed25519.PrivateKey {
	seed := make([]byte, ed25519.SeedSize)
	for i := range seed {
		seed[i] = byte(i)
	}
	return ed25519.NewKeyFromSeed(seed)
}

func TestSignatureMatchesTheAPIVector(t *testing.T) {
	key := vectorKey()
	pub := base64.StdEncoding.EncodeToString(key.Public().(ed25519.PublicKey))
	if pub != vectorPublicKey {
		t.Fatalf("public key %s", pub)
	}
	msg := Canonical("POST", "/agent/v1/heartbeat", "1700000000", "nonce-nonce-nonce-1",
		[]byte(`{"version":"0.1.0","uptime_seconds":5}`))
	if got := base64.StdEncoding.EncodeToString(ed25519.Sign(key, msg)); got != vectorSignature {
		t.Fatalf("signature %s", got)
	}
}

func TestPostSignsTheExactBodyAndPath(t *testing.T) {
	key := vectorKey()
	var seen http.Header
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		seen = r.Header.Clone()
		sig, _ := base64.StdEncoding.DecodeString(r.Header.Get("X-Groundcrew-Signature"))
		msg := Canonical(r.Method, r.URL.Path, r.Header.Get("X-Groundcrew-Timestamp"), r.Header.Get("X-Groundcrew-Nonce"), body)
		if !ed25519.Verify(key.Public().(ed25519.PublicKey), msg, sig) {
			w.WriteHeader(http.StatusUnauthorized)
			_ = json.NewEncoder(w).Encode(map[string]string{"detail": "bad signature"})
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]int{"heartbeat_seconds": 30})
	}))
	defer srv.Close()

	c := New(srv.URL+"/", "agent-1", key, "groundcrew-agent/test")
	c.now = func() time.Time { return time.Unix(1700000000, 0) }
	var out struct {
		HeartbeatSeconds int `json:"heartbeat_seconds"`
	}
	if err := c.Post(context.Background(), "/agent/v1/heartbeat", map[string]any{"version": "0.1.0"}, &out); err != nil {
		t.Fatal(err)
	}
	if out.HeartbeatSeconds != 30 || seen.Get("X-Groundcrew-Agent") != "agent-1" || seen.Get("X-Groundcrew-Timestamp") != "1700000000" {
		t.Fatalf("out %+v headers %v", out, seen)
	}
	if n := seen.Get("X-Groundcrew-Nonce"); len(n) < 16 {
		t.Fatalf("nonce %q", n)
	}
}

func TestRefusalsCarryTheDetailAndAreClassified(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
		_, _ = w.Write([]byte(`{"detail":"This agent is not enrolled here (or its connection was removed)."}`))
	}))
	defer srv.Close()
	err := New(srv.URL, "agent-1", vectorKey(), "t").Post(context.Background(), "/agent/v1/heartbeat", map[string]any{}, nil)
	if err == nil || !IsPermanent(err) || err.Error() != "Groundcrew answered 401: This agent is not enrolled here (or its connection was removed)." {
		t.Fatalf("got %v", err)
	}
}

func TestAFailedRequestDropsReusedConnections(t *testing.T) {
	calls := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if calls == 1 {
			// Hang past the client's timeout, as a dead connection does.
			time.Sleep(300 * time.Millisecond)
		}
		_, _ = w.Write([]byte(`{}`))
	}))
	defer srv.Close()
	c := New(srv.URL, "agent-1", vectorKey(), "t")
	c.HTTP.Timeout = 100 * time.Millisecond
	if err := c.Post(context.Background(), "/x", map[string]any{}, nil); err == nil {
		t.Fatal("want a timeout")
	}
	c.HTTP.Timeout = 2 * time.Second
	if err := c.Post(context.Background(), "/x", map[string]any{}, nil); err != nil {
		t.Fatalf("the next request must work on a new connection: %v", err)
	}
}
