import type { Metadata } from "next";
import Link from "next/link";

import { Callout, DocSection, DocsArticle, Inline, Step, Steps, SubHeading, Table } from "@/components/public/docs-ui";

export const metadata: Metadata = {
  title: "Incidents and fixes",
  description:
    "How the incident watcher opens incidents, how Medic finds the root cause with evidence, and how fixes are approved, applied, and verified.",
};

const TOC = [
  { id: "open", title: "How incidents open" },
  { id: "rules", title: "What the watcher checks" },
  { id: "investigation", title: "How Medic investigates" },
  { id: "root-cause", title: "Reading the root cause" },
  { id: "fixes", title: "Fixes" },
  { id: "emergency", title: "Emergency actions" },
  { id: "verify", title: "Verify and resolve" },
  { id: "followups", title: "Follow-ups" },
  { id: "statuses", title: "Incident statuses" },
];

export default function IncidentsPage() {
  return (
    <DocsArticle
      path="/docs/incidents"
      title="Incidents and fixes"
      lead="When something breaks, Medic gathers evidence across your connections, finds the root cause, and proposes fixes. You approve each fix. Medic applies it and only calls the incident resolved after the signals that showed the problem are healthy again."
      toc={TOC}
    >
      <DocSection id="open" title="How incidents open">
        <p>There are two ways an incident starts:</p>
        <ul>
          <li>
            <strong>The incident watcher</strong> checks linked resources every minute with read-only calls. When a rule
            fires, it opens an incident and Medic starts at once. An owner can turn the watcher off for the workspace in{" "}
            <strong>Settings</strong>, <strong>Approvals and limits</strong>.
          </li>
          <li>
            <strong>Report incident</strong> on the Incidents page. Pick the resources that look broken, say what you see
            if you like, and choose a severity or let Medic decide. Medic looks back 60 minutes from the report by
            default.
          </li>
        </ul>
        <p>
          There is one open incident per group of related resources. A new signal or report that touches an open
          incident joins it instead of opening another, and an incident on a related resource can take over an older one
          (the older one shows as merged, with a link).
        </p>
      </DocSection>

      <DocSection id="rules" title="What the watcher checks">
        <Table
          head={["Signal", "Fires when"]}
          rows={[
            ["Health check", "ECS tasks stop for failing health checks, or running Kubernetes pods fail their readiness probe"],
            ["ECS deployment failed", "A service's newest deployment is marked failed"],
            ["ECS tasks stopped", "Tasks stop with a non-zero exit code, out-of-memory included"],
            ["Log error spike", "Error lines in the last 5 minutes at 3 times the rate of the 25 minutes before, at least 5"],
            ["Database CPU or latency", "Slow queries, or RDS CPU and read latency both well above their baseline"],
            ["Load balancer", "A jump in 5xx responses, unhealthy targets for 2 minutes, or response times 3 times their baseline"],
            ["Lambda", "A rise in errors or throttles compared with the minutes before"],
            ["EC2 status check", "An instance's system or instance status check is impaired"],
            ["Kubernetes rollout", "A Deployment passes its progress deadline, or a StatefulSet's rollout is stuck"],
            ["Kubernetes pods", "Pods crash-loop, cannot pull their image, or exit with an error, out-of-memory included"],
          ]}
        />
        <p>
          The watcher reads only what discovery found, through the same read-only access. A check that fails is skipped
          and logged; it never opens an incident by itself.
        </p>
      </DocSection>

      <DocSection id="investigation" title="How Medic investigates">
        <p>An investigation is a run you can follow live on the incident page. Medic works in this order:</p>
        <Steps>
          <Step title="Gather">
            <p>
              Logs, metrics, and state for the affected resources and their neighbours on the map: the database a
              service reads, the secret it uses, the log group it writes to, recent commits and workflow runs, and
              recent deploys. Each line is redacted and gets a stable evidence id.
            </p>
          </Step>
          <Step title="Remember">
            <p>
              Looks for similar past incidents in this workspace. A close match&apos;s diagnosis or fix can be reused,
              and the incident says so.
            </p>
          </Step>
          <Step title="Research">
            <p>Searches the web for the most distinctive error, keeping at most 3 results, which are listed on the incident.</p>
          </Step>
          <Step title="Diagnose">
            <p>
              Medic, on NVIDIA Nemotron Ultra, writes the root cause from the evidence. Every claim must cite evidence
              ids. Claims that cite nothing real are dropped, and the incident lists what was dropped.
            </p>
          </Step>
          <Step title="Propose">
            <p>One or more fixes, in order, each with its own approval. Then apply, verify, and remember.</p>
          </Step>
        </Steps>
      </DocSection>

      <DocSection id="root-cause" title="Reading the root cause">
        <p>The incident page shows:</p>
        <ul>
          <li>A one-line summary and the root cause, with a confidence of high, medium, or low.</li>
          <li>Each claim with links to the exact evidence lines it rests on.</li>
          <li>
            The triggering change when Medic found one: a commit, a deploy, a secret that changed, or a config change.
          </li>
          <li>A timeline with the signals and the changes around them.</li>
        </ul>
        <Callout>
          <p>
            Medic never reads secret values or your data. For a changed secret it sees that a new version became current
            and when, not what is in it.
          </p>
        </Callout>
      </DocSection>

      <DocSection id="fixes" title="Fixes">
        <p>Medic prefers a lasting fix in your repository, and adds a quick fix only when production is hurting now.</p>
        <SubHeading>Pull requests</SubHeading>
        <p>
          A fix pull request changes code or infrastructure code in your repository. The approval card shows the files
          and the diff. <strong>Approve and open PR</strong> opens it on a new <Inline>groundcrew/fix-*</Inline> branch.
          Groundcrew never pushes to your default branch and never merges. Your normal review, checks, and pipeline do
          the rest.
        </p>
        <SubHeading>Emergency actions</SubHeading>
        <p>
          When production is down, or degraded with a high-confidence cause and a known way back, Medic also proposes an
          emergency action and puts it first. The card shows the plan as before and after lines.{" "}
          <strong>Approve and run</strong> runs it once.
        </p>
        <p>
          When one fix depends on another, the card says <em>Runs after</em> the first one. A failed emergency fix can be
          planned again with a new approval.
        </p>
      </DocSection>

      <DocSection id="emergency" title="Emergency actions">
        <p>
          These need emergency fixes turned on for the connection, and by default only owners approve them (an owner can
          let approvers approve them too).
        </p>
        <Table
          head={["Action", "What it does"]}
          rows={[
            ["Roll back an ECS service", "Points the service at the previous task definition revision"],
            ["Restart an ECS service", "Starts new tasks on the same revision"],
            ["Roll back a secret", "Makes the previous Secrets Manager version current again, without reading either value"],
            ["Roll back a Lambda alias", "Points the alias at the previous published version"],
            ["Reboot an EC2 instance", "Only when it is running with an impaired status check"],
            ["Undo a Kubernetes rollout", "Puts a Deployment or StatefulSet back on its previous revision"],
            ["Restart a Kubernetes rollout", "Restarts the pods of a Deployment or StatefulSet"],
          ]}
        />
        <p>
          Each action reads the current state into a plan first. When it runs, it checks the state again: if it changed
          since you approved, nothing is written and the fix shows as failed with the reason. A changed Kubernetes
          ConfigMap or Secret gets a note instead of an action, because Groundcrew cannot restore what it never reads.
        </p>
      </DocSection>

      <DocSection id="verify" title="Verify and resolve">
        <p>
          After a fix is applied, Medic re-checks the signals that opened the incident: the rollout, the health check,
          the error rate, and metrics such as database CPU and latency. When they are healthy the incident resolves with
          the time to detect and to recover. If they stay bad until the check times out, the incident fails and says
          what is still failing.
        </p>
        <p>
          When Medic finds no fix, it waits 90 seconds for related signals before giving up, so a second symptom can join
          the incident and lead to a better diagnosis. You can stop an investigation at any time with{" "}
          <strong>Stop</strong>: its pending approvals are rejected and the incident closes as failed.
        </p>
      </DocSection>

      <DocSection id="followups" title="Follow-ups">
        <p>
          A rollback can bring production back while the cause is still in your repository. Groundcrew tracks that so
          the next deploy does not bring the failure back:
        </p>
        <ul>
          <li>
            A fix pull request that is still open when the incident resolves becomes a follow-up. Groundcrew watches it
            until it is merged and a successful deploy workflow on the base branch contains the merge.
          </li>
          <li>
            If the incident was resolved by a rollback with no fix pull request, and Medic knows the commit that caused
            it, the incident shows &quot;Running on a rollback&quot; until a deploy no longer contains that change. A
            person can dismiss it.
          </li>
        </ul>
        <p>
          Follow-ups are checked read-only through the GitHub App, for 7 days. The Overview page counts incidents with a
          permanent fix still pending.
        </p>
      </DocSection>

      <DocSection id="statuses" title="Incident statuses">
        <Table
          head={["Status", "Means"]}
          rows={[
            ["Open, investigating", "Medic is gathering evidence and diagnosing"],
            ["Waiting approval", "A fix is ready and waits for an owner or approver"],
            ["Applying, verifying", "An approved fix is running, or its result is being checked"],
            ["Resolved", "The signals are healthy again after the fix"],
            ["Failed", "No fix worked, none was approved, or the investigation was stopped"],
            ["Merged", "A newer incident on a related resource took this one over"],
          ]}
        />
        <p>
          Filter the Incidents list by status, severity, and time range. To hear about
          incidents in Slack or by email, see <Link href="/docs/notifications">Notifications</Link>.
        </p>
      </DocSection>
    </DocsArticle>
  );
}
