import type { Metadata } from "next";
import Link from "next/link";

import { Callout, DocSection, DocsArticle, Table } from "@/components/public/docs-ui";

export const metadata: Metadata = {
  title: "Approvals and audit log",
  description:
    "What needs an approval in Groundcrew, how an approval is tied to the exact plan you saw, and what the activity log records.",
};

const TOC = [
  { id: "rule", title: "The rule" },
  { id: "what", title: "What needs an approval" },
  { id: "decide", title: "Approving and rejecting" },
  { id: "bound", title: "Bound to the plan" },
  { id: "inbox", title: "The Approvals inbox" },
  { id: "audit", title: "The activity log" },
  { id: "access", title: "What Groundcrew can access" },
];

export default function ApprovalsPage() {
  return (
    <DocsArticle
      path="/docs/approvals"
      title="Approvals and audit log"
      lead="Groundcrew's agents read freely and change nothing on their own. Every change to your infrastructure or repositories is a plan that a person approves, and every decision is recorded with who made it."
      toc={TOC}
    >
      <DocSection id="rule" title="The rule">
        <p>
          Every tool an agent can use is either a read, a write, or an emergency action. Reads run at once. Writes and
          emergency actions never run directly: they create an approval, and the run waits until a person decides.
        </p>
        <Callout kind="safety">
          <p>
            Models never run their own text as an action. When Medic suggests a fix, Groundcrew builds the concrete
            pull request or action plan itself, and that plan is what you approve.
          </p>
        </Callout>
      </DocSection>

      <DocSection id="what" title="What needs an approval">
        <Table
          head={["Change", "Kind", "Who can approve"]}
          rows={[
            ["A deploy plan", "Write", "Owners and approvers"],
            ["A fix pull request", "Write", "Owners and approvers"],
            ["A rollback after a failed deploy", "Emergency", "Owners, or approvers too if allowed"],
            ["An emergency fix (rollback, restart, secret rollback, reboot)", "Emergency", "Owners, or approvers too if allowed"],
          ]}
        />
        <p>
          Owners decide who approves emergency actions in <strong>Settings</strong>,{" "}
          <strong>Approvals and limits</strong>. Viewers never approve.{" "}
          <Link href="/docs/roles#matrix">All permissions by role</Link>.
        </p>
      </DocSection>

      <DocSection id="decide" title="Approving and rejecting">
        <p>
          The approval card shows what will happen: for a deploy, every resource and the exact image; for a pull request,
          the files and diff; for an emergency action, before and after lines. Choose the approve button on the card
          (for example <strong>Approve and deploy</strong> or <strong>Approve and run</strong>) or{" "}
          <strong>Reject</strong>.
        </p>
        <ul>
          <li>Approving runs that plan once. The run picks up where it waited.</li>
          <li>Rejecting ends that step without any change. For an incident, Medic records the rejection.</li>
          <li>
            Stopping a run rejects all of its pending approvals.
          </li>
        </ul>
        <p>
          Approving is always done signed in, in Groundcrew. Notifications never carry approve links; see{" "}
          <Link href="/docs/notifications#approve">why</Link>.
        </p>
      </DocSection>

      <DocSection id="bound" title="Bound to the plan">
        <p>An approval is not a general permission. It is tied to one plan:</p>
        <ul>
          <li>
            <strong>The exact plan.</strong> The approval records a fingerprint of the plan: the action, the connection,
            the target, and the before and after state. A different plan does not match it.
          </li>
          <li>
            <strong>Used once.</strong> After approving, Groundcrew issues a signed, single-use token for that plan and
            that run. The first write spends it, so the change happens exactly once, however many workers pick it up.
          </li>
          <li>
            <strong>Checked again before writing.</strong> An emergency action re-reads the current state. If it no
            longer matches the plan, for example because someone deployed in the meantime, nothing is written and the
            fix is marked failed with the reason.
          </li>
        </ul>
      </DocSection>

      <DocSection id="inbox" title="The Approvals inbox">
        <p>
          <strong>Approvals</strong> in the sidebar lists everything waiting for a decision across the workspace, with a
          count badge, and the history of decided approvals: what it was, who decided, and when. The Overview page
          shows the pending count too.
        </p>
      </DocSection>

      <DocSection id="audit" title="The activity log">
        <p>
          <strong>Settings</strong>, <strong>Activity</strong> is the workspace&apos;s audit log. Every member can read
          it. Each entry has who did it, when, and the client address. It records:
        </p>
        <ul>
          <li>Every approval and rejection, with the tool, its kind, and the run.</li>
          <li>Members added, removed, or given another role; invites created, changed, or cancelled; members leaving.</li>
          <li>Connections added, changed (regions, deploys, emergency fixes, deploy settings), or removed.</li>
          <li>Workspace name and settings changes, including notifications and limits.</li>
          <li>GitHub installations an owner proved access to.</li>
        </ul>
        <p>
          Entries cannot be edited or deleted by members. When a person deletes their account, their name stays on the
          entries they made. The log goes only when the whole workspace is deleted.
        </p>
      </DocSection>

      <DocSection id="access" title="What Groundcrew can access">
        <p>
          <strong>Settings</strong>, <strong>What Groundcrew can access</strong> lists each connection with what it
          reads and what it may change after an approval, so you can review the whole workspace&apos;s access in one
          place. The full permission model is on the <Link href="/security">Security</Link> page.
        </p>
      </DocSection>
    </DocsArticle>
  );
}
