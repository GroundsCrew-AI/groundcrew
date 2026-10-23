import type { Metadata } from "next";
import Link from "next/link";

import { Callout, DocSection, DocsArticle, Inline, Step, Steps } from "@/components/public/docs-ui";

export const metadata: Metadata = {
  title: "Quick start",
  description: "Sign in, connect your first account, run discovery, and start your first deploy or incident in Groundcrew.",
};

const TOC = [
  { id: "sign-in", title: "1. Sign in" },
  { id: "connect", title: "2. Connect something" },
  { id: "discover", title: "3. Run discovery" },
  { id: "deploy", title: "4. Deploy an app" },
  { id: "incidents", title: "5. Let Medic watch" },
  { id: "team", title: "6. Bring your team" },
  { id: "next", title: "Where to go next" },
];

export default function GettingStartedPage() {
  return (
    <DocsArticle
      path="/docs/getting-started"
      title="Quick start"
      lead="From signing in to your first map, deploy, and incident. Most people finish the first three steps in about ten minutes."
      toc={TOC}
    >
      <DocSection id="sign-in" title="1. Sign in">
        <p>
          Open <Link href="/signin">Sign in</Link> and choose <strong>Continue with GitHub</strong>, or enter your email
          address to get a sign-in link. A sign-in link works once and expires after 15 minutes. It opens a page with a
          Sign in button, so a mail scanner that opens the link does not use it up.
        </p>
        <p>
          The first sign-in creates your account and a workspace of its own, and you are its owner. A workspace holds
          everything else: connections, the map, deploys, incidents, approvals, and members. You can create more
          workspaces later and switch between them.
        </p>
        <p>
          A GitHub sign-in and an email link to the same verified address reach the same account, so you can use either
          one later.
        </p>
      </DocSection>

      <DocSection id="connect" title="2. Connect something">
        <p>
          Open <strong>Connections</strong>. Each card says what Groundcrew will be able to see and do before you add
          anything. Only workspace owners add, change, or remove connections.
        </p>
        <Steps>
          <Step title="AWS">
            <p>
              Create one read-only IAM role from Groundcrew&apos;s CloudFormation template. Only Groundcrew can assume
              it, and only with your workspace&apos;s ExternalId. No AWS keys are stored.{" "}
              <Link href="/docs/connect/aws">Connect AWS</Link>.
            </p>
          </Step>
          <Step title="GitHub">
            <p>
              Install the Groundcrew GitHub App on the repositories you choose. Groundcrew reads them to see how your
              apps are built and deployed, builds them for deploys, and opens fix pull requests.{" "}
              <Link href="/docs/connect/github">Connect GitHub</Link>.
            </p>
          </Step>
          <Step title="Vercel or Kubernetes">
            <p>
              Paste a Vercel team token, or a kubeconfig for a read-only service account.{" "}
              <Link href="/docs/connect/vercel">Connect Vercel</Link>,{" "}
              <Link href="/docs/connect/kubernetes">Connect Kubernetes</Link>.
            </p>
          </Step>
        </Steps>
        <Callout kind="safety">
          <p>
            Every connection starts read-only. Deploys and emergency fixes are separate switches that you turn on per
            connection, and even then each change waits for an approval.
          </p>
        </Callout>
      </DocSection>

      <DocSection id="discover" title="3. Run discovery">
        <p>
          Choose <strong>Run discovery</strong> at the top of the Connections page. Groundcrew reads every connection in
          parallel and streams what it finds. When it is done, open <strong>Map</strong>: your resources grouped by
          provider, with lines for how they connect, such as a service that reads a database, uses a secret, or is
          deployed by a workflow in a linked repository.
        </p>
        <p>
          Scout, the agent that reads your system, writes a short summary of what it found at the top of the map. Use{" "}
          <strong>Refresh</strong> on a single connection when only that one changed.{" "}
          <Link href="/docs/discovery">How discovery works</Link>.
        </p>
      </DocSection>

      <DocSection id="deploy" title="4. Deploy an app">
        <p>
          With GitHub connected and a deploy target (a Kubernetes cluster, or an AWS account with deploys turned on),
          open <strong>Deploy</strong>. Pick a repository and a branch, tag, or commit, pick the target, and add env vars
          if the app needs them.
        </p>
        <p>
          Groundcrew reads the repository, writes or uses its Dockerfile, builds and smoke-tests the image in an
          isolated sandbox, and shows you the plan: every resource it will create, with secret values masked. Nothing
          leaves the sandbox until you choose <strong>Approve and deploy</strong>. Then it deploys, checks the app
          answers, and shows the live URL. <Link href="/docs/deploys">Deploys in detail</Link>.
        </p>
      </DocSection>

      <DocSection id="incidents" title="5. Let Medic watch">
        <p>
          The incident watcher checks linked services, databases, functions, and workloads every minute. When a signal
          goes bad, such as a failing health check, tasks stopping, or an error spike in the logs, it opens an incident
          and Medic starts investigating. You can also open one yourself with <strong>Report incident</strong> on the
          Incidents page.
        </p>
        <p>
          Medic gathers logs, metrics, and recent changes, finds the root cause with each claim linked to its evidence,
          and proposes fixes: a pull request, and when production is down, an emergency action such as a rollback.
          Each one waits for your approval. <Link href="/docs/incidents">Incidents and fixes</Link>.
        </p>
      </DocSection>

      <DocSection id="team" title="6. Bring your team">
        <p>
          In <strong>Settings</strong>, <strong>Members</strong>, add people by email address or GitHub login, as an
          owner, an approver, or a viewer. New members are viewers unless you pick another role.{" "}
          <Link href="/docs/roles">Members and roles</Link>.
        </p>
        <p>
          In <strong>Settings</strong>, <strong>Notifications</strong>, add a Slack channel or choose who gets email when
          an incident opens, waits for an approval, resolves, or fails.{" "}
          <Link href="/docs/notifications">Notifications</Link>.
        </p>
      </DocSection>

      <DocSection id="next" title="Where to go next">
        <ul>
          <li>
            Read the <Link href="/security">security model</Link>: what each role and connection can do, and how to
            remove access.
          </li>
          <li>
            See the <Link href="/docs/limits">limits</Link> each workspace has, and set a lower daily model spend cap if
            you want one.
          </li>
          <li>
            Running Groundcrew on your own server instead? The repository&apos;s <Inline>docs/self-hosting.md</Inline>{" "}
            covers the settings an operator needs.
          </li>
        </ul>
      </DocSection>
    </DocsArticle>
  );
}
