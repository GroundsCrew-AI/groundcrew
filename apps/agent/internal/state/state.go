// Package state keeps the agent's private key and enrollment in its state
// directory (default /var/lib/groundcrew-agent, mode 0700). The private key
// never leaves the host; Groundcrew only ever sees the public key.
package state

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"os"
	"path/filepath"
)

const (
	keyFile        = "key.pem"
	enrollmentFile = "enrollment.json"
)

// Enrollment is what Groundcrew answered when the agent enrolled.
type Enrollment struct {
	AgentID      string `json:"agent_id"`
	ConnectionID string `json:"connection_id"`
	Name         string `json:"name"`
	URL          string `json:"url"`
}

type Dir struct{ Path string }

// ErrNotEnrolled means `groundcrew-agent enroll` has not run yet.
var ErrNotEnrolled = errors.New("this agent is not enrolled yet: run `groundcrew-agent enroll --token <join token>`")

func (d Dir) ensure() error {
	if err := os.MkdirAll(d.Path, 0o700); err != nil {
		return err
	}
	return os.Chmod(d.Path, 0o700)
}

// NewKey makes a fresh key in memory. SaveKey stores it once Groundcrew
// accepted it, so a failed enrollment never replaces a working key.
func NewKey() (ed25519.PrivateKey, error) {
	_, key, err := ed25519.GenerateKey(rand.Reader)
	return key, err
}

// SaveKey replaces the stored key.
func (d Dir) SaveKey(key ed25519.PrivateKey) error {
	if err := d.ensure(); err != nil {
		return err
	}
	der, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		return err
	}
	data := pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der})
	return writeFile(filepath.Join(d.Path, keyFile), data)
}

// Key reads the private key.
func (d Dir) Key() (ed25519.PrivateKey, error) {
	data, err := os.ReadFile(filepath.Join(d.Path, keyFile))
	if errors.Is(err, os.ErrNotExist) {
		return nil, ErrNotEnrolled
	}
	if err != nil {
		return nil, err
	}
	block, _ := pem.Decode(data)
	if block == nil {
		return nil, fmt.Errorf("%s is not a PEM key", keyFile)
	}
	parsed, err := x509.ParsePKCS8PrivateKey(block.Bytes)
	if err != nil {
		return nil, err
	}
	key, ok := parsed.(ed25519.PrivateKey)
	if !ok {
		return nil, fmt.Errorf("%s is not an Ed25519 key", keyFile)
	}
	return key, nil
}

func (d Dir) SaveEnrollment(e Enrollment) error {
	if err := d.ensure(); err != nil {
		return err
	}
	data, err := json.MarshalIndent(e, "", "  ")
	if err != nil {
		return err
	}
	return writeFile(filepath.Join(d.Path, enrollmentFile), data)
}

func (d Dir) Enrollment() (Enrollment, error) {
	var e Enrollment
	data, err := os.ReadFile(filepath.Join(d.Path, enrollmentFile))
	if errors.Is(err, os.ErrNotExist) {
		return e, ErrNotEnrolled
	}
	if err != nil {
		return e, err
	}
	return e, json.Unmarshal(data, &e)
}

// writeFile replaces path atomically, readable by the owner only.
func writeFile(path string, data []byte) error {
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}
