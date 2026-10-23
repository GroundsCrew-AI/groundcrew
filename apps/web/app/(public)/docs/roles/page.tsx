import { Check, Minus } from "lucide-react";
import type { Metadata } from "next";
import Link from "next/link";

import { Callout, DocSection, DocsArticle, Table } from "@/components/public/docs-ui";

export const metadata: Metadata = {
  title: "Members and roles",
  description: "Workspaces, the owner, approver, and viewer roles, inviting people, and changing or removing members.",
};

const TOC = [
  { id: "workspaces", title: "Workspaces" },
  { id: "roles", title: "The three roles" },
  { id: "matrix", title: "Who can do what" },
  { id: "add", title: "Adding people" },
  { id: "change", title: "Changing roles and removing people" },
  { id: "leave", title: "Leaving a workspace" },
  { id: "accounts", title: "Your account and sign-in" },
];

function Yes() {
  return (
    <>
      <Check aria-hidden="true" className="size-4 text-[var(--status-ok)]" />
      <span className="sr-only">Yes</span>
    </>
  );
}

function No() {
  return (
    <>
      <Minus aria-hidden="true" className="size-4 text-subtle" />
      <span className="sr-only">No</span>
    </>
  );
}

const MATRIX: [string, boolean, boolean, boolean][] = [
  ["See the map, runs, deploys, incidents, approvals, and activity", true, true, true],
  ["Start discovery or refresh a connection", true, true, false],
  ["Start a deploy, report an incident, stop a run", true, true, false],
  ["Approve or reject deploy plans and fix pull requests", true, true, false],
  ["Approve emergency actions (rollbacks, restarts)", true, false, false],
  ["Add, change, or remove connections", true, false, false],
  ["Change workspace name, settings, limits, notifications", true, false, false],
  ["Add members, change roles, remove people", true, false, false],
  ["Delete the workspace", true, false, false],
];

export default function RolesPage() {
  return (
    <DocsArticle
      path="/docs/roles"
      title="Members and roles"
      lead="A workspace is where a team works together: its connections, map, deploys, incidents, and approvals. Each member has one role in it, which decides what they can start, approve, and change."
      toc={TOC}
    >
      <DocSection id="workspaces" title="Workspaces">
        <p>
          Your first sign-in creates a workspace with you as its owner. You can create more, for example one per client
          or per environment, and be a member of workspaces other people own. Nothing is shared between workspaces:
          each one has its own connections, map, history, limits, and audit log.
        </p>
        <p>
          A person who is not a member of a workspace cannot see that it exists. Groundcrew answers as if it were not
          there.
        </p>
      </DocSection>

      <DocSection id="roles" title="The three roles">
        <ul>
          <li>
            <strong>Owner</strong>: manages members, connections, and settings, and approves everything. A workspace
            always has at least one owner.
          </li>
          <li>
            <strong>Approver</strong>: starts deploys, discovery, and investigations, and approves deploy plans and
            fixes. Emergency actions too, if an owner allows it.
          </li>
          <li>
            <strong>Viewer</strong>: sees everything in the workspace and changes nothing.
          </li>
        </ul>
        <p>New members are viewers unless the owner who adds them picks another role.</p>
      </DocSection>

      <DocSection id="matrix" title="Who can do what">
        <Table
          head={["Action", "Owner", "Approver", "Viewer"]}
          rows={MATRIX.map(([what, owner, approver, viewer]) => [
            what,
            owner ? <Yes key="o" /> : <No key="o" />,
            approver ? <Yes key="a" /> : <No key="a" />,
            viewer ? <Yes key="v" /> : <No key="v" />,
          ])}
        />
        <p>
          Emergency actions are owners only by default. In <strong>Settings</strong>,{" "}
          <strong>Approvals and limits</strong>, an owner can choose <strong>Owners and approvers</strong> instead.
          Rejecting is always open to owners and approvers. Viewers never approve or reject.
        </p>
        <Callout kind="safety">
          <p>
            Roles are checked by the server on every request, not only hidden in the interface. A viewer who calls the
            API directly gets the same refusal.
          </p>
        </Callout>
      </DocSection>

      <DocSection id="add" title="Adding people">
        <p>
          In <strong>Settings</strong>, <strong>Members</strong>, an owner adds someone by email address or by GitHub
          login, and picks a role:
        </p>
        <ul>
          <li>
            Someone who already has a Groundcrew account with that confirmed address, or that GitHub account, joins at
            once.
          </li>
          <li>
            Anyone else gets an invite that applies when they sign up or sign in. An invite by email address is also
            sent by email. They join once their address is confirmed, through an email sign-in link or a GitHub sign-in
            with that verified address.
          </li>
        </ul>
        <p>
          Pending invites are listed under Members, where an owner can change their role or cancel them. Every addition,
          invite, and cancellation is in the <Link href="/docs/approvals#audit">activity log</Link>.
        </p>
      </DocSection>

      <DocSection id="change" title="Changing roles and removing people">
        <p>
          Owners change a member&apos;s role or remove them under Members. A change takes effect on that person&apos;s
          next request. The last owner cannot be demoted or removed: make someone else an owner first.
        </p>
        <p>
          Removing someone does not undo what they did. Their name stays on approvals they made and in the activity log,
          as a record of who did it.
        </p>
      </DocSection>

      <DocSection id="leave" title="Leaving a workspace">
        <p>
          Any member can leave a workspace with <strong>Leave workspace</strong> at the bottom of Settings. Your other
          workspaces are not affected. If you are the only owner of a shared workspace, make someone else an owner
          before you leave.
        </p>
      </DocSection>

      <DocSection id="accounts" title="Your account and sign-in">
        <p>
          On groundkrew.com you sign in with GitHub or with an emailed sign-in link. The same verified email address is
          the same account, whichever way you sign in. To delete your account and the workspaces only you are in, see{" "}
          <Link href="/docs/data">Delete data</Link>.
        </p>
      </DocSection>
    </DocsArticle>
  );
}
