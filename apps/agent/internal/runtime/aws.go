package runtime

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"sort"
	"strings"
	"sync"
	"time"
)

// AWS calls from the agent: Signature Version 4 in the standard library (no SDK, so the binary
// stays small) and credentials from where the agent runs (an ECS task role, or environment
// variables for tests). The agent never stores AWS keys.

type awsCredentials struct {
	AccessKeyID     string
	SecretAccessKey string
	SessionToken    string
	Expires         time.Time
}

// awsCredentialSource gives current credentials, refreshing them before they expire.
type awsCredentialSource struct {
	mu     sync.Mutex
	cached awsCredentials
	http   *http.Client
	// The ECS task role endpoint (169.254.170.2 + AWS_CONTAINER_CREDENTIALS_RELATIVE_URI).
	endpoint string
	auth     string
}

func newAWSCredentialSource() (*awsCredentialSource, error) {
	src := &awsCredentialSource{http: &http.Client{Timeout: 10 * time.Second}}
	switch {
	case os.Getenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI") != "":
		src.endpoint = "http://169.254.170.2" + os.Getenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI")
	case os.Getenv("AWS_CONTAINER_CREDENTIALS_FULL_URI") != "":
		src.endpoint = os.Getenv("AWS_CONTAINER_CREDENTIALS_FULL_URI")
		src.auth = os.Getenv("AWS_CONTAINER_AUTHORIZATION_TOKEN")
	case os.Getenv("AWS_ACCESS_KEY_ID") != "":
		src.cached = awsCredentials{
			AccessKeyID:     os.Getenv("AWS_ACCESS_KEY_ID"),
			SecretAccessKey: os.Getenv("AWS_SECRET_ACCESS_KEY"),
			SessionToken:    os.Getenv("AWS_SESSION_TOKEN"),
		}
	default:
		return nil, errors.New("no AWS credentials: not running in an ECS task")
	}
	return src, nil
}

func (s *awsCredentialSource) get(ctx context.Context) (awsCredentials, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.endpoint == "" || (s.cached.AccessKeyID != "" && time.Until(s.cached.Expires) > 5*time.Minute) {
		return s.cached, nil
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, s.endpoint, nil)
	if err != nil {
		return awsCredentials{}, err
	}
	if s.auth != "" {
		req.Header.Set("Authorization", s.auth)
	}
	resp, err := s.http.Do(req)
	if err != nil {
		return awsCredentials{}, fmt.Errorf("reading the task role's credentials: %w", err)
	}
	defer resp.Body.Close()
	var out struct {
		AccessKeyID     string    `json:"AccessKeyId"`
		SecretAccessKey string    `json:"SecretAccessKey"`
		Token           string    `json:"Token"`
		Expiration      time.Time `json:"Expiration"`
	}
	if resp.StatusCode != http.StatusOK || json.NewDecoder(resp.Body).Decode(&out) != nil {
		return awsCredentials{}, fmt.Errorf("the task role's credentials endpoint answered %d", resp.StatusCode)
	}
	s.cached = awsCredentials{out.AccessKeyID, out.SecretAccessKey, out.Token, out.Expiration}
	return s.cached, nil
}

func sha256Hex(b []byte) string {
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}

func hmacSHA256(key []byte, data string) []byte {
	m := hmac.New(sha256.New, key)
	m.Write([]byte(data))
	return m.Sum(nil)
}

// awsURIEncode encodes like SigV4 wants: every byte except unreserved characters.
func awsURIEncode(s string, keepSlash bool) string {
	var b strings.Builder
	for _, c := range []byte(s) {
		switch {
		case 'A' <= c && c <= 'Z', 'a' <= c && c <= 'z', '0' <= c && c <= '9', c == '-', c == '_', c == '.', c == '~':
			b.WriteByte(c)
		case c == '/' && keepSlash:
			b.WriteByte(c)
		default:
			fmt.Fprintf(&b, "%%%02X", c)
		}
	}
	return b.String()
}

// signV4 signs req (whose body is `body`) for service and region at time `now`: it sets
// X-Amz-Date, X-Amz-Security-Token (with a session), and Authorization.
func signV4(req *http.Request, body []byte, creds awsCredentials, service, region string, now time.Time) {
	stamp := now.UTC().Format("20060102T150405Z")
	day := stamp[:8]
	req.Header.Set("X-Amz-Date", stamp)
	if creds.SessionToken != "" {
		req.Header.Set("X-Amz-Security-Token", creds.SessionToken)
	}
	host := req.Host
	if host == "" {
		host = req.URL.Host
	}
	headers := map[string]string{"host": host}
	for name, values := range req.Header {
		lower := strings.ToLower(name)
		if lower == "authorization" || lower == "user-agent" {
			continue
		}
		headers[lower] = strings.Join(strings.Fields(strings.Join(values, ",")), " ")
	}
	names := make([]string, 0, len(headers))
	for n := range headers {
		names = append(names, n)
	}
	sort.Strings(names)
	var canonHeaders strings.Builder
	for _, n := range names {
		canonHeaders.WriteString(n + ":" + headers[n] + "\n")
	}
	signed := strings.Join(names, ";")

	query := req.URL.Query()
	keys := make([]string, 0, len(query))
	for k := range query {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var pairs []string
	for _, k := range keys {
		vals := append([]string(nil), query[k]...)
		sort.Strings(vals)
		for _, v := range vals {
			pairs = append(pairs, awsURIEncode(k, false)+"="+awsURIEncode(v, false))
		}
	}
	path := req.URL.EscapedPath()
	if path == "" {
		path = "/"
	}
	canonical := strings.Join([]string{
		req.Method, path, strings.Join(pairs, "&"), canonHeaders.String(), signed, sha256Hex(body),
	}, "\n")
	scope := day + "/" + region + "/" + service + "/aws4_request"
	toSign := "AWS4-HMAC-SHA256\n" + stamp + "\n" + scope + "\n" + sha256Hex([]byte(canonical))
	key := hmacSHA256([]byte("AWS4"+creds.SecretAccessKey), day)
	key = hmacSHA256(key, region)
	key = hmacSHA256(key, service)
	key = hmacSHA256(key, "aws4_request")
	signature := hex.EncodeToString(hmacSHA256(key, toSign))
	req.Header.Set("Authorization", fmt.Sprintf(
		"AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s",
		creds.AccessKeyID, scope, signed, signature))
}

// awsClient makes JSON calls (X-Amz-Target) to AWS services in one region.
type awsClient struct {
	region string
	creds  interface {
		get(ctx context.Context) (awsCredentials, error)
	}
	http *http.Client
	// endpoint overrides https://<service>.<region>.amazonaws.com (tests).
	endpoint func(service string) string
	now      func() time.Time
}

type awsError struct {
	Status  int
	Type    string
	Message string
}

func (e awsError) Error() string {
	return fmt.Sprintf("AWS answered %d %s: %s", e.Status, e.Type, e.Message)
}

// call posts one JSON 1.1 request, e.g. ("ecs", "AmazonEC2ContainerServiceV20141113.DescribeServices").
func (c *awsClient) call(ctx context.Context, service, target string, in, out any) error {
	body, err := json.Marshal(in)
	if err != nil {
		return err
	}
	endpoint := "https://" + service + "." + c.region + ".amazonaws.com/"
	if c.endpoint != nil {
		endpoint = c.endpoint(service)
	}
	u, err := url.Parse(endpoint)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, u.String(), bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/x-amz-json-1.1")
	req.Header.Set("X-Amz-Target", target)
	creds, err := c.creds.get(ctx)
	if err != nil {
		return err
	}
	now := time.Now
	if c.now != nil {
		now = c.now
	}
	signV4(req, body, creds, service, c.region, now())
	resp, err := c.http.Do(req)
	if err != nil {
		return fmt.Errorf("cannot reach AWS %s: %w", service, err)
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	if err != nil {
		return err
	}
	if resp.StatusCode >= 300 {
		var e struct {
			Type    string `json:"__type"`
			Message string `json:"message"`
			Msg     string `json:"Message"`
		}
		_ = json.Unmarshal(data, &e)
		msg := e.Message + e.Msg
		if i := strings.LastIndex(e.Type, "#"); i >= 0 {
			e.Type = e.Type[i+1:]
		}
		return awsError{resp.StatusCode, e.Type, msg}
	}
	if out == nil {
		return nil
	}
	return json.Unmarshal(data, out)
}
