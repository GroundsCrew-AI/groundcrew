package state

import (
	"errors"
	"os"
	"path/filepath"
	"testing"
)

func TestKeyAndEnrollmentAreKeptPrivate(t *testing.T) {
	d := Dir{Path: filepath.Join(t.TempDir(), "state")}
	if _, err := d.Key(); !errors.Is(err, ErrNotEnrolled) {
		t.Fatalf("want ErrNotEnrolled, got %v", err)
	}
	key, err := NewKey()
	if err != nil {
		t.Fatal(err)
	}
	if _, err := d.Key(); !errors.Is(err, ErrNotEnrolled) {
		t.Fatal("a new key is not stored until SaveKey")
	}
	if err := d.SaveKey(key); err != nil {
		t.Fatal(err)
	}
	again, err := d.Key()
	if err != nil || !again.Equal(key) {
		t.Fatalf("key round trip: %v", err)
	}
	if err := d.SaveEnrollment(Enrollment{AgentID: "a1", URL: "https://api.groundkrew.com"}); err != nil {
		t.Fatal(err)
	}
	e, err := d.Enrollment()
	if err != nil || e.AgentID != "a1" {
		t.Fatalf("enrollment %+v %v", e, err)
	}
	for _, p := range []string{d.Path, filepath.Join(d.Path, keyFile), filepath.Join(d.Path, enrollmentFile)} {
		info, err := os.Stat(p)
		if err != nil {
			t.Fatal(err)
		}
		if info.Mode().Perm()&0o077 != 0 {
			t.Errorf("%s is %v, want no group or other access", p, info.Mode().Perm())
		}
	}
}
