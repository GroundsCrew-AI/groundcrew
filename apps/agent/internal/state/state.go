// Package state keeps the agent's private key and enrollment in its state
// directory (default /var/lib/groundcrew-agent, mode 0700). The private key
// never leaves the host; Groundcrew only ever sees the public key.
package state

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

const (
	keyFile        = "key.pem"
	enrollmentFile = "enrollment.json"
	commandKeyFile = "command_key"
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

// ErrCommandKeyChanged means Groundcrew sent a command key other than the one
// pinned: commands are refused until the agent is enrolled again.
var ErrCommandKeyChanged = errors.New("Groundcrew's command key changed since it was pinned: enroll the agent again to trust the new key")

// PinCommandKey stores Groundcrew's command key the first time it is seen and
// returns the pinned key; a different key later is ErrCommandKeyChanged.
func (d Dir) PinCommandKey(b64 string) (ed25519.PublicKey, error) {
	key, err := base64.StdEncoding.DecodeString(b64)
	if err != nil || len(key) != ed25519.PublicKeySize {
		return nil, fmt.Errorf("not an Ed25519 public key: %q", b64)
	}
	if pinned, err := d.CommandKey(); err == nil && pinned != nil {
		if !pinned.Equal(ed25519.PublicKey(key)) {
			return pinned, ErrCommandKeyChanged
		}
		return pinned, nil
	}
	return key, writeFile(filepath.Join(d.Path, commandKeyFile), []byte(b64+"\n"))
}

// CommandKey is the pinned key, or nil before the first heartbeat.
func (d Dir) CommandKey() (ed25519.PublicKey, error) {
	data, err := os.ReadFile(filepath.Join(d.Path, commandKeyFile))
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	key, err := base64.StdEncoding.DecodeString(strings.TrimSpace(string(data)))
	if err != nil || len(key) != ed25519.PublicKeySize {
		return nil, fmt.Errorf("%s is not an Ed25519 public key", commandKeyFile)
	}
	return key, nil
}

// ClearCommandKey forgets the pinned key (a new enrollment trusts the key it gets next).
func (d Dir) ClearCommandKey() error {
	err := os.Remove(filepath.Join(d.Path, commandKeyFile))
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	return err
}
