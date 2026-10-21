import type { Metadata } from "next";
import Link from "next/link";

import { CONTACT } from "@/components/public/contact";
import { Article, Inline, Section, SubHeading } from "@/components/public/prose";

export const metadata: Metadata = {
  title: "Security",
  description:
    "Read-only by default, scoped write tiers you opt into, approvals bound to the exact plan, roles, an audit log, and isolated builds.",
};

export default function SecurityPage() {
  return (
    <Article
      title="Security"
      lead="Groundcrew reads freely and writes only through actions a person approves. Access is scoped on your side, so the limits hold even if Groundcrew itself makes a mistake."
    >
      <Section id="permissions" title="The permission model">
        <SubHeading>Read-only by default</SubHeading>
        <p>
          Connecting AWS creates one role, <Inline>groundcrew-read-only</Inline>, from a CloudFormation template you can
          read before you run it. Only Groundcrew&apos;s platform identity can assume it, only with your workspace&apos;s
          ExternalId, for one hour at a time. It reads configuration, metadata, logs, and metrics. An explicit deny blocks
          secret values, parameter values, KMS decryption, and S3 object contents, and an explicit deny wins over any
          permission added later. Groundcrew also refuses any AWS call outside a read-only allowlist before it is sent.
        </p>
        <p>
          On Kubernetes, the read-only ClusterRole has get, list, and watch on workloads, services, ingresses, pods, and
          events, plus pod logs. It has no access to Secrets or ConfigMaps, no exec, and no write verb. Vercel and GitHub
          reads go through allowlists of read-only endpoints.
        </p>
        <SubHeading>Write tiers you opt into</SubHeading>
        <ul>
          <li>
            <strong>Deploy</strong> (<Inline>Tier=deploy</Inline>): a role that may create and change only resources named{" "}
            <Inline>groundcrew-*</Inline> and tagged <Inline>project=groundcrew</Inline>, in a dedicated{" "}
            <Inline>groundcrew-deploys</Inline> ECS cluster. It can write deploy secrets under{" "}
            <Inline>groundcrew/*</Inline> but never read them.
          </li>
          <li>
            <strong>Operate</strong> (<Inline>EnableOperate=true</Inline>): a role for emergency fixes (roll an ECS service
            back to an earlier revision, restart it, move a secret back to its previous version, roll back a Lambda alias,
            reboot an impaired EC2 instance), limited to the exact resources you list. It cannot pass Groundcrew&apos;s own
            roles and cannot read secret values.
          </li>
          <li>
            On Kubernetes, deploys go only into <Inline>gc-*</Inline> namespaces, enforced by an admission policy, and
            emergency fixes are a separate, optional role that can only patch Deployments and StatefulSets.
          </li>
        </ul>
      </Section>

      <Section id="approvals" title="Approvals">
        <ul>
          <li>Every write (a deploy plan, a fix pull request) and every emergency action needs a person&apos;s approval.</li>
          <li>
            The approval is bound to a hash of the exact plan: the resources, the current state, and the desired state. If
            the plan changes, the approval no longer applies.
          </li>
          <li>
            An approved action can run once. Before it writes, Groundcrew checks the state again and stops if it changed
            since the plan was approved.
          </li>
          <li>Rejecting is always possible, and stopping a run rejects everything it was waiting for.</li>
          <li>Code fixes are pull requests on a new branch. Groundcrew never merges and never pushes to your default branch.</li>
        </ul>
      </Section>

      <Section id="roles" title="Roles and the audit log">
        <ul>
          <li>
            <strong>Owners</strong> manage members, connections, and settings, and approve everything.
          </li>
          <li>
            <strong>Approvers</strong> start deploys and runs and approve deploys and fixes. A workspace can keep emergency
            actions for owners only.
          </li>
          <li>
            <strong>Viewers</strong> see everything in the workspace and change nothing.
          </li>
        </ul>
        <p>
          Each workspace has an audit log of who approved or rejected what and when, and of every change to members,
          invitations, connections, and settings. Every member can read it in Settings. People outside a workspace cannot
          see that it exists: its pages and API routes answer as if it did not.
        </p>
      </Section>

      <Section id="builds" title="Isolated builds">
        <p>
          Building your code means running it. Builds and smoke tests run in a separate container daemon that runs without
          root under its own system user, with containers inside a gVisor sandbox. A build has its own network with no route
          to Groundcrew&apos;s database, API, or the cloud metadata service, and limits on CPU, memory, disk, processes, and
          time. Nothing from the host is mounted into it. Registry credentials are passed for a single push and not kept,
          and everything a build created is removed when it ends. An image leaves the sandbox only after you approve the
          deploy plan.
        </p>
      </Section>

      <Section id="data" title="Credentials and data">
        <ul>
          <li>No long-lived cloud keys: AWS access is short-lived role sessions, kept in memory.</li>
          <li>Tokens and kubeconfigs you paste are encrypted at rest and never shown again.</li>
          <li>Tokens and keys are redacted from logs, error messages, and text sent to models.</li>
          <li>All traffic to Groundcrew uses TLS 1.2 or later, with HSTS.</li>
          <li>Sign-in is GitHub or a single-use email link; sessions use secure, HTTP-only cookies.</li>
        </ul>
        <p>
          The full list of what is read and stored is in the <Link href="/privacy">Privacy Policy</Link>.
        </p>
      </Section>

      <Section id="revoke" title="Removing access">
        <p>You can cut Groundcrew off at any time from your side:</p>
        <ul>
          <li>AWS: delete the <Inline>groundcrew-read-only</Inline> CloudFormation stack. Every role it created goes with it.</li>
          <li>GitHub: uninstall the Groundcrew GitHub App, or remove repositories from it.</li>
          <li>
            Kubernetes: <Inline>kubectl delete -f k8s-readonly-rbac.yaml</Inline> (and the operate file if you applied it),
            or delete the service account.
          </li>
          <li>Vercel: delete the token in your Vercel account settings.</li>
        </ul>
        <p>
          Then delete the connection in Groundcrew to remove what it stored. Step-by-step guides are in the{" "}
          <Link href="/docs">docs</Link>.
        </p>
      </Section>

      <Section id="report" title="Reporting a vulnerability">
        <p>
          Write to <a href={`mailto:${CONTACT.security}`}>{CONTACT.security}</a> with what you found and how to reproduce
          it. Please give us time to fix it before telling others, and do not access other people&apos;s data or disrupt
          the service while testing. We will reply as soon as we can and tell you when it is fixed.
        </p>
      </Section>
    </Article>
  );
}
