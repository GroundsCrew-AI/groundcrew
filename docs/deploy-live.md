# Live deploys: runbook

How to prove the deploy infrastructure works on real targets, and clean up after. The kind
section comes with deliverable 6; this page starts with the AWS smoke test.

## AWS: `make smoke-ecs`

Proves the `groundcrew-deploy` role (connect stack `Tier=deploy`, `docs/phase4-infra.md`) can
deploy a container end to end, and that AWS itself refuses what it must not do. Run it after
creating or updating a connect stack, and before Ahmed's ECS target relies on a connection.

```bash
make smoke-ecs CONNECTION=<aws connection id>              # dry run: read-only checks + plan
make smoke-ecs CONNECTION=<aws connection id> DRY_RUN=0    # the real run (about 5-10 minutes)
make smoke-ecs CONNECTION=<aws connection id> CLEANUP=1    # remove leftovers of a failed run
```

The connection id is on the Connections page (or `SELECT id FROM connections WHERE kind='aws'`).
The connection must be connected with tier `deploy`. It needs the local stack's database
(`make db` or `make dev`) and Groundcrew's platform identity in `.env`
(`GROUNDCREW_AWS_ACCESS_KEY_ID` / `GROUNDCREW_AWS_SECRET_ACCESS_KEY`), plus Docker.

### What it uses

Only `ConnectionCredentialsProvider` for that connection: `aws_deploy_session()` (the deploy
role, guarded by `AWS_DEPLOY_OPERATIONS`) for everything, and `aws_session()` (read-only) for
one check. Never your AWS CLI profile or `~/.aws`. Docker logs in to ECR with the role's
registry token through `--password-stdin` into a temporary `DOCKER_CONFIG`, so your
`~/.docker` is not touched.

Everything it creates is named `groundcrew-smoke*` / `/groundcrew/smoke` /
`groundcrew/smoke/TEST_VAR` and tagged `project=groundcrew` and
`groundcrew:deploy=smoke-<UTC time>`.

### The dry run (default)

Read-only: assumes the deploy role and checks the identity and account, that
`groundcrew-deploys` is ACTIVE, the default VPC's subnets, that the ECS service-linked role
exists, that no smoke resources are left over, that Docker works (it pulls `nginx:1.27-alpine`
locally), and this machine's public IP. Then it prints every AWS write the real run would make.
A test runs the dry run against sessions guarded by the read-only allowlist, so any write in
it would fail the test.

### The real run (`DRY_RUN=0`)

| Step | What | Passes when |
|---|---|---|
| 1 | ECR `groundcrew-smoke`, push `nginx:1.27-alpine` | push succeeds |
| 2 | Log group `/groundcrew/smoke`, 1-day retention | created |
| 3 | Security group `groundcrew-smoke-<time>` in the default VPC, tcp/80 **from this machine's IP /32 only** | created |
| 4 | Secret `groundcrew/smoke/TEST_VAR`, a random dummy value made for this run | created |
| 5 | Task definition `groundcrew-smoke`: 0.25 vCPU / 512 MB, execution role `groundcrew-task-execution`, `TEST_VAR` from the secret | registered |
| 6 | Service `smoke` in `groundcrew-deploys`, public IP | stable within 10 minutes; the task's public IP is found |
| 7 | `GET http://<task IP>/` | 200 within 90 s |
| 8 | The container logs `TEST_VAR=set sha256:<12 hex>` | the hash matches the dummy value |
| 9a-e | Negative checks (below) | AWS refuses |
| 10 | Cleanup (below) | everything deleted |

**The secret value never leaves the process.** The container prints only `set` and a 12-character
SHA-256 prefix; the script compares that prefix. Everything the script prints or saves goes
through a scrubber that refuses to output the dummy value or the registry password (a test
checks the output and the report of a full run for both).

### Negative checks

Sent with the deploy role's own credentials, but through clients without the code allowlist
(it would stop a and e before AWS sees them), so the refusal comes from AWS IAM. These five
hardcoded requests in `negative_checks()` are the only unguarded calls in the script.

| Check | Request | PASS | IAM gap |
|---|---|---|---|
| a | `secretsmanager:GetSecretValue groundcrew/smoke/TEST_VAR` | `AccessDeniedException` (explicit Deny) | allowed (the body is never printed) |
| b | `ecr:CreateRepository smoke-untagged-name` with the right tags | `AccessDeniedException` | allowed |
| c | `ec2:CreateSecurityGroup` without the project tag, `DryRun=True` | `UnauthorizedOperation` | `DryRunOperation` (nothing is created either way) |
| d | `ecs:CreateService` in `smoke-nonexistent-cluster` | `AccessDeniedException` | `ClusterNotFoundException` |
| e | `iam:CreateRole groundcrew-smoke-should-not-exist` | `AccessDenied` | allowed |

For a and e the table also notes that the code allowlist refused them first. If b or e is wrongly
allowed, the deploy role cannot delete what it made: the script prints the `aws` command to run
yourself and does not run it.

### Cleanup

Always runs (also after a failure or Ctrl-C), in order: service (scale to 0, delete, wait
inactive), security group (retried for up to 3 minutes while the task's network interface is
released), log group, ECR repository (force), secret (no recovery window). Each delete checks
ownership first: the resource's own tags where AWS returns them (service, security group,
secret), otherwise that this run created it (repository, log group; the deploy role cannot read
their tags). A failed delete does not stop the rest; it is listed as NOT CLEANED UP.

The task definition revision stays: the deploy role cannot deregister task definitions (on
purpose, `docs/phase4-infra.md`), and they cost nothing.

`CLEANUP=1` removes the same fixed names when they are tagged `project=groundcrew` (for the
repository and log group, checked with `tag:GetResources`). Anything else is left alone.

### Reading the output

A table of step, result, seconds, note. Results: `PASS`; `FAIL` (the note has the AWS error
code); `GAP` (a negative check AWS allowed: an IAM gap); `SKIP` (cleanup found a smoke-named
resource that is not ours and left it). The exit code is non-zero on any FAIL, GAP, or leftover.
The report is saved to `.smoke/<run id>.json` (git-ignored, no secret values).

An IAM gap is fixed in `infra/aws-connect.yaml` (and its API copy), checked with `cfn-lint`,
Access Analyzer `ValidatePolicy`, and `tests/test_aws_connect_template.py`, then republished
(`make publish-connect-template`) and the stack updated by the account owner, and the smoke run
again.

### Cost

About $0.003 for a 10-minute run: a 0.25 vCPU / 0.5 GB Fargate task ($0.04048 per vCPU-hour,
$0.004445 per GB-hour, us-east-2 list prices; estimates) plus its public IPv4 ($0.005/hour), and a
secret kept for minutes ($0.40 per secret-month, prorated). ECR storage and log ingestion for
one small image and a few lines round to $0. The script prints its own estimate from how long
the service ran.
