# Phase 4 deploy infrastructure: the contract

For the Builder, the targets, and the Deployer (`app/sandbox/`, `app/targets/`,
`app/agents/builder/`, `app/agents/deployer/`). Deploy code reads every name, prefix, and tag
below from `Settings` (`app/config.py`) and the helpers in `app/connectors/deploy_infra.py`,
never from literals. The IAM policy (`infra/aws-connect.yaml`, `Tier=deploy`) and the kind
admission policy (`infra/kind/`) allow exactly these names, so a resource named or tagged any
other way is refused by AWS or Kubernetes, and `make cleanup-deploys` never sees it.

## Settings

| Env var | Default | What it is | Where the value comes from |
|---|---|---|---|
| `GC_AWS_REGION` | `us-east-2` | Region every AWS deploy runs in | The demo account's org policy allows workloads only in us-east-2 |
| `GC_ECS_CLUSTER` | `groundcrew-deploys` | Fargate cluster for deploys | Created by the connect stack with `Tier=deploy` (output `ClusterName`) |
| `GC_TASK_EXECUTION_ROLE_ARN` | unset: `arn:aws:iam::<account>:role/groundcrew-task-execution` | Role ECS uses to pull images and write logs | Created by the stack (output `TaskExecutionRoleArn`); the name is fixed, so the ARN follows from the connection's account |
| `GC_SUBNET_IDS` | unset: the default VPC's default subnets | Subnets for Fargate tasks (comma separated) | `resolve_subnets()`; set it when the account has no default VPC |
| `GC_LOG_GROUP_PREFIX` | `/groundcrew/` | CloudWatch log group prefix | Fixed; the deploy role may only create `/groundcrew/*` |
| `GC_ECR_PREFIX` | `groundcrew-` | ECR repository prefix | Fixed; the deploy role may only create `groundcrew-*` |
| `GC_KIND_REGISTRY` | `localhost:5001` (host); `kind-registry:5000` for the worker in `make dev KIND=1` | Where images for the local kind cluster are pushed, and the name pod specs use | `make kind-up` (the nodes pull both names from the same registry through containerd mirrors) |
| `GC_NAMESPACE_PREFIX` | `gc-` | Kubernetes namespace prefix | Fixed; the admission policy only allows the deployer to create `gc-*` |

The validators in `Settings` refuse values the policies would deny: a cluster or ECR prefix
not starting with `groundcrew-`, a log prefix not under `/groundcrew/`, a namespace prefix not
starting with `gc-`, or a task execution role other than `groundcrew-task-execution`.

## Names (`app/connectors/deploy_infra.py`)

App names are slugged once (lowercase letters, digits, hyphens; DNS-1123 is the strictest of
the three rules), so one app gets matching names everywhere:

| Helper | For app `Shop API` |
|---|---|
| `ecr_repository(settings, app)` | `groundcrew-shop-api` |
| `log_group(settings, app)` | `/groundcrew/shop-api` |
| `namespace(settings, app)` | `gc-shop-api` (cut to 63 characters) |
| `ecs_service(app)` | `shop-api`, in `GC_ECS_CLUSTER` |
| `task_family(app)` | `groundcrew-shop-api` (the deploy role only runs `groundcrew-*` families) |
| `task_execution_role_arn(settings, account_id)` | the setting, or the fixed name in that account |

## Tags (required)

Every resource a deploy creates carries both tags. The IAM conditions and cleanup depend on them.

| Where | Helper | Value |
|---|---|---|
| AWS (ECS services and task definitions, ECR repositories, security groups, log groups) | `aws_tags(run_id)` | `project=groundcrew`, `groundcrew:deploy=<run_id>` |
| Kubernetes (namespace and every object in it) | `k8s_labels(run_id)` | `project=groundcrew`, `groundcrew/deploy=<run_id>` (a label name cannot contain `:`) |

`required_tags(run_id)` returns the plain dict. Pass the tags **on create** (`tags=` on
`CreateService` / `RegisterTaskDefinition` / `CreateRepository`, `TagSpecifications` on
`CreateSecurityGroup`, `tags` on `CreateLogGroup`): the deploy role only allows those calls with
`project=groundcrew` in the request, and only allows tagging as part of a create.

Optional: `groundcrew:keep=true` (AWS) / `groundcrew/keep=true` (Kubernetes), constants
`KEEP_TAG` / `K8S_KEEP_LABEL`, pins a resource so `make cleanup-deploys` never deletes it. Like
the other tags, it can only be set on create.

## Network

`resolve_subnets(session, settings)` returns the subnets for `awsvpcConfiguration`:

- `GC_SUBNET_IDS` set: those subnets, after checking they exist in `GC_AWS_REGION`.
- Otherwise the default VPC's default subnets (`assignPublicIp=ENABLED`, like the demo).
- No default VPC, or one with no default subnets: `DeployInfraError` saying to run
  `aws ec2 create-default-vpc --region <region>` or set `GC_SUBNET_IDS`.

It only calls `ec2:DescribeVpcs` and `ec2:DescribeSubnets`, so it works on the read-only
session and on the deploy session. The demo account (881050425883) has a default VPC in
us-east-2 (`vpc-076ec4ac924fd464d`, three default subnets with public IPs; checked
2026-10-09).

## Credentials

- `CredentialsProvider.aws_session(connection_id)`: always the read-only role
  (`groundcrew-read-only`), as today.
- `CredentialsProvider.aws_deploy_session(connection_id)`: the `groundcrew-deploy` role, in
  `GC_AWS_REGION`, guarded by `AWS_DEPLOY_OPERATIONS`. Raises `CredentialsError` unless the
  connection's tier is `deploy`. Proposed contract change (`docs/phase3-interfaces.md`).
- `CredentialsProvider.kubeconfig(connection_id)`: a 0600 temp file with the connection's
  kubeconfig (verified live on kind). For kind that is the `groundcrew-deployer` ServiceAccount
  (never cluster-admin), registered with the Connections page or `make kind-register`
  (`docs/kind.md`).

## AWS deploy tier (`infra/aws-connect.yaml`, `Tier=deploy`)

The connect stack has a `Tier` parameter. `observe` (the default) renders exactly the template
from before tiers existed (`tests/test_aws_connect_template.py` compares the rendered resources
with a snapshot of it). `deploy` adds three resources and changes none:

| Resource | What it is |
|---|---|
| `groundcrew-deploys` | ECS cluster, Fargate and Fargate Spot capacity providers, Container Insights off, tagged `project=groundcrew`. Free until something runs |
| `groundcrew-task-execution` | Role ECS assumes for a deploy's tasks (trust: `ecs-tasks.amazonaws.com` from this account only). Pulls `groundcrew-*` images, writes `/groundcrew/*` logs, and reads `groundcrew/*` secrets so ECS can inject them when a task starts. Nothing else |
| `groundcrew-deploy` | Role Groundcrew deploys with. Same trust as `groundcrew-read-only` (the platform principal, with this workspace's ExternalId), 1-hour sessions |

New outputs: `DeployRoleArn`, `ClusterName`, `TaskExecutionRoleArn`. The platform identity
(`groundcrew-platform`, `sts:AssumeRole` on `role/groundcrew-*`) needs no change.

### What `groundcrew-deploy` may do

Every Allow, with its scope (`tests/test_aws_connect_template.py` fails if an action is added or
a scope widens). `<r>` and `<a>` are the stack's region and account.

| Action | Resource | Condition |
|---|---|---|
| `ecs:CreateService`, `UpdateService`, `DeleteService`, `DescribeServices` | `service/groundcrew-deploys/*` | |
| `ecs:DescribeClusters` | the `groundcrew-deploys` cluster | |
| `ecs:ListServices`, `ListTasks` | `*` | `ecs:cluster` = `groundcrew-deploys` |
| `ecs:RunTask` | `task-definition/groundcrew-*:*` | `ecs:cluster` = `groundcrew-deploys` |
| `ecs:StopTask`, `DescribeTasks` | `task/groundcrew-deploys/*` | |
| `ecs:RegisterTaskDefinition` | `*` | request tag `project=groundcrew` |
| `ecs:TagResource` | `*` | `ecs:CreateAction` in CreateService / RegisterTaskDefinition / RunTask, and request tag `project=groundcrew` |
| `ecs:DescribeTaskDefinition`, `ListTaskDefinitions` | `*` | none (read-only, see below) |
| `ecr:GetAuthorizationToken` | `*` | none (see below) |
| `ecr:CreateRepository`, `ecr:TagResource` | `repository/groundcrew-*` | request tag `project=groundcrew` |
| `ecr:` push and pull (`BatchCheckLayerAvailability`, `InitiateLayerUpload`, `UploadLayerPart`, `CompleteLayerUpload`, `PutImage`, `BatchGetImage`, `GetDownloadUrlForLayer`), `DescribeRepositories`, `DescribeImages`, `ListImages`, `BatchDeleteImage`, `DeleteRepository` | `repository/groundcrew-*` | |
| `ec2:DescribeVpcs`, `DescribeSubnets`, `DescribeSecurityGroups`, `DescribeSecurityGroupRules`, `DescribeNetworkInterfaces` | `*` | none (see below) |
| `ec2:CreateSecurityGroup` | `security-group/*` | request tag `project=groundcrew` |
| `ec2:CreateSecurityGroup` | `vpc/*` in `<r>` / `<a>` | none (see below) |
| `ec2:CreateTags` | `security-group/*` | `ec2:CreateAction` = CreateSecurityGroup |
| `ec2:AuthorizeSecurityGroupIngress`, `RevokeSecurityGroupIngress`, `DeleteSecurityGroup` | `security-group/*` | resource tag `project=groundcrew` |
| `logs:CreateLogGroup`, `DeleteLogGroup`, `PutRetentionPolicy`, `TagResource`, `TagLogGroup`, `FilterLogEvents` | `log-group:/groundcrew/*` | |
| `logs:DescribeLogGroups` | `*` | none (see below) |
| `secretsmanager:CreateSecret` | `secret:groundcrew/*` | request tag `project=groundcrew` |
| `secretsmanager:PutSecretValue`, `TagResource`, `DeleteSecret`, `DescribeSecret` | `secret:groundcrew/*` | |
| `iam:PassRole` | `role/groundcrew-task-execution` | `iam:PassedToService` = `ecs-tasks.amazonaws.com` |
| `tag:GetResources` | `*` | none (see below) |
| **Deny** `secretsmanager:GetSecretValue`, `ssm:GetParameter*`, `kms:Decrypt`, `s3:GetObject`, `s3:GetObjectVersion` | `*` | explicit, same as the read-only role |

Not granted on purpose: `ecs:DeregisterTaskDefinition` (it cannot be scoped to Groundcrew's
families, task definitions cost nothing, and a rollback uses the previous revision), and
`secretsmanager:GetSecretValue` (the Deny above; Groundcrew writes a deploy's secrets but never
reads them back). **The deploy role has no unscoped write action**: every action that is `*`
with no condition only reads (a test enforces it).

`groundcrew-task-execution` may do exactly this:

| Action | Resource |
|---|---|
| `ecr:GetAuthorizationToken` | `*` (registry login) |
| `ecr:BatchCheckLayerAvailability`, `BatchGetImage`, `GetDownloadUrlForLayer` | `repository/groundcrew-*` |
| `logs:CreateLogStream`, `PutLogEvents` | `log-group:/groundcrew/*` |
| `secretsmanager:GetSecretValue` | `secret:groundcrew/*` (ECS injects them at task start; Groundcrew never assumes this role) |

Secrets use the AWS managed key (`aws/secretsmanager`), so neither role needs `kms:Decrypt`.

### What cannot be scoped, and why

| Action | Why it is `*` | What it means |
|---|---|---|
| `ecs:DescribeTaskDefinition`, `ecs:ListTaskDefinitions` | No resource-level permissions | Read-only: task definition contents (env var values included) are already readable by the read-only role's Describe |
| `ecs:RegisterTaskDefinition` (a write) | No resource-level permissions | Not unscoped: it needs the request tag `project=groundcrew`. IAM does not check the family name, but `ecs:RunTask` only runs `groundcrew-*` families |
| `ecr:GetAuthorizationToken` | Account-wide by design (it returns a registry login, not repository access) | The token alone grants nothing: push and pull are limited to `repository/groundcrew-*` |
| `ec2:Describe*` | EC2 Describe calls do not support resource-level permissions | Read-only network metadata |
| `ec2:CreateSecurityGroup` on `vpc/*` (a write) | CreateSecurityGroup is also authorized against the VPC; the default VPC's ID differs per account and is unknown when the stack is written | Not unscoped: the same call is also checked against the new group, which must carry `project=groundcrew`; the role cannot change any group without that tag |
| `logs:DescribeLogGroups` | Authorized against all log groups | Read-only names and settings |
| `tag:GetResources` | The tagging API has no resource-level permissions | Read-only; cleanup uses it to find `project=groundcrew` resources |

Checked on 2026-10-09 without changing anything in AWS: `cfn-lint` passes for both tiers; IAM
Access Analyzer `ValidatePolicy` reports no findings for either role's permission or trust
policy; `SimulateCustomPolicy`:

| Role | Action | Resource | Result |
|---|---|---|---|
| deploy | `secretsmanager:CreateSecret` | `groundcrew/x`, tagged `project=groundcrew` | allowed |
| deploy | `secretsmanager:CreateSecret` | `groundcrew/x`, no tag | denied |
| deploy | `secretsmanager:CreateSecret` | `checkout/x`, tagged | denied |
| deploy | `secretsmanager:GetSecretValue` | `groundcrew/x` | denied (explicit) |
| task-execution | `secretsmanager:GetSecretValue` | `groundcrew/x` | allowed |
| task-execution | `secretsmanager:GetSecretValue` | `checkout/database-url` | denied |
| deploy | `secretsmanager:PutSecretValue` / `DeleteSecret` / `TagResource` | `checkout/database-url` | denied |
| deploy | `ecs:DeregisterTaskDefinition` | any | denied |
| deploy | ECS, ECR, EC2, Logs, PassRole on `groundcrew-*` / tagged resources | | allowed |
| deploy | the same on the demo's (`service/checkout/*`, `checkout-api` task definition, repository, log group, untagged security groups, `checkout-api-task-execution`), RDS, IAM | | denied |
| task-execution | `ecr:BatchGetImage` | `checkout-api` / `groundcrew-shop` | denied / allowed |

### Turning deploys on or off

- **New account:** choose **Deploy** on the AWS card of the Connections page before **Connect
  AWS**. The Quick-create link carries `Tier=deploy`; Verify checks both roles.
- **Connected account:** Quick-create can only create stacks, so update the existing
  `groundcrew-read-only` stack: Update, Replace existing template, Amazon S3 URL (the
  **Enable deploys** dialog shows `AWS_CONNECT_TEMPLATE_URL` with a Copy button, from
  `GET /connections/aws/template-url`; without it, the dialog offers `aws-connect.yaml` to
  upload), then `Tier=deploy`. Back in the dialog, **Verify** checks the deploy role before
  Groundcrew uses it (`POST /connections/{id}/aws/tier`).
- **Off:** **Turn off deploys** stops Groundcrew using the role at once; update the stack with
  `Tier=observe` to delete the cluster and both roles (delete deployed services first; cleanup
  does that).

### Names a deploy uses

`task_family(app)` gives `groundcrew-<app>` (the only families `ecs:RunTask` allows), next to
`ecr_repository`, `log_group`, and `ecs_service`. `secret_name(app, var)` gives
`groundcrew/<app>/<VAR>` for an env secret: the ECS target creates it with
`CreateSecret` (tags `project=groundcrew` and `groundcrew:deploy=<run_id>`), updates it with
`PutSecretValue`, and references it in the task definition's `secrets` (`valueFrom` = the ARN);
ECS reads it with the task execution role. `deploy_role_arn(account)` gives the role ARN.

### Cleanup (`make cleanup-deploys`)

Deletes what deploys left behind (`app/connectors/deploy_cleanup.py`,
`scripts/cleanup_deploys.py`). **Dry run by default**; nothing is deleted without `APPLY=1`.

```bash
make cleanup-deploys HOURS=24                   # dry run: what would go, and why the rest stays
make cleanup-deploys HOURS=24 APPLY=1           # delete it
make cleanup-deploys HOURS=24 CONNECTION=<id>   # one AWS connection (default: every deploy-tier one)
```

It goes through `aws_deploy_session()` for each connection: Groundcrew's platform identity assumes
`groundcrew-deploy` (never a profile or `~/.aws`). Before any role is assumed it checks that the
platform identity is in `AWS_ACCOUNT` (default `881050425883`, the demo account), and it skips
connections in any other account. Then it checks that each session is that role, in the
connection's account and `GC_AWS_REGION`. `HOURS=0` means everything tagged, whatever its age. The dry run, and the
listing step of `APPLY=1`, use a separate copy of the session that may only call operations in
both `AWS_DEPLOY_OPERATIONS` and `AWS_READ_OPERATIONS`, so a bug cannot delete anything during
the listing. kind uses the deployer kubeconfig (`.kind/groundcrew-deployer.host.kubeconfig`),
never cluster-admin; without it, kind is skipped.

**Only resources tagged `project=groundcrew` and older than `HOURS`:**

| What | Found by | Age from | Delete |
|---|---|---|---|
| ECS services in `GC_ECS_CLUSTER` | `ListServices`, `DescribeServices` (tags) | `createdAt` | Scale to 0, delete, wait until inactive |
| Security groups | `DescribeSecurityGroups` filtered on `tag:project=groundcrew` | **their run**, see below | Only once no network interface uses it (waits up to 3 minutes after the services go) |
| Log groups under `GC_LOG_GROUP_PREFIX` | `DescribeLogGroups`, tags from `tag:GetResources` | `creationTime` | Delete |
| ECR repositories `GC_ECR_PREFIX*` | `tag:GetResources`, then `DescribeRepositories` by name | `createdAt` | Delete with its images (force) |
| Secrets `groundcrew/*` | `tag:GetResources`, then `DescribeSecret` (never the value) | `CreatedDate` | Force delete (no recovery window), then wait until `DescribeSecret` says it is gone |
| Namespaces `gc-*` in kind | list with label `project=groundcrew` | `creationTimestamp` | Delete, wait until gone |

**Pinned resources are always kept**, whatever their age, and shown as `keep (pinned)`:

| Where | Pin | Who can set it |
|---|---|---|
| AWS (service, security group, log group, repository, secret) | tag `groundcrew:keep=true` | The deploy role can tag only when it creates a resource, so a deploy that should stay sets it on create. Pinning an existing resource is done by the account owner (console or `aws ... tag-resource`) |
| Kubernetes namespace | label `groundcrew/keep=true` | The deploy target on create (the admission policy allows any extra label), or a cluster admin: `kubectl --context kind-groundcrew label namespace <gc-name> groundcrew/keep=true` |

Before each delete, `APPLY=1` checks again and leaves alone anything pinned since the dry run
(service, security group, secret, namespace). `gc-whoami` in the local kind cluster (a
`whoami` app from the Phase 3 discovery check) is pinned.

**Security groups** have no creation time in EC2. A group goes only when its
`groundcrew:deploy` run is provably older than `HOURS`: another resource of the same run (a
service, log group, repository, secret, or task definition) was created before the cutoff.
A group with no run tag, or whose run has nothing left to prove its age, stays and the dry run
says why. A group without `project=groundcrew` is never even listed.

**Task definitions are never deregistered:** the deploy role cannot (see above), they cost
nothing, and every revision stays available for a rollback. Cleanup lists the `groundcrew-*`
ones as `keep` and uses them as evidence of a run's age.

Before each delete, `APPLY=1` checks again (the service's and secret's own tags, the group's
attachments). If the resource is no longer ours or still in use, it is left alone (`SKIP`). A
failed delete (`FAIL`) does not stop the rest, and the exit code is non-zero.

The dry run found, and the code fixed, two IAM scopes: listing every ECR repository is
`repository/*`, and describing a tagged secret outside `groundcrew/` is refused. Cleanup
describes only `groundcrew-*` repositories and `groundcrew/*` secrets by name. The demo's
`checkout-*` task definitions and security groups are tagged `project=groundcrew` as well.
Their names and the missing `groundcrew:deploy` tag keep them out of cleanup.

### Cost of a live deploy

us-east-2 on-demand list prices from the AWS Price List API (offer files published
2026-09-11 for ECS, ECR and Secrets Manager; 2026-09-17 for VPC; 2026-10-07 for CloudWatch;
read 2026-10-11). Totals are estimates: real bills round per second (Fargate, with a 1-minute
minimum) and add data transfer.

| Item | Price | Notes |
|---|---|---|
| Fargate vCPU | $0.04048 per vCPU-hour | Linux/x86 |
| Fargate memory | $0.004445 per GB-hour | |
| Fargate ephemeral storage | $0.000111 per GB-hour above the free 20 GB | |
| Fargate Spot | **not confirmed:** variable, not in the price list | AWS advertises up to 70% off; the cluster offers it, deploys do not use it yet |
| Public IPv4 address | $0.005 per hour (in use or idle) | One per task with `assignPublicIp=ENABLED` |
| Secrets Manager | $0.40 per secret-month (prorated), $0.05 per 10,000 API calls | |
| ECR storage | $0.10 per GB-month | |
| CloudWatch Logs | $0.50 per GB ingested, $0.03 per GB-month stored | |
| ECS cluster, security groups, task definitions, IAM roles | $0 | |
| Data transfer out | **not confirmed here:** see AWS's EC2 data transfer pricing | Small for a smoke test |

Examples (estimates):

| Deploy | Per hour | Per day | Per 30 days |
|---|---|---|---|
| 0.25 vCPU / 0.5 GB, public IP (the smoke test's task) | $0.0173 | $0.42 | $12.49 |
| 0.5 vCPU / 1 GB, public IP | $0.0297 | $0.71 | $21.37 |
| 1 vCPU / 2 GB, public IP | $0.0544 | $1.30 | $39.15 |
| Plus per deploy, while it exists | 1 secret: $0.40/month; a 200 MB image: $0.02/month; logs: $0.50/GB | | |

A deploy someone forgets costs about $12 a month at the smallest size. That is why
`make cleanup-deploys` exists. The smoke test's 163-second run cost about $0.0008.

## Kubernetes namespaces

The deployer ServiceAccount can create only `gc-*` namespaces and can only bind the
`gc-namespace-admin` ClusterRole inside them. After creating a namespace, the k8s target
creates one RoleBinding in it before anything else:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata: {name: groundcrew-deployer, namespace: gc-<app>}
roleRef: {apiGroup: rbac.authorization.k8s.io, kind: ClusterRole, name: gc-namespace-admin}
subjects: [{kind: ServiceAccount, name: groundcrew-deployer, namespace: groundcrew-system}]
```

This protection needs Kubernetes 1.30 or later (ValidatingAdmissionPolicy is GA from 1.30).
The kind node image is pinned to v1.37; customer clusters older than 1.30 do not get the
namespace restriction. The namespace needs label `project=groundcrew` (from `k8s_labels`), or
the admission policy refuses it. Locally, `make kind-up` sets all of this up; register
`.kind/groundcrew-deployer.kubeconfig` (from `make kind-kubeconfig`) as the Kubernetes
connection, and push images to `GC_KIND_REGISTRY`. The full setup is in `docs/kind.md`.

## Build daemon (local dev)

Sandbox builds (`app/sandbox/`, used by the Builder) run in the `dind` Compose service (`docker:29.7.2-dind`), a
Docker daemon of its own. The worker reaches it over TLS, and **no service mounts the host's
`/var/run/docker.sock`** (`tests/test_compose_dind.py` fails if one does).

| | |
|---|---|
| Worker env | `DOCKER_HOST=tcp://dind:2376`, `DOCKER_TLS_VERIFY=1`, `DOCKER_CERT_PATH=/certs/client`, `SANDBOX_SMOKE_HOST=dind` (a container the Builder starts with `-p`/`-P` answers at `dind:<port>`, not localhost) |
| TLS | dind generates a CA, its server certificate (for hostname `dind`), and a client certificate on start (into `dind-certs-issued`, key mode 0644, reset on every start). The one-shot `dind-certs` service then copies the client certificate into `dind-certs-client`, owned by the worker's uid 1000, key 0600, directory 0700; the worker mounts it read-only and starts only after the copy succeeded. `dind-certs-ca` keeps the CA across restarts, so the copy stays valid when dind restarts alone. Plain TCP (2375) is refused |
| Network | `default` only; with `make dev KIND=1` also `kind`, so it can push to `kind-registry:5000`. **No published ports**: nothing outside the Compose network can reach it |
| Registry | `infra/dind/daemon.json`: `insecure-registries` is only `kind-registry:5000` (plain HTTP, local). ECR pushes use HTTPS and the deploy role's registry token |
| Limits | `DIND_CPUS` (default 2), `DIND_MEMORY` (default `4g`), 2048 processes |
| Cache | Volume `dind-data` (`/var/lib/docker`): images and the BuildKit cache survive restarts; BuildKit prunes above 10 GB. `docker compose down -v` clears it |
| CLI | The api image carries the Docker CLI and buildx (same version, `docker:29.7.2-cli`), no daemon. Only the worker gets `DOCKER_HOST` |

Images for kind are tagged `$GC_KIND_REGISTRY/<repo>:<tag>`. In `make dev KIND=1` the worker's
`GC_KIND_REGISTRY` is `kind-registry:5000`; `make kind-up` adds a containerd mirror for that
name on the nodes (next to `localhost:5001`), so a pod spec uses the exact name the build
pushed. `make dind-check` proves the path live (`docs/deploy-live.md`).

**dind is privileged, and that is acceptable for local dev only.** A privileged container is
root on the host, and the client certificate controls that daemon. So whoever can read the
worker's `/certs/client` (uid 1000 in the worker, or root) effectively has the host.
**Hosted deployments (Phase 6) must run builds on a separate build host or in Nebius
Sandboxes, never in a privileged container beside the API or worker.** The Builder sees only the standard `DOCKER_*` variables and
`SANDBOX_SMOKE_HOST`, so moving builds out changes configuration, not code.

## Sandbox (`app/sandbox/`)

Builder builds, smoke-tests, and pushes only through the `Sandbox` protocol
(`get_sandbox(settings)`), so moving builds to a separate host or to Nebius is configuration.

| Setting | Default | What it is |
|---|---|---|
| `SANDBOX_PROVIDER` | `docker` | `docker` (the CLI against `DOCKER_HOST`), or `nebius` (stub: `TODO(nebius-sandbox)`) |
| `SANDBOX_SMOKE_HOST` | `localhost` | Where published smoke ports answer: `dind` for the Compose worker |
| `SANDBOX_BUILD_TIMEOUT_SECONDS` | `900` | Per build; the CLI is killed and BuildKit cancels the build |
| `SANDBOX_SMOKE_TIMEOUT_SECONDS` | `60` | How long the health path may take to answer 2xx/3xx |
| `SANDBOX_PUSH_TIMEOUT_SECONDS` | `600` | Per push |

- **build**: `docker build --progress=plain --platform linux/amd64` (Fargate and the kind nodes
  are x86_64) with labels `groundcrew=1` and `groundcrew.run=<run id>`. `BuildResult` has the
  last 8,000 characters of the log (redacted), the failing instruction parsed from BuildKit's
  output (`RUN pip install ...`), and the lines that look like the cause.
- **run_smoke**: the container gets `--cap-drop ALL`, `--security-opt no-new-privileges`,
  `--cpus 1`, `--memory 512m`, `--pids-limit 256`, a 64 MB tmpfs on `/tmp`, and is never
  privileged. Env values reach `docker run` through the CLI's environment (`--env NAME` with no
  value), never argv. The port is published on the daemon's host and polled at
  `SANDBOX_SMOKE_HOST`: the health path when Scout found one, else `/`; 2xx or 3xx passes. With
  a health path, `/` is probed once and recorded but does not decide (APIs often have no `/`;
  `legacy-flask-app` answers 404 there). On failure the container's last 200 log lines come
  back with secret values scrubbed. The container is removed on every path.
- **push**: `kind_target(settings, repo, tag)` (no login) or `ecr_target(session, repo, tag)`
  with a session from `aws_deploy_session()`. An ECR login goes into a temporary `DOCKER_CONFIG`
  through `--password-stdin`, the same way `scripts/smoke_ecs_deploy.py` does it, and the
  password is scrubbed from any error. Push runs only after approval, as the first step of
  apply; the ECR repository is created by the ECS target (M3) right before it.
- **cleanup(run_id)**: removes the run's labelled containers and images; the build cache stays.

Live checks (2026-10-11, dind 29.7.2 from the worker): `legacy-flask-app` with a naive
`python:3.12-slim` Dockerfile fails at `RUN pip install` with `Error: pg_config executable not
found` (78 s); with `gcc libc6-dev libpq-dev` added it builds (97 s), the smoke test passes
(`/health` 200, `/` 404 recorded), and the push to `kind-registry:5000` takes under a second.
One capture failed instead with a PyPI broken pipe while installing build dependencies
(`tests/fixtures/sandbox/pip-network-flake.log`): Builder must retry such failures without
spending a fix attempt.

## Targets (`app/targets/`)

A target takes an image that was built and smoke-tested in the sandbox to a running app. One
protocol for Kubernetes (`K8sTarget`) and AWS ECS (`EcsTarget`); `target_for(settings,
credentials, connection)` picks one (`k8s`, or `aws` with `Tier=deploy`).

| Call | What it does | Writes |
|---|---|---|
| `discover()` | What the target runs: `gc-*` namespaces and their objects (same external ids as discovery), or the services in `groundcrew-deploys` (by ARN) | no |
| `plan(spec)` | `TargetPlan`: apply steps in order, rendered manifests or task definition (secrets `********`), `previous_revision`; `plan_hash()` binds the approval | no |
| `preflight(plan)` | image built and smoke-tested, required env, port, names (exists and not Groundcrew's, or a namespace still terminating), ECS cluster active | no |
| `apply(plan, spec, token, sandbox)` | push first, then the steps; `spec` must match the plan (`PlanMismatchError`) | yes, with `target_apply` |
| `verify(deployment)` | rollout, then the health path and `/` over HTTP (`/` is recorded but does not decide when there is a health path) | no |
| `logs(deployment, since)` | pod logs, or CloudWatch `/groundcrew/<app>`, redacted | no |
| `rollback_plan(deployment)` | back to `previous_revision`, or delete a first deploy | no |
| `rollback(plan, token)` | runs that plan | yes, with `target_rollback` |

**Nothing leaves the sandbox before approval.** Preflight reads only. The first apply step is
the image push (on ECS, after creating the ECR repository), so the approval comes before the
image reaches any registry.

**Approved actions.** `apply()` and `rollback()` refuse to run without an `ApprovedAction` from
`app.policy.actions.issue_approved_action`: an approved, unused approval for the same run, with
tool name `target_apply` (policy write) or `target_rollback` (policy emergency) and
`arguments.plan_hash` equal to the plan's hash. Issuing marks the approval used; the target
consumes the token before its first write. No token, a reused token, a token for another plan,
run, or action, or a hand-made one all raise `PolicyViolationError`. Medic calls `rollback()`
the same way, with its own emergency approval. A rollback, including deleting a first deploy,
never runs on its own.

### Kubernetes

Credentials: `CredentialsProvider.kubeconfig()` (the `groundcrew-deployer` ServiceAccount).

1. push to `GC_KIND_REGISTRY/<app>:<tag>` (pods pull the same name)
2. Namespace `gc-<app>`, labels `project=groundcrew`, `groundcrew/deploy=<run>` (create only)
3. RoleBinding `groundcrew-deployer` to `gc-namespace-admin` (create only)
4. Secret `<app>-env` with the secret env vars (server-side apply, field manager `groundcrew`)
5. Deployment (readiness probe on the health path, or TCP on the port; 250m / 512Mi)
6. Service (port 80 to the app's port) and Ingress `<app>.GC_KIND_APP_DOMAIN` (class `nginx`)

The deployer cannot read Secrets before the RoleBinding exists, so a plan for a new namespace
does not look inside it. Writes right after the RoleBinding retry a 403 for a few seconds.
Verify waits for the rollout (stopping early on `ErrImagePull`, `ImagePullBackOff`,
`CreateContainerConfigError`, repeated `CrashLoopBackOff`, a `ReplicaFailure` condition, or
`ProgressDeadlineExceeded`), then sends requests to `GC_KIND_INGRESS_URL` with the app's
`Host` header. Rollback merges an earlier ReplicaSet's pod template back into the Deployment
(like `kubectl rollout undo`); deleting a first deploy deletes the namespace.

### AWS ECS

Credentials: `CredentialsProvider.aws_deploy_session()` only (guarded by
`AWS_DEPLOY_OPERATIONS`). Names from `deploy_infra` (`ecr_repository`, `log_group`,
`task_family`, `ecs_service`, `secret_name`), and the required tags on everything created.

1. ECR repository `groundcrew-<app>`, then push `<account>.dkr.ecr.<region>.amazonaws.com/groundcrew-<app>:<tag>`
2. log group `/groundcrew/<app>`, `GC_LOG_RETENTION_DAYS` retention
3. security group `groundcrew-<app>`: the app's port from `GC_INGRESS_CIDR` (0.0.0.0/0 for the demo)
4. Secrets Manager `groundcrew/<app>/<VAR>` (create or put; never read back)
5. task definition `groundcrew-<app>`, a new revision (0.25 vCPU / 0.5 GB, awslogs, secrets by ARN)
6. service `<app>` in `groundcrew-deploys` (create, or update with `forceNewDeployment`)

Task definitions are never deregistered: every revision stays available for a rollback.
Verify waits for the primary deployment's `rolloutState` (stopping on `FAILED`, with the stopped
tasks' reasons), finds the new task's public IP, and checks `http://<ip>:<port>`. Rollback
points the service at the previous task definition; deleting a first deploy scales the
service to 0 and deletes it (the image, log group, security group, and secrets stay for
`make cleanup-deploys`).

### Settings

| Setting | Default | What it is |
|---|---|---|
| `GC_INGRESS_CIDR` | `0.0.0.0/0` | Source of the ECS security group rule |
| `GC_KIND_APP_DOMAIN` | `localhost` | kind apps answer at `http://<app>.<domain>` |
| `GC_KIND_INGRESS_URL` | `http://127.0.0.1` | Where verify reaches ingress-nginx (`http://groundcrew-control-plane` from Compose with `KIND=1`) |
| `GC_VERIFY_TIMEOUT_SECONDS` | `300` | Rollout or stable-service wait |
| `GC_LOG_RETENTION_DAYS` | `7` | The app's CloudWatch log group |

### Checking it

- `pytest tests/test_targets.py tests/test_approved_action.py`: both targets on fakes (order of
  writes, tags, masking, tokens, verify, logs, rollback).
- `RUN_LIVE_TESTS=1 pytest tests/test_targets_kind_live.py -s` (after `make kind-up` and
  `make kind-kubeconfig`): build and smoke-test a small app, then plan, approve, apply, verify,
  logs, a second deploy, a rollback, and the delete, on the live kind cluster.
- `make plan-target CONNECTION=<id>`: the plan and preflight for a real connection, read-only.
  Run it and get an OK before any live ECS apply.

Live (2026-10-12, kind): first deploy verified in 8.0 s (rollout 1/1, `/health` 200), second
deploy 6.0 s, rollback to revision 1 verified, namespace deleted. ECS: dry run only
(`make plan-target`, account 881050425883): 7 apply steps, all preflight checks pass.
