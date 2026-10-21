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

## Accounts and sign-in

Anyone can create an account: with an email address and a password, or with Google or GitHub. The
web app shows a button for each provider whose OAuth credentials are set (`AUTH_GOOGLE_ID` /
`AUTH_GOOGLE_SECRET`, `AUTH_GITHUB_ID` / `AUTH_GITHUB_SECRET` or the GitHub App's client). The
callback URLs are `<web origin>/api/auth/callback/google` and `.../github`.

- Password accounts confirm their address from an emailed link before they can sign in; the link
  asks for the password too, so a link someone else's sign-up sent you does nothing. Forgotten
  passwords are reset from an emailed link, which also lets Google and GitHub accounts add one.
- Changing or resetting a password signs that account out everywhere.
- A Google or GitHub sign-in whose email the provider verified joins the account with that
  confirmed address. If the address was never confirmed, the provider's proof wins: the unconfirmed
  password is removed.
- Ten wrong passwords in a row lock an account for 15 minutes (`AUTH_LOCKOUT_ATTEMPTS`,
  `AUTH_LOCKOUT_MINUTES`); the sign-in endpoints are also rate limited per client address.
- Owners invite people by email (they get an email; they join once the address is confirmed) or
  by GitHub login.

Email needs SMTP: set `SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURITY` (`starttls`, `ssl`, or `none`),
`SMTP_USERNAME`, `SMTP_PASSWORD`, and `EMAIL_FROM`. Any provider with SMTP works (SES, Postmark,
SendGrid, Mailgun, your own relay). In dev without `SMTP_HOST`, the API prints each email with its
link to stderr. To run with provider sign-in only, set `PASSWORD_AUTH_ENABLED=false`.

## Notifications

Each workspace tells its team when an incident opens (not when a signal joins an open incident,
never for test incidents), when Medic waits for an approval, and when an incident resolves or
fails. An owner sets it up in Settings, Notifications; members see it read-only.

- **Slack**: in Slack, add the Incoming Webhooks app (or create an app with incoming webhooks),
  pick the channel, and paste the `https://hooks.slack.com/services/...` URL. It is stored
  encrypted with `SECRETS_KEY`, never shown again (the page says "Saved"), and masked in logs.
- **Email**: to every member, owners only (the default), or nobody. Only confirmed addresses get
  mail, through the same SMTP settings as above.
- **Events**: each of the four can be turned off. **Send test** posts a sample to every channel
  that is set up and shows what each one answered.

Messages carry the incident title, severity, affected resources, a one-line summary (the root
cause once Medic knows it), and a link to the incident page; approval messages add each fix and
its plan (before and after). They never carry approve or reject links: mail security scanners
and link previews open every link they see, so approving stays a signed-in action in the app,
on the incident's approval card. Sending happens in worker jobs (`notify`, up to 5 attempts with
backoff), so a slow or broken Slack or SMTP never holds up Medic or the incident watcher.
Approvals of deploy plans are not notified yet.

## Required in production

With `APP_ENV=staging` or `production`, the API and worker refuse to start until these are set:

| Setting | Why |
| --- | --- |
| `DATABASE_URL` | Postgres with pgvector |
| `SECRETS_KEY` | Fernet key for stored secrets (`make keygen`) |
| `INTERNAL_JWT_SECRET` | 32+ characters, shared with the web app |
| `APPROVAL_TOKEN_KEY` | Signs single-use approved-action tokens |
| `WEB_ORIGIN` | The public web URL (not localhost): CORS and links in emails |
| `SMTP_HOST` (and the other `SMTP_*`) | Confirmation and reset emails, when `PASSWORD_AUTH_ENABLED=true` |

They also refuse to start with any local development switch set: `LIVE_SCRIPT_SELF_APPROVE`,
`PIPELINE_FAKE`, `K8S_ALLOW_PRIVATE_SERVERS`, `DEPLOY_FIXTURES_DIR`. Each of those, the dev evidence
endpoint, deploy fixtures, the e2e test user, and the local kind cluster as a deploy target are
also refused where they are used (`tests/test_production_lockout.py`).

Sign-in on a hosted instance is GitHub only until email links are set up: email and password and
Google are off unless `PASSWORD_AUTH_ENABLED=true` or `GOOGLE_AUTH_ENABLED=true` (set the latter for
the web app too).

The web app needs `AUTH_SECRET`, `INTERNAL_JWT_SECRET`, and `API_INTERNAL_URL`, plus the OAuth
credentials of the providers you offer. `GET /ready` answers 200 once the database is reachable
and migrated to the latest revision.

## Other settings

- `SECRETS_KEY` (`make keygen`): Fernet key for stored secrets (Vercel tokens, kubeconfigs, Azure client
  secrets). They are decrypted only inside the calls that use them.
- `DISCOVERY_REGIONS` (default `us-east-2`): the regions discovery may call. IAM and STS are global.
- `MAX_RESOURCES_PER_CONNECTION` (default 2000) and `DISCOVERY_TIMEOUT_SECONDS` (default 100).
