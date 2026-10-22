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

**groundkrew.com** runs this way: the instance role `groundcrew-platform`
(`infra/platform/iam.tf`) replaces the IAM user, and
`GROUNDCREW_PLATFORM_PRINCIPAL_ARN=arn:aws:iam::881050425883:role/groundcrew-platform`. That ARN is
also the default of `GroundcrewPrincipalArn` in `infra/aws-connect.yaml`.

**Moving existing connections to the role.** A connect stack trusts exactly one principal, so each
existing `groundcrew-read-only` stack is updated once: CloudFormation, the stack, Update, Use current
template (or upload the new `aws-connect.yaml`), set `GroundcrewPrincipalArn` to the role ARN above,
keep every other parameter, Submit. Then Verify the connection in Groundcrew (Connections, the
account, Verify) so it is checked through the new identity. With the CLI:

```bash
aws cloudformation update-stack --region <region> --stack-name groundcrew-read-only \
  --use-previous-template --capabilities CAPABILITY_NAMED_IAM \
  --parameters ParameterKey=GroundcrewPrincipalArn,ParameterValue=arn:aws:iam::881050425883:role/groundcrew-platform \
    ParameterKey=ExternalId,UsePreviousValue=true ParameterKey=Tier,UsePreviousValue=true \
    ParameterKey=EnableOperate,UsePreviousValue=true ParameterKey=OperateServiceArns,UsePreviousValue=true \
    ParameterKey=OperateSecretArns,UsePreviousValue=true ParameterKey=OperatePassRoleArns,UsePreviousValue=true \
    ParameterKey=OperateFunctionArns,UsePreviousValue=true ParameterKey=OperateInstanceArns,UsePreviousValue=true \
    ParameterKey=RoleName,UsePreviousValue=true ParameterKey=CreateResourceExplorerIndex,UsePreviousValue=true
```

When no stack trusts the user any more, delete its access keys and the `groundcrew-platform` user.

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
`GITHUB_APP_ID` and `GITHUB_APP_PRIVATE_KEY_PATH` (keep the `.pem` in `secrets/` locally, or
`/etc/groundcrew/github-app.pem` on the platform host). Sign-in uses the same app's OAuth client.

**Which installations a workspace may use.** The app is installed on many accounts, so a
workspace sees and links only installations an owner proved access to: Connections, GitHub,
**Find my installations** sends them through GitHub authorization (the app's OAuth client);
GitHub's answer to `GET /user/installations` with that one-time token lists exactly the
installations they can reach, and the token is dropped (`app/connectors/github_claims.py`).
Installations already used by the workspace's GitHub connections count too.

**Webhooks** (`POST /github/webhook`) must be signed with `GITHUB_WEBHOOK_SECRET`; an uninstall
stops the connections that used it, suspend and unsuspend stop and restart them, and removed
repositories leave the connections.

### The production GitHub App (create it once, separate from the development app)

GitHub, your organization, Settings, Developer settings, GitHub Apps, New GitHub App:

| Setting | Value |
| --- | --- |
| GitHub App name | `Groundcrew` (it becomes the install URL, `github.com/apps/<name>`) |
| Homepage URL | `https://groundkrew.com` |
| Callback URLs | `https://groundkrew.com/api/auth/callback/github` (sign-in), then add `https://groundkrew.com/connections/github/callback` (Find my installations) |
| Expire user authorization tokens | on |
| Request user authorization (OAuth) during installation | **off** (the Setup URL below does it, with Groundcrew's own checked state) |
| Enable Device Flow | off |
| Setup URL | `https://groundkrew.com/connections/github/setup`, **Redirect on update** on |
| Webhook | Active, URL `https://api.groundkrew.com/github/webhook`, a random secret (`openssl rand -hex 32`), SSL verification on |
| Repository permissions | Contents: **Read and write** (reads files; fix PRs create a `groundcrew/fix-*` branch and commit to it). Pull requests: **Read and write** (opens fix PRs, reads their state). Actions: **Read** (workflow runs: deploys and follow-ups). Metadata: Read (required) |
| Account permissions | Email addresses: **Read** (sign-in links accounts by verified email) |
| Subscribe to events | none needed: `installation` and `installation_repositories` are always sent |
| Where can this GitHub App be installed? | **Any account** |

Not granted: Workflows (a fix PR that changes `.github/workflows/` would be refused by GitHub;
grant Workflows: Read and write only if you want those), Administration, Secrets, and every
other permission.

After creating it: generate a private key, and copy the App ID, Client ID, and a new client
secret. On the server:

```bash
gc-secrets set GITHUB_APP_ID
gc-secrets set GITHUB_APP_CLIENT_ID
gc-secrets set GITHUB_APP_CLIENT_SECRET
gc-secrets set GITHUB_WEBHOOK_SECRET          # the webhook secret above
gc-secrets set-file github-app.pem < key.pem  # GITHUB_APP_PRIVATE_KEY_PATH=/etc/groundcrew/github-app.pem
```

The callback URLs are on `groundkrew.com`, not `api.groundkrew.com`: GitHub sign-in is
handled by the web app (Auth.js), whose session cookie lives on that origin, and the
installation callback is a page there. Only the webhook goes to the API.

## Email (sign-in links, invites, notifications)

Groundcrew sends email over SMTP (`SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURITY`, `SMTP_USERNAME`,
`SMTP_PASSWORD`, `EMAIL_FROM`). A hosted instance refuses to start without `SMTP_HOST` while
email sign-in is on.

**Resend (recommended).** Add the domain `groundkrew.com` in Resend, Domains. Resend shows the
exact DNS records to add at your registrar; they are of this shape (copy the values from Resend,
not from here):

| Type | Name | Value |
| --- | --- | --- |
| MX | `send.groundkrew.com` | `feedback-smtp.<region>.amazonses.com`, priority 10 (bounce handling) |
| TXT | `send.groundkrew.com` | `v=spf1 include:amazonses.com ~all` (SPF for the bounce domain) |
| TXT | `resend._domainkey.groundkrew.com` | `p=MIGfMA0...` (DKIM public key from Resend) |
| TXT | `_dmarc.groundkrew.com` | `v=DMARC1; p=none; rua=mailto:dmarc@groundkrew.com` |

Start DMARC at `p=none` and read the reports for two weeks, then move to `p=quarantine`. If
`groundkrew.com` has no other mail sender, its own SPF can be `v=spf1 -all` (no server sends as
the bare domain: Resend's envelope uses `send.groundkrew.com`). Then:

```
SMTP_HOST=smtp.resend.com
SMTP_PORT=465
SMTP_SECURITY=ssl
SMTP_USERNAME=resend
SMTP_PASSWORD=<a Resend API key with sending access only>
EMAIL_FROM=Groundcrew <no-reply@groundkrew.com>
```

**Gmail** works for small volumes (about 500 a day) with an app password
(`smtp.gmail.com`, 587, `starttls`), but mail then comes from your Gmail address: SPF, DKIM, and
DMARC for `groundkrew.com` cannot apply, and recipients see the Gmail address. Prefer Resend for
a public service.

## Accounts and sign-in

On a hosted instance people sign in with **GitHub** or an **emailed sign-in link**; the first
sign-in creates the account and a workspace of its own. Email and password, and Google, exist
for local development and can be turned on with `PASSWORD_AUTH_ENABLED=true` /
`GOOGLE_AUTH_ENABLED=true`. Callback URLs: `<web origin>/api/auth/callback/github` (and
`.../google`).

- A sign-in link works once and expires after `EMAIL_LINK_MINUTES` (15). It opens a page with a
  Sign in button, so mail scanners that open links do not use it up. Asking for a link answers
  the same whether or not the address has an account; a newer link retires the older ones.
- The same verified email is the same user: a GitHub sign-in with a verified address and an
  email link to that address reach one account. An account whose address was never proven is
  taken over by whoever proves it (GitHub's verified email, or the link), and its password is
  removed.
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
  by GitHub login, as an **owner** (members, connections, settings, every approval), an
  **approver** (starts runs, deploys, discovery and incidents; approves deploys and fixes), or a
  **viewer** (reads everything, changes nothing). New members are viewers unless the owner
  picks another role. Emergency actions can be kept for owners (Settings, Approvals and limits).
- Every approval and every change to members, invites, connections and settings is in the
  workspace's audit log (Settings, Activity; `GET /workspaces/{id}/audit`), with who, when, and
  the client address.
- Deleting: an owner deletes a connection (its credential, discovered resources and telemetry
  go), or the whole workspace after typing its name. Anyone deletes their own account
  (Settings); workspaces only they are in go with it, and a shared workspace where they are the
  only owner must get another owner first.

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
| `AGENT_API_URL` | The public API URL Groundcrew agents call (not localhost), shown in their install command |
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

## Limits and abuse protection

Each workspace has quotas (`app/quotas.py`); a reached one answers 429 with
`{"code": "quota_exceeded", "quota": ..., "limit": ..., "resets_at": ...}` and Settings, Usage
shows them. Defaults, all settable:

| Setting | Default | What |
| --- | --- | --- |
| `LLM_WORKSPACE_DAILY_USD_CAP` | `LLM_DAILY_USD_CAP` | model spend per workspace per UTC day; owners can only lower it |
| `LLM_DAILY_USD_CAP`, `LLM_TOTAL_USD_CAP` | 2.00, 25 | the whole platform; when reached, agents stop with a message saying so |
| `QUOTA_TAVILY_PER_WORKSPACE_DAY` | 10 | web research calls |
| `QUOTA_CONCURRENT_RUNS_PER_WORKSPACE` | 2 | runs a person started still going (the watcher's are not counted) |
| `QUOTA_BUILDS_PER_WORKSPACE_DAY`, `QUOTA_MAX_BUILD_SECONDS` | 10, 900 | deploy builds and the longest build attempt |
| `QUOTA_CONNECTIONS_PER_WORKSPACE` | 10 | connections |
| `RATE_LIMIT_USER_PER_MINUTE`, `RATE_LIMIT_STREAM_TOKENS_PER_MINUTE`, `MAX_OPEN_STREAMS_PER_USER` | 300, 30, 10 | per user, in the API |
| `MAX_REQUEST_BYTES` | 1048576 | larger request bodies get 413 (API and web proxy) |
| `SIGNUPS_PER_HOUR`, `SIGNUPS_PER_IP_DAY` | 30, 5 | new accounts |

Operators (on the server, against its database):

```bash
python -m app.admin_cli signups status|pause|resume     # pausing still lets invited people join
python -m app.admin_cli quota show <workspace id>
python -m app.admin_cli quota set <workspace id> builds 25   # one workspace's own limit
python -m app.admin_cli quota clear <workspace id> builds
```

People listed in `PLATFORM_ADMINS` (emails or GitHub logins) also see the sign-up switch in
Settings. nginx limits each client address in front of all this (infra/platform/host).

## Security headers

The web app sends a per-request Content-Security-Policy with a nonce, `frame-ancestors 'none'`,
X-Frame-Options, Referrer-Policy, Permissions-Policy, and HSTS over HTTPS; its session cookie is
`__Secure-`, HttpOnly and SameSite=Lax when `AUTH_URL` is https. The `/api/gc` proxy refuses
state-changing requests from other origins. The API sends nosniff, DENY, no-referrer and a
deny-all CSP on every response, allows CORS only from `WEB_ORIGIN` for reading live streams, and
serves no `/docs` or `/openapi.json` outside dev and test.

## Other settings

- `SECRETS_KEY` (`make keygen`): Fernet key for stored secrets (Vercel tokens, kubeconfigs, Azure client
  secrets). They are decrypted only inside the calls that use them.
- `DISCOVERY_REGIONS` (default `us-east-2`): the regions discovery may call. IAM and STS are global.
- `MAX_RESOURCES_PER_CONNECTION` (default 2000) and `DISCOVERY_TIMEOUT_SECONDS` (default 100).
