// Package client talks to Groundcrew's agent API (/agent/v1). Every request
// after enrollment is signed with the agent's Ed25519 key over the same text
// the API checks (apps/api/app/remote_agents/signing.py):
//
//	groundcrew-agent-v1
//	<METHOD>
//	<path with query>
//	<unix timestamp>
//	<nonce>
//	<sha256 hex of the body>
package client

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"strconv"
	"strings"
	"time"
)

const signatureVersion = "groundcrew-agent-v1"

// Client is safe for concurrent use.
type Client struct {
	BaseURL string
	AgentID string
	Key     ed25519.PrivateKey
	HTTP    *http.Client
	// UserAgent names the agent and its version.
	UserAgent string
	now       func() time.Time
}

// New returns a client with sensible timeouts.
func New(baseURL, agentID string, key ed25519.PrivateKey, userAgent string) *Client {
	// Connections are reused between requests; after a network change (a new address, a
	// router restart) the reused ones can be dead without any error, so they are kept alive
	// with TCP keep-alives and dropped after any failed request (see Post).
	transport := &http.Transport{
		Proxy: http.ProxyFromEnvironment,
		DialContext: (&net.Dialer{
			Timeout:   10 * time.Second,
			KeepAlive: 15 * time.Second,
		}).DialContext,
		TLSHandshakeTimeout: 10 * time.Second,
		IdleConnTimeout:     60 * time.Second,
		MaxIdleConnsPerHost: 4,
		ForceAttemptHTTP2:   true,
	}
	return &Client{
		BaseURL:   strings.TrimRight(baseURL, "/"),
		AgentID:   agentID,
		Key:       key,
		HTTP:      &http.Client{Timeout: 30 * time.Second, Transport: transport},
		UserAgent: userAgent,
		now:       time.Now,
	}
}

// Error is a refusal from Groundcrew, with its explanation.
type Error struct {
	Status int
	Detail string
}

func (e *Error) Error() string {
	return fmt.Sprintf("Groundcrew answered %d: %s", e.Status, e.Detail)
}

// Permanent reports whether retrying cannot help (the agent was removed, the request is wrong).
func (e *Error) Permanent() bool {
	return e.Status == http.StatusUnauthorized || e.Status == http.StatusForbidden ||
		e.Status == http.StatusNotFound || e.Status == http.StatusUnprocessableEntity
}

// Canonical is the text an agent request is signed over.
func Canonical(method, path, timestamp, nonce string, body []byte) []byte {
	sum := sha256.Sum256(body)
	return []byte(strings.Join([]string{
		signatureVersion, strings.ToUpper(method), path, timestamp, nonce, hex.EncodeToString(sum[:]),
	}, "\n"))
}

// Sign adds the four signature headers to req for body.
func (c *Client) Sign(req *http.Request, body []byte) error {
	nonce, err := newNonce()
	if err != nil {
		return err
	}
	stamp := strconv.FormatInt(c.now().Unix(), 10)
	path := req.URL.EscapedPath()
	if req.URL.RawQuery != "" {
		path += "?" + req.URL.RawQuery
	}
	sig := ed25519.Sign(c.Key, Canonical(req.Method, path, stamp, nonce, body))
	req.Header.Set("X-Groundcrew-Agent", c.AgentID)
	req.Header.Set("X-Groundcrew-Timestamp", stamp)
	req.Header.Set("X-Groundcrew-Nonce", nonce)
	req.Header.Set("X-Groundcrew-Signature", base64.StdEncoding.EncodeToString(sig))
	return nil
}

func newNonce() (string, error) {
	b := make([]byte, 18)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(b), nil
}

// Post sends in as JSON to path (signed unless the client has no agent id yet)
// and decodes the answer into out.
func (c *Client) Post(ctx context.Context, path string, in, out any) error {
	body, err := json.Marshal(in)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.BaseURL+path, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("User-Agent", c.UserAgent)
	if c.AgentID != "" {
		if err := c.Sign(req, body); err != nil {
			return err
		}
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		// The connection it used may be dead: the next request dials a new one.
		c.HTTP.CloseIdleConnections()
		return err
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return err
	}
	if resp.StatusCode/100 != 2 {
		return &Error{Status: resp.StatusCode, Detail: detail(raw)}
	}
	if out == nil {
		return nil
	}
	return json.Unmarshal(raw, out)
}

// detail is FastAPI's {"detail": ...}, or the body itself.
func detail(raw []byte) string {
	var body struct {
		Detail any `json:"detail"`
	}
	if json.Unmarshal(raw, &body) == nil && body.Detail != nil {
		if s, ok := body.Detail.(string); ok {
			return s
		}
		b, _ := json.Marshal(body.Detail)
		return string(b)
	}
	s := strings.TrimSpace(string(raw))
	if len(s) > 300 {
		s = s[:300] + "..."
	}
	return s
}

// IsPermanent reports whether err is a refusal retrying cannot fix.
func IsPermanent(err error) bool {
	var e *Error
	return errors.As(err, &e) && e.Permanent()
}
