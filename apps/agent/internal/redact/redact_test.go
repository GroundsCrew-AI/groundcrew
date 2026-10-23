package redact

import (
	"strings"
	"testing"
)

func TestSecretsAreRemovedAndOrdinaryTextKept(t *testing.T) {
	for _, secret := range []string{
		"postgres://app:hunter2@db:5432/app",
		"Authorization: Bearer abcdefghijklmnop",
		"password=hunter2",
		`"api_key": "abc123secret"`,
		"key AKIAABCDEFGHIJKLMNOP used",
		"token ghp_abcdefghijklmnopqrstuvwxyz0123456789",
	} {
		got := Line(secret)
		if strings.Contains(got, "hunter2") || strings.Contains(got, "abcdefghijklmnop") ||
			strings.Contains(got, "abc123secret") || strings.Contains(got, "AKIAABCDEFGHIJKLMNOP") ||
			strings.Contains(got, "ghp_abc") {
			t.Errorf("not redacted: %q -> %q", secret, got)
		}
	}
	plain := "ERROR handling request /orders: connection refused (retry 3)"
	if Line(plain) != plain {
		t.Errorf("changed ordinary text: %q", Line(plain))
	}
}
