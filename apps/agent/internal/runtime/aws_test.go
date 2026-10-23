package runtime

import (
	"net/http"
	"strings"
	"testing"
	"time"
)

var exampleCreds = awsCredentials{AccessKeyID: "AKIDEXAMPLE", SecretAccessKey: "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"}
var exampleTime = time.Date(2015, 8, 30, 12, 36, 0, 0, time.UTC)

// AWS's published examples: the IAM ListUsers request in the SigV4 documentation, and
// get-vanilla from the SigV4 test suite.
func TestSigV4MatchesAWSExamples(t *testing.T) {
	req, _ := http.NewRequest(http.MethodGet, "https://iam.amazonaws.com/?Action=ListUsers&Version=2010-05-08", nil)
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded; charset=utf-8")
	signV4(req, nil, exampleCreds, "iam", "us-east-1", exampleTime)
	want := "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/iam/aws4_request, " +
		"SignedHeaders=content-type;host;x-amz-date, " +
		"Signature=5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7"
	if got := req.Header.Get("Authorization"); got != want {
		t.Fatalf("iam example:\n got %s\nwant %s", got, want)
	}

	vanilla, _ := http.NewRequest(http.MethodGet, "https://example.amazonaws.com/", nil)
	signV4(vanilla, nil, exampleCreds, "service", "us-east-1", exampleTime)
	if got := vanilla.Header.Get("Authorization"); !strings.HasSuffix(got,
		"SignedHeaders=host;x-amz-date, Signature=5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31") {
		t.Fatalf("get-vanilla: %s", got)
	}
}
