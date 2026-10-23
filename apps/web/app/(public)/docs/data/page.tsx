import type { Metadata } from "next";
import Link from "next/link";

import { Callout, DocSection, DocsArticle, Inline, Table } from "@/components/public/docs-ui";

export const metadata: Metadata = {
  title: "Delete data",
  description:
    "Delete a connection, a workspace, or your account in Groundcrew, what goes with each, and how to end access on your side.",
};

const TOC = [
  { id: "overview", title: "Three ways to delete" },
  { id: "connection", title: "Delete a connection" },
  { id: "workspace", title: "Delete a workspace" },
  { id: "account", title: "Delete your account" },
  { id: "your-side", title: "Ending access on your side" },
  { id: "kept", title: "What is kept" },
];

export default function DataPage() {
  return (
    <DocsArticle
      path="/docs/data"
      title="Delete data"
      lead="You can delete a single connection, a whole workspace, or your account at any time. Deletion takes effect at once and cannot be undone."
      toc={TOC}
    >
      <DocSection id="overview" title="Three ways to delete">
        <Table
          head={["Delete", "Who", "Where"]}
          rows={[
            ["A connection", "Owners", "Connections, the remove button on the connection"],
            ["A workspace", "Owners", "Settings, Delete workspace, after typing its name"],
            ["Your account", "You", "Settings, Delete account"],
          ]}
        />
      </DocSection>

      <DocSection id="connection" title="Delete a connection">
        <p>Removing a connection deletes, at once:</p>
        <ul>
          <li>Its stored credential: the encrypted token or kubeconfig, or for AWS the role ARN and ExternalId.</li>
          <li>Every resource it discovered, and the lines to and from them on the map.</li>
          <li>The logs, metrics, and events Groundcrew read for those resources.</li>
        </ul>
        <p>
          Past incidents stay as history, without the deleted resources&apos; details. When the removal is done, the
          dialog says how many resources were removed from the map and what to remove on your side to end access
          completely.
        </p>
      </DocSection>

      <DocSection id="workspace" title="Delete a workspace">
        <p>
          Deleting a workspace removes everything Groundcrew stored for it: members and invites, connections and their
          credentials, the map, runs and their steps, approvals, incidents, telemetry, usage, and the activity log.
          Queued work for its runs is cancelled. Cached cloud sessions for its connections are dropped at once.
        </p>
        <Callout kind="warning">
          <p>
            Apps Groundcrew deployed keep running in your accounts, and pull requests it opened stay open on GitHub.
            Remove them there if you no longer want them. <Link href="/docs/deploys#cost">How to remove deployed apps</Link>.
          </p>
        </Callout>
      </DocSection>

      <DocSection id="account" title="Delete your account">
        <p>Deleting your account removes:</p>
        <ul>
          <li>Your sign-in identities and pending sign-in links.</li>
          <li>Your memberships.</li>
          <li>Every workspace where you are the only member, with everything in it.</li>
        </ul>
        <p>
          Shared workspaces stay for the other members. If you are the only owner of a shared workspace, the dialog says
          so: make someone else an owner there first, or delete that workspace. Your name stays on approvals you made and
          in the activity logs of shared workspaces, as a record of who did what.
        </p>
      </DocSection>

      <DocSection id="your-side" title="Ending access on your side">
        <p>
          Groundcrew can delete only what it stores. The access you granted lives in your own accounts, and removing it
          there ends it for good:
        </p>
        <ul>
          <li>
            <strong>AWS</strong>: delete the <Inline>groundcrew-read-only</Inline> CloudFormation stack. Remove apps
            Groundcrew deployed first, if you used deploys.
          </li>
          <li>
            <strong>GitHub</strong>: uninstall the Groundcrew GitHub App in your account or organization settings. Access
            ends at once, and Groundcrew stops the connections that used it.
          </li>
          <li>
            <strong>Vercel</strong>: delete the token in your Vercel account settings.
          </li>
          <li>
            <strong>Kubernetes</strong>: delete the service account and its RBAC with <Inline>kubectl delete -f</Inline>{" "}
            on the manifests you applied. That also ends every token issued for it.
          </li>
          <li>
            <strong>Slack</strong>: remove the incoming webhook in Slack.
          </li>
        </ul>
        <p>Each connect guide has its own removal steps.</p>
      </DocSection>

      <DocSection id="kept" title="What is kept">
        <ul>
          <li>Server logs, which may include your email address and request addresses, expire after 30 days.</li>
          <li>
            The database has no backups, so a deleted workspace or account cannot be restored, by you or by us.
          </li>
        </ul>
        <p>
          The <Link href="/privacy">Privacy Policy</Link> lists everything Groundcrew stores and who processes it.
        </p>
      </DocSection>
    </DocsArticle>
  );
}
