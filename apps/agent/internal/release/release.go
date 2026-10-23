// Package release checks Groundcrew agent releases: SHA256SUMS signed with the release key
// (Ed25519, held by the owner, docs/agent-plan.md "A5 plan"), and a binary's sum in it.
package release

import (
	"crypto/ed25519"
	"crypto/sha256"
	"crypto/x509"
	_ "embed"
	"encoding/hex"
	"encoding/pem"
	"errors"
	"fmt"
	"strings"
)

// releasePub is the release public key (PEM, SubjectPublicKeyInfo). `make agent-release-key`
// writes it; install.sh carries the same key (a test keeps them equal).
//
//go:embed release.pub
var releasePub []byte

// PublicKey parses a PEM Ed25519 public key.
func PublicKey(pemBytes []byte) (ed25519.PublicKey, error) {
	block, _ := pem.Decode(pemBytes)
	if block == nil {
		return nil, errors.New("no release key in this build (make agent-release-key)")
	}
	key, err := x509.ParsePKIXPublicKey(block.Bytes)
	if err != nil {
		return nil, fmt.Errorf("reading the release key: %w", err)
	}
	pub, ok := key.(ed25519.PublicKey)
	if !ok {
		return nil, errors.New("the release key is not an Ed25519 key")
	}
	return pub, nil
}

// Key is the release key this agent was built with.
func Key() (ed25519.PublicKey, error) { return PublicKey(releasePub) }

// Verify checks that sig (raw, 64 bytes, as `openssl pkeyutl -sign -rawin` writes it) signs sums.
func Verify(pub ed25519.PublicKey, sums, sig []byte) error {
	if len(sig) != ed25519.SignatureSize || !ed25519.Verify(pub, sums, sig) {
		return errors.New("SHA256SUMS is not signed with the Groundcrew release key")
	}
	return nil
}

// Check verifies the signature, then that data is the file `name` listed in sums.
func Check(pub ed25519.PublicKey, sums, sig []byte, name string, data []byte) error {
	if err := Verify(pub, sums, sig); err != nil {
		return err
	}
	want, err := Sum(sums, name)
	if err != nil {
		return err
	}
	got := sha256.Sum256(data)
	if hex.EncodeToString(got[:]) != want {
		return fmt.Errorf("%s does not match its signed checksum", name)
	}
	return nil
}

// Sum is name's SHA-256 in a SHA256SUMS file ("<hex>  <name>" lines).
func Sum(sums []byte, name string) (string, error) {
	for _, line := range strings.Split(string(sums), "\n") {
		fields := strings.Fields(line)
		if len(fields) == 2 && strings.TrimPrefix(fields[1], "*") == name && len(fields[0]) == 64 {
			return strings.ToLower(fields[0]), nil
		}
	}
	return "", fmt.Errorf("%s is not in SHA256SUMS", name)
}
