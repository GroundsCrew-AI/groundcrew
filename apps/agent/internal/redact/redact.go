// Package redact removes what looks like a secret from a log line before it
// leaves the host, the same idea as Groundcrew's own redaction
// (apps/api/app/security/redact.py), which runs again on arrival. Patterns are
// broad: a false positive costs a little readability, a miss leaks a secret.
package redact

import "regexp"

const Redacted = "[REDACTED]"

var patterns = []struct {
	re   *regexp.Regexp
	with string
}{
	// PEM private keys
	{regexp.MustCompile(`(?s)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|\z)`), Redacted},
	// Credentials in URLs: scheme://user:password@host
	{regexp.MustCompile(`(\b[a-zA-Z][a-zA-Z0-9+.-]*://[^/\s:@]+:)[^@\s/]+(@)`), "${1}" + Redacted + "${2}"},
	// Authorization headers and bearer tokens
	{regexp.MustCompile(`(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{8,}`), "${1} " + Redacted},
	// Well-known token formats
	{regexp.MustCompile(`\bsk-[A-Za-z0-9_-]{16,}`), Redacted},
	{regexp.MustCompile(`\b(AKIA|ASIA)[0-9A-Z]{16}\b`), Redacted},
	{regexp.MustCompile(`\bgh[pousr]_[A-Za-z0-9]{30,}\b`), Redacted},
	{regexp.MustCompile(`\bgithub_pat_[A-Za-z0-9_]{20,}`), Redacted},
	{regexp.MustCompile(`\bxox[abprs]-[A-Za-z0-9-]{10,}`), Redacted},
	{regexp.MustCompile(`\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}`), Redacted},
	// key=value or "key": "value" where the key names a secret
	{regexp.MustCompile(`(?i)(\b[\w.-]*(password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key|credential|authorization)[\w.-]*["']?\s*[:=]\s*["']?)[^\s"',;}&]+`), "${1}" + Redacted},
}

// Line returns the line with anything secret-looking replaced.
func Line(s string) string {
	for _, p := range patterns {
		s = p.re.ReplaceAllString(s, p.with)
	}
	return s
}
