# Self-hosting Groundcrew

What a server running Groundcrew needs before its users can connect their clouds. Everything here is a
one-time setup by whoever runs the server. Users then follow "Connecting your cloud" in the README.

## AWS: one platform identity

Groundcrew reads a customer's AWS account through a read-only role the customer creates
(`infra/aws-connect.yaml`). To use that role, Groundcrew needs one identity of its own. That identity can
do exactly one thing: assume roles named `groundcrew-*`, and each customer role also requires that
workspace's ExternalId. Groundcrew never stores a customer's AWS keys.

Create it with two commands, using an admin profile for the account that runs Groundcrew:

```bash
aws iam create-user --user-name groundcrew-platform --tags Key=project,Value=groundcrew

aws iam put-user-policy --user-name groundcrew-platform \
  --policy-name groundcrew-assume-customer-roles \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Sid": "AssumeCustomerReadOnlyRoles",
      "Effect": "Allow",
      "Action": "sts:AssumeRole",
      "Resource": "arn:aws:iam::*:role/groundcrew-*"
    }]
  }'
```

Then make it an access key (`aws iam create-access-key --user-name groundcrew-platform`) and set these in
`.env`:

| Variable | Value |
|---|---|
| `GROUNDCREW_AWS_ACCESS_KEY_ID` | the new key's `AccessKeyId` |
| `GROUNDCREW_AWS_SECRET_ACCESS_KEY` | the new key's `SecretAccessKey` (secret) |
| `GROUNDCREW_PLATFORM_PRINCIPAL_ARN` | `arn:aws:iam::<your account>:user/groundcrew-platform` |

Groundcrew builds its AWS session only from those two keys. It never reads AWS profiles, `AWS_PROFILE`,
or `~/.aws`, so other credentials on the machine are never used. The API and the worker both get them
through `env_file: .env` in `docker-compose.yml`.

**Hosted without keys.** On ECS, EC2, or anything else with a task or instance role, leave both keys empty
and give that role the same one-statement policy. Groundcrew then uses the default credential chain
(environment, container, or instance role), still without profiles or `~/.aws`. Set
`GROUNDCREW_PLATFORM_PRINCIPAL_ARN` to that role's ARN.

## AWS: the one-click template

The **Connect AWS** button opens a CloudFormation Quick-create link with the template, the stack name,
the role name, the ExternalId, and the platform identity filled in. CloudFormation only accepts templates
stored in S3, so the template has to be uploaded once, and again whenever `infra/aws-connect.yaml` changes:

```bash
AWS_PROFILE=<an admin profile for the account> make publish-connect-template
```

The target checks the account first (`CONNECT_TEMPLATE_ACCOUNT`, default 881050425883), uploads to
`s3://groundcrew-templates-<account>/aws-connect.yaml` in us-east-2, and prints the line to put in `.env`:

```
AWS_CONNECT_TEMPLATE_URL=https://groundcrew-templates-881050425883.s3.us-east-2.amazonaws.com/aws-connect.yaml
```

To create the bucket for another account, make it private, versioned, and tagged, then point
`CONNECT_TEMPLATE_ACCOUNT` / `CONNECT_TEMPLATE_BUCKET` at it:

```bash
B=groundcrew-templates-<account>; R=us-east-2
aws s3api create-bucket --bucket $B --region $R --create-bucket-configuration LocationConstraint=$R \
  --object-ownership BucketOwnerEnforced
aws s3api put-public-access-block --bucket $B --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-versioning --bucket $B --versioning-configuration Status=Enabled
aws s3api put-bucket-tagging --bucket $B --tagging 'TagSet=[{Key=project,Value=groundcrew}]'
```

**Limitation: a private bucket only works within its own account.** CloudFormation reads the template
with the permissions of whoever is signed in to the console. With a private bucket, the link works only
when the stack is created in the same account as the bucket (the demo setup). Serving customers in other
accounts needs a publicly readable template URL, which is a later task: publish from a dedicated
account that holds nothing but the template.

Without `AWS_CONNECT_TEMPLATE_URL`, Connect AWS says one-click setup is not available and shows the
AWS CLI command instead.

## GitHub App

Groundcrew reads repositories through its GitHub App with short-lived installation tokens. Set
`GITHUB_APP_ID` and `GITHUB_APP_PRIVATE_KEY_PATH` (keep the `.pem` in `secrets/`, which is git-ignored).
`docker-compose.yml` mounts `./secrets` read-only into the API and the worker, so a relative path such as
`secrets/groundcrew-app.pem` works on the host and in the containers. Sign-in uses the same app's OAuth
client; see "Sign-in setup" in the README.

## Other settings

- `SECRETS_KEY` (`make keygen`): Fernet key for stored secrets (Vercel tokens, kubeconfigs, Azure client
  secrets). They are decrypted only inside the calls that use them.
- `DISCOVERY_REGIONS` (default `us-east-2`): the regions discovery may call. IAM and STS are global.
- `MAX_RESOURCES_PER_CONNECTION` (default 2000) and `DISCOVERY_TIMEOUT_SECONDS` (default 100).
