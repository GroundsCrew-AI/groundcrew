# Phase 4 deploy infrastructure: the contract

For Ahmed's Builder, targets, and Deployer. Deploy code reads every name, prefix, and tag
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
| `GC_KIND_REGISTRY` | `localhost:5001` | Where images for the local kind cluster are pushed | `make kind-up` (the nodes pull the same name through a registry mirror) |
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
  `groundcrew-read-only` stack (Update, Replace existing template with the current
  `aws-connect.yaml`, `Tier=deploy`), then **Enable deploys** on the connection, which verifies
  the deploy role before Groundcrew uses it (`POST /connections/{id}/aws/tier`).
- **Off:** **Turn off deploys** stops Groundcrew using the role at once; update the stack with
  `Tier=observe` to delete the cluster and both roles (delete deployed services first; cleanup
  does that).

### Names a deploy uses

`task_family(app)` gives `groundcrew-<app>` (the only families `ecs:RunTask` allows), next to
`ecr_repository`, `log_group`, and `ecs_service`. `secret_name(app, var)` gives
`groundcrew/<app>/<VAR>` for an env secret: Ahmed's ECS target creates it with
`CreateSecret` (tags `project=groundcrew` and `groundcrew:deploy=<run_id>`), updates it with
`PutSecretValue`, and references it in the task definition's `secrets` (`valueFrom` = the ARN);
ECS reads it with the task execution role. `deploy_role_arn(account)` gives the role ARN.

### Cleanup plan (deliverable 4)

`make cleanup-deploys` deletes services, security groups, log groups, ECR repositories, and
`groundcrew/*` secrets tagged `project=groundcrew`. It **does not deregister task definitions**:
the deploy role cannot (see above), they cost nothing, and leaving them keeps every revision
available for a rollback. Deleted secrets use Secrets Manager's recovery window (default 7 days)
unless cleanup is told otherwise.

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

## What lands later in this branch

`make cleanup-deploys` and the cost per live deploy (deliverable 4), CI (deliverable 5), and the
live runbook `docs/deploy-live.md` (deliverable 6). The deploy tier is written and tested
offline; no stack has been created or updated with it yet.
