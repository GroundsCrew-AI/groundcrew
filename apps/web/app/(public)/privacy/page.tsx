import type { Metadata } from "next";
import Link from "next/link";

import { CONTACT, POLICIES_UPDATED } from "@/components/public/contact";
import { Article, Inline, Section, SubHeading } from "@/components/public/prose";

export const metadata: Metadata = {
  title: "Privacy Policy",
  description: "What Groundcrew reads, what it stores, and how to delete it. Secret values are never read.",
};

export default function PrivacyPage() {
  return (
    <Article
      title="Privacy Policy"
      updated={POLICIES_UPDATED}
      lead="Groundcrew reads what it needs to map, deploy, and repair your systems, and as little else as it can. It never reads secret values. This page lists what it reads, what it stores, who else sees it, and how to delete it."
    >
      <Section id="account" title="Your account">
        <ul>
          <li>
            From GitHub sign-in: your GitHub user id, login, name, avatar URL, and email address with whether GitHub
            verified it.
          </li>
          <li>From email sign-in: your email address and when you confirmed it. Sign-in links are single use; only a hash of each link is stored.</li>
          <li>Your workspaces, your role in each, and invitations you sent or received.</li>
          <li>
            An audit log of actions in each workspace: who approved or rejected what, member and connection changes, and the
            network address the request came from.
          </li>
        </ul>
      </Section>

      <Section id="connections" title="What Groundcrew reads from your connections">
        <SubHeading>AWS</SubHeading>
        <p>
          Through the read-only role you create from our CloudFormation template: resource inventory and configuration
          (for example ECS services, task definitions, load balancers, databases, Lambda functions), tags, CloudWatch logs
          and metrics during an incident, and secret metadata such as names and version dates. The role has an explicit
          deny on <Inline>secretsmanager:GetSecretValue</Inline>, <Inline>ssm:GetParameter*</Inline>,{" "}
          <Inline>kms:Decrypt</Inline>, <Inline>s3:GetObject</Inline>, and <Inline>s3:GetObjectVersion</Inline>, so secret
          values, decrypted data, and the contents of your S3 objects cannot be read. Task definitions you already have may
          contain plain environment values, which AWS returns with their configuration.
        </p>
        <SubHeading>Kubernetes</SubHeading>
        <p>
          Through the service account you create: namespaces, workloads, services, ingresses, pods, events, and pod logs.
          Secrets are never read. If you turn on the optional ConfigMap or Secret change detection, Groundcrew asks only for
          their metadata (name and change times), never their contents.
        </p>
        <SubHeading>GitHub</SubHeading>
        <p>
          Through the GitHub App, only on the repositories you choose: repository files (to detect how an app is built, to
          build it, and to write fix pull requests), workflow runs, commits, and pull requests. Clones are temporary and
          deleted after use.
        </p>
        <SubHeading>Vercel</SubHeading>
        <p>
          With the token you paste: projects, linked repositories, domains, recent production deployments, and the names
          and types of environment variables. The only values kept are plain variables whose names end in{" "}
          <Inline>_URL</Inline> or <Inline>_HOST</Inline>; every other value is dropped before it is stored or logged.
        </p>
      </Section>

      <Section id="stored" title="What Groundcrew stores">
        <ul>
          <li>The resource graph built from what it read: names, types, configuration, relationships, and tags.</li>
          <li>Runs, their steps, approvals, incidents with the evidence lines they cite, and pull requests opened.</li>
          <li>
            No AWS access keys. Groundcrew assumes your role with a workspace-specific ExternalId and keeps the resulting
            one-hour session in memory only.
          </li>
          <li>
            Credentials you paste (a Vercel token, a kubeconfig, a container registry token, a Slack webhook) are encrypted
            before they are stored and are never shown again.
          </li>
          <li>
            Environment values you give a deploy are encrypted while the deploy job runs and deleted when it ends. Secret
            values for an AWS deploy go into Secrets Manager in your account; Groundcrew writes them and never reads them
            back.
          </li>
          <li>Server logs, which may include your email address, workspace ids, and request addresses, kept for 30 days.</li>
        </ul>
        <p>
          The database has no backups. If the server is lost, the data in it is lost too. Your own systems are not
          affected.
        </p>
      </Section>

      <Section id="processors" title="Who else processes data">
        <ul>
          <li>
            <strong>Nebius Token Factory</strong> runs the language models (NVIDIA Nemotron). Prompts contain the
            information an agent needs for its step, such as resource configuration, repository files, and log lines.
            Tokens and keys found in that text are redacted before it is sent.
          </li>
          <li>
            <strong>Tavily</strong> receives web search queries built from error messages during an incident. Queries hold
            no secrets and no identifiers.
          </li>
          <li>
            <strong>Our email provider</strong> sends sign-in links, invitations, and the notifications you turn on.
          </li>
          <li>
            <strong>Slack</strong> receives the incident notifications you set up, at the webhook you provide.
          </li>
          <li>
            <strong>Amazon Web Services</strong> hosts the Groundcrew server in the United States (us-east-2).
          </li>
        </ul>
        <p>We do not sell your data and do not use it to train models.</p>
      </Section>

      <Section id="cookies" title="Cookies">
        <p>
          Groundcrew sets only the cookies sign-in needs: an encrypted session cookie and short-lived cookies used while
          you sign in. There are no tracking or analytics cookies and no third-party scripts.
        </p>
      </Section>

      <Section id="delete" title="Deleting your data">
        <ul>
          <li>
            <strong>A connection:</strong> owners delete it on the Connections page. Its stored credential and every
            resource and relationship it discovered are deleted. Remove access on your side too (delete the CloudFormation
            stack, uninstall the GitHub App, or delete the Kubernetes service account).
          </li>
          <li>
            <strong>A workspace:</strong> owners delete it in Settings, with its connections, runs, incidents, approvals,
            and audit log.
          </li>
          <li>
            <strong>Your account:</strong> delete it in Settings. Workspaces you own alone are deleted with it; in shared
            workspaces your name stays on past approvals in the audit log.
          </li>
        </ul>
        <p>
          Server logs expire after 30 days. To ask what we hold about you, or for any privacy question, write to{" "}
          <a href={`mailto:${CONTACT.privacy}`}>{CONTACT.privacy}</a>.
        </p>
      </Section>

      <Section id="security" title="Security">
        <p>
          How access is limited, approved, and audited is described on the <Link href="/security">Security</Link> page.
        </p>
      </Section>

      <Section id="changes" title="Changes">
        <p>We will update the date above when this policy changes and tell you in the app when a change matters.</p>
      </Section>
    </Article>
  );
}
