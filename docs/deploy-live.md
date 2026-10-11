# Live deploys: runbook

How to prove the deploy infrastructure works on real targets, and clean up after: the local
kind cluster first (no cloud, no cost), then AWS ECS. Both end with `make cleanup-deploys`.

## kind: local Kubernetes

Everything here runs on this machine. The setup itself (installing Docker, what `make kind-up`
creates, the deployer's permissions) is in `docs/kind.md`.

### Run it

```bash
make kind-up                                  # cluster, registry, ingress, deployer RBAC (~3 min the first time)
make kind-kubeconfig                          # .kind/groundcrew-deployer{,.host}.kubeconfig
make kind-check                               # the deployer can do what it must, and nothing else
make dev KIND=1                               # api, worker, and dind on the kind network (keep it running)
make kind-register LOGIN=<github login> DISCOVER=1   # kind as a Kubernetes connection, then discovery
make dind-check                               # build in dind from the worker, push, run in kind
make cleanup-deploys HOURS=24                 # dry run: what deploys left behind
```

Already had a cluster before the `dind` service existed? Run `make kind-up` once more: it adds
the `kind-registry:5000` mirror to the running node (nothing is restarted). Then restart the
stack with `make dev KIND=1`.

### What each step proves

| Step | Proves | Passes when |
|---|---|---|
| `make kind-up` | The cluster, the registry (`localhost:5001` here, `kind-registry:5000` on the `kind` network, both mirrored on the node), ingress-nginx, and the deployer's RBAC and admission policy exist | It prints `Ready` |
| `make kind-kubeconfig` | The deployer ServiceAccount has a token, and kubeconfigs for Compose (`groundcrew-control-plane:6443`) and the host | Both files exist, mode 600 |
| `make kind-check` | As the ServiceAccount: read everywhere but no Secrets outside `gc-*`; may create only `gc-*` namespaces labelled `project=groundcrew`; may bind only `gc-namespace-admin`, only in `gc-*`; cannot touch `groundcrew-system` | `All checks passed.` |
| `make dev KIND=1` | Groundcrew in Compose reaches the cluster and registry by name; the worker reaches dind over TLS only | api, worker, and dind running; `dind-certs` exited 0 |
| `make kind-register LOGIN=... DISCOVER=1` | The API accepts the deployer kubeconfig (it lists namespaces before saving), discovery reads the cluster, and every `gc-*` namespace is on the Map, without reading a Secret | `Live check passed.` |
| `make dind-check` | The Builder's path: the worker builds a fixture in dind (not the host daemon, no socket mounted, key 0600), smoke-tests it at `$SANDBOX_SMOKE_HOST`, pushes it to `kind-registry:5000`, and the deployer runs it in a `gc-*` namespace behind ingress-nginx | `PASS`, then cleanup deletes the namespace |
| `make cleanup-deploys HOURS=N` | What deploys left behind is found by tag and age; pinned namespaces (`groundcrew/keep=true`) stay | The table matches what you expect; `APPLY=1` deletes the `DELETE` rows |

A deploy's app answers at `http://<app>.localhost` (ingress host `<app>.localhost`; `*.localhost`
resolves to 127.0.0.1). Check its namespace with the host kubeconfig:

```bash
export PATH="$PWD/.tools/bin:$PATH"
kubectl --kubeconfig .kind/groundcrew-deployer.host.kubeconfig get ns -l project=groundcrew --show-labels
kubectl --kubeconfig .kind/groundcrew-deployer.host.kubeconfig -n gc-<app> get rolebinding,deploy,svc,ingress
curl -i http://<app>.localhost/
```

### Results (2026-10-11)

| Step | Result |
|---|---|
| `make kind-check` | All checks passed |
| `make kind-register LOGIN=Rizwan-095 DISCOVER=1` | Already registered (reused); discovery succeeded; 13 Kubernetes resources on the Map; `gc-whoami` on the Map; live check passed |
| `make dind-check` | PASS: TLS only (2375 refused, host daemon not reachable, key 0600 uid 1000); `express-redis` fixture built (31 s cold, 3 s cached); `GET http://dind:<port>/healthz` OK; pushed to `kind-registry:5000`; pod ready in 4 to 5 s; `GET http://dind-check.localhost/healthz` through ingress-nginx OK; cleaned up |
| `make cleanup-deploys HOURS=24` (dry run) | Platform identity in 881050425883; AWS: only `groundcrew-smoke:1` (task definition, kept); kind: `gc-whoami` kept (pinned), 0 to delete |

### Troubleshooting

| Symptom | Fix |
|---|---|
| `network kind not found` on `make dev KIND=1` | `make kind-up` first: the network exists only once kind has run |
| Worker: `x509: certificate is valid for ..., not dind` | dind needs `hostname: dind` (in `docker-compose.yml`); recreate it with `make dev KIND=1` |
| Worker: `open /certs/client/key.pem: permission denied`, or `dind-certs` exited non-zero | `docker compose logs dind-certs`; it copies the client certificate only after dind is healthy. `docker compose up -d dind-certs` reruns it |
| `ErrImagePull` for `kind-registry:5000/...`, or `http: server gave HTTP response to HTTPS client` | The node has no mirror for that name: `make kind-up` again |
| `ErrImagePull` for `localhost:5001/...` | Not pushed (`curl -s localhost:5001/v2/_catalog`), or the registry is off the kind network: `make kind-up` again |
| `Cannot resolve the cluster address https://groundcrew-control-plane:6443` | Groundcrew is not on the kind network: `make down && make dev KIND=1`; register `.kind/groundcrew-deployer.kubeconfig`, not the `.host` one |
| `ValidatingAdmissionPolicy 'groundcrew-deployer-scope' ... denied` | Working as intended: a write outside `gc-*`, a namespace without `project=groundcrew`, or a RoleBinding to anything but `gc-namespace-admin` |
| `http://<app>.localhost` returns 404 from nginx | The Ingress needs `ingressClassName: nginx` and host `<app>.localhost`; give ingress-nginx a few seconds |
| `cleanup-deploys` shows `kind ... skipped` | No `.kind/groundcrew-deployer.host.kubeconfig` (`make kind-kubeconfig`), or the cluster is down |
| A namespace you want to keep is listed as `DELETE` | Pin it: `kubectl --context kind-groundcrew label namespace <gc-name> groundcrew/keep=true` |

More (ports in use, inotify limits, a revoked token): `docs/kind.md`, Troubleshooting.

## AWS: `make smoke-ecs`

Proves the `groundcrew-deploy` role (connect stack `Tier=deploy`, `docs/phase4-infra.md`) can
deploy a container end to end, and that AWS itself refuses what it must not do. Run it after
creating or updating a connect stack, and before the ECS target relies on a connection.

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
released), log group, ECR repository (force), secret (no recovery window; AWS removes it
asynchronously, so cleanup waits up to a minute until `DescribeSecret` says it is gone, and the
leftover check ignores a secret already marked deleted). Each delete checks
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

### First live run (2026-10-09, demo account 881050425883)

All 25 steps passed: the 7 read-only checks; push, log group, security group (tcp/80 from one
/32), secret, task definition, service stable in 37 s, HTTP 200, `TEST_VAR` hash matched; AWS
refused all five negative checks (`AccessDeniedException` for a, b, d; `UnauthorizedOperation`
for c; `AccessDenied` for e; the code allowlist also refused a and e); cleanup deleted
everything (the service took 111 s to go inactive). The service ran 163 s; estimated cost
$0.0008. No IAM gap: the template needed no change. A dry run right after found the
force-deleted secret still describable for a moment, which led to the wait described above.

### Cost

About $0.003 for a 10-minute run: a 0.25 vCPU / 0.5 GB Fargate task ($0.04048 per vCPU-hour,
$0.004445 per GB-hour, us-east-2 list prices; estimates) plus its public IPv4 ($0.005/hour), and a
secret kept for minutes ($0.40 per secret-month, prorated). ECR storage and log ingestion for
one small image and a few lines round to $0. The script prints its own estimate from how long
the service ran.

## After either: `make cleanup-deploys`

```bash
make cleanup-deploys HOURS=24              # dry run (read only): AWS, every deploy-tier connection, and kind
make cleanup-deploys HOURS=24 APPLY=1      # delete the DELETE rows
make cleanup-deploys HOURS=0 APPLY=1       # everything tagged project=groundcrew, whatever its age
```

It stops unless Groundcrew's platform identity is in `AWS_ACCOUNT` (default 881050425883), checked
before any role is assumed. It deletes only resources tagged `project=groundcrew` and older than
`HOURS`, never anything pinned (`groundcrew:keep=true`, `groundcrew/keep=true`), and never a
task definition. Security groups go only when unattached and their run is provably old. The rules
and what each resource needs: `docs/phase4-infra.md`, Cleanup.
