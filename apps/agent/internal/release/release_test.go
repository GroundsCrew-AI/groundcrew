package release

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"crypto/x509"
	"encoding/hex"
	"encoding/pem"
	"os"
	"regexp"
	"strings"
	"testing"
)

func keyPair(t *testing.T) (ed25519.PublicKey, ed25519.PrivateKey, []byte) {
	t.Helper()
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	der, err := x509.MarshalPKIXPublicKey(pub)
	if err != nil {
		t.Fatal(err)
	}
	return pub, priv, pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: der})
}

func TestASignedReleaseChecksAndAnythingElseIsRefused(t *testing.T) {
	_, priv, pemBytes := keyPair(t)
	pub, err := PublicKey(pemBytes)
	if err != nil {
		t.Fatal(err)
	}
	bin := []byte("the agent")
	sum := sha256.Sum256(bin)
	sums := []byte(hex.EncodeToString(sum[:]) + "  groundcrew-agent-linux-amd64\n")
	sig := ed25519.Sign(priv, sums)

	if err := Check(pub, sums, sig, "groundcrew-agent-linux-amd64", bin); err != nil {
		t.Fatalf("good release: %v", err)
	}
	if err := Check(pub, sums, sig, "groundcrew-agent-linux-amd64", []byte("changed")); err == nil {
		t.Fatal("a changed binary passed")
	}
	if err := Check(pub, sums, sig, "groundcrew-agent-linux-arm64", bin); err == nil {
		t.Fatal("a binary not in SHA256SUMS passed")
	}
	tampered := append([]byte(strings.Repeat("0", 64)+"  groundcrew-agent-linux-amd64\n"), sums...)
	if err := Verify(pub, tampered, sig); err == nil {
		t.Fatal("changed sums passed")
	}
	_, other, _ := keyPair(t)
	if err := Verify(pub, sums, ed25519.Sign(other, sums)); err == nil {
		t.Fatal("another key's signature passed")
	}
	if _, err := PublicKey(nil); err == nil {
		t.Fatal("no key parsed")
	}
}

// install.sh carries the same public key as the agent, so both check against one key.
func TestInstallScriptHasTheSameKey(t *testing.T) {
	script, err := os.ReadFile("../../packaging/install.sh")
	if err != nil {
		t.Fatal(err)
	}
	m := regexp.MustCompile(`(?s)RELEASE_PUB='(.*?)'`).FindSubmatch(script)
	if m == nil {
		t.Fatal("install.sh has no RELEASE_PUB")
	}
	if strings.TrimSpace(string(m[1])) != strings.TrimSpace(string(releasePub)) {
		t.Fatal("install.sh's RELEASE_PUB differs from internal/release/release.pub (make agent-release-key)")
	}
}
