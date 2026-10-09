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
- `CredentialsProvider.aws_deploy_session(connection_id)`: the `groundcrew-deploy` role. Raises
  a clear error unless the connection's tier is `deploy`. Proposed contract change, documented
  in `docs/phase3-interfaces.md`; it lands with the AWS deploy tier.
- `CredentialsProvider.kubeconfig(connection_id)`: for kind, the `groundcrew-deployer`
  ServiceAccount's kubeconfig from `make kind-kubeconfig` (never cluster-admin).

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

The AWS deploy tier and `aws_deploy_session` (deliverable 3), `make cleanup-deploys` and the
cost per live deploy (deliverable 4), and the live runbook `docs/deploy-live.md` (deliverable 6).
