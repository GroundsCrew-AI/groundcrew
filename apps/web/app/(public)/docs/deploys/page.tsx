import type { Metadata } from "next";
import Link from "next/link";

import { Callout, DocSection, DocsArticle, Inline, Step, Steps, Table } from "@/components/public/docs-ui";

export const metadata: Metadata = {
  title: "Deploys",
  description:
    "Take a GitHub repository to a live URL on Kubernetes or AWS ECS: build and smoke test in a sandbox, approve the plan, deploy, verify.",
};

const TOC = [
  { id: "before", title: "Before you start" },
  { id: "start", title: "Start a deploy" },
  { id: "stages", title: "What happens, stage by stage" },
  { id: "plan", title: "Reviewing the plan" },
  { id: "env", title: "Env vars and secrets" },
  { id: "settings", title: "Change settings without a rebuild" },
  { id: "targets", title: "What gets created" },
  { id: "verify", title: "Verify and rollback" },
  { id: "builds", title: "Isolated builds" },
  { id: "trouble", title: "When a deploy fails" },
  { id: "cost", title: "Cost and cleanup" },
];

export default function DeploysPage() {
  return (
    <DocsArticle
      path="/docs/deploys"
      title="Deploys"
      lead="A deploy takes a repository from GitHub to a live URL in one run. Three agents do the work: Scout reads the repository, Builder builds and smoke-tests it in a sandbox, and Deployer plans, waits for your approval, deploys, and checks that it works."
      toc={TOC}
    >
      <DocSection id="before" title="Before you start">
        <ul>
          <li>
            A <Link href="/docs/connect/github">GitHub connection</Link> with the repository linked.
          </li>
          <li>
            A deploy target: a <Link href="/docs/connect/kubernetes">Kubernetes cluster</Link> connected with the
            deployer account, or an <Link href="/docs/connect/aws">AWS account</Link> with deploys turned on. An AWS
            connection that only observes is refused, with a hint to turn deploys on.
          </li>
          <li>The owner or approver role. Viewers can follow deploys but not start or approve them.</li>
        </ul>
        <p>
          The app needs to be something that runs in a container and answers HTTP on a port. If the repository has a
          Dockerfile, Groundcrew builds it as it is first. If not, Builder writes one.
        </p>
      </DocSection>

      <DocSection id="start" title="Start a deploy">
        <Steps>
          <Step title="Pick the source">
            <p>
              Open <strong>Deploy</strong>. Choose the GitHub connection and the repository, then a branch, tag, or
              commit.
            </p>
          </Step>
          <Step title="Pick the target">
            <p>A Kubernetes cluster, or an AWS account with deploys on.</p>
          </Step>
          <Step title="Add env vars">
            <p>
              Add the variables the app needs. Mark the sensitive ones as secret. You can also leave them out and add
              them at the approval card if Scout finds that the app needs them.
            </p>
          </Step>
          <Step title="Deploy">
            <p>
              The deploy page follows the run live: each stage, Builder&apos;s attempts, and the plan when it is ready.
              You can leave the page; the deploy keeps going and waits for an approval.
            </p>
          </Step>
        </Steps>
      </DocSection>

      <DocSection id="stages" title="What happens, stage by stage">
        <Table
          head={["Stage", "Who", "What happens"]}
          rows={[
            ["Scout", "Scout", "Reads the repository: language, framework, port, health path, and the env vars the code reads."],
            ["Build", "Builder", "Builds the image in the sandbox and starts it to check that it answers HTTP. When the build fails or the app crashes or stays silent, it reads the error and the app's output, changes the Dockerfile, and tries again, up to 5 attempts. Builder never puts your env vars or credentials into the image."],
            ["Smoke", "Builder", "The result of that check for the image that will be deployed. The sandbox cannot reach your databases, so an app that answers but reports itself unhealthy (a 503 from its health check) goes on, with a note; Verify checks health again on the target."],
            ["Plan", "Deployer", "Turns what Scout found into the exact resources for your target."],
            ["Preflight", "Deployer", "Read-only checks: the image passed, required env vars are set, names are free, the target is ready."],
            ["Approval", "You", "The plan waits for an owner or approver."],
            ["Apply", "Deployer", "Pushes the image, then creates or updates each resource in the plan, in order."],
            ["Verify", "Deployer", "Waits for the rollout, then checks the health path over HTTP."],
          ]}
        />
        <Callout kind="safety">
          <p>
            Nothing leaves the sandbox before you approve. Until then the image is only built and smoke-tested; pushing
            it to a registry is the first step of Apply.
          </p>
        </Callout>
      </DocSection>

      <DocSection id="plan" title="Reviewing the plan">
        <p>
          The approval card shows what will happen in your account: every resource Deployer will create or change, the
          image it will push (by its exact digest), the env vars with secret values replaced by{" "}
          <Inline>********</Inline>, and the result of each preflight check.
        </p>
        <ul>
          <li>
            <strong>Approve and deploy</strong> runs this plan once. The approval is bound to the plan itself: if
            anything in it changed, the approval would not apply.
          </li>
          <li>
            <strong>Reject</strong> ends the deploy without touching anything.
          </li>
          <li>
            A failed preflight check disables Approve. Fix the cause and deploy again, or use the options below for a
            missing env var.
          </li>
        </ul>
      </DocSection>

      <DocSection id="env" title="Env vars and secrets">
        <p>
          A deploy takes up to 100 env vars. A secret value goes to a Kubernetes Secret or to AWS Secrets Manager, and
          is never shown again: the deploy page lists names, whether each is secret, and whether a value was given.
          Plain values do appear in the plan, because the plan is what you approve.
        </p>
        <p>
          When preflight fails only because a required variable is missing, the approval card offers{" "}
          <strong>Set value</strong> (a masked input) or <strong>Continue without it</strong>. Either one re-runs only
          preflight with the image already built. Nothing is built again, and the old plan is shown as replaced.
        </p>
        <Callout>
          <p>
            Secret values are encrypted while the deploy is running and removed when it ends. They are never written to
            run steps, approvals, or logs.
          </p>
        </Callout>
      </DocSection>

      <DocSection id="settings" title="Change settings without a rebuild">
        <p>
          To change env vars on a live deploy, open it and choose <strong>Change settings</strong>. Keep each variable,
          give it a new value, or remove it, and add new ones. Nothing is rebuilt: the image running now runs again with
          the new values, so it takes about a minute and no model is used.
        </p>
        <p>
          The approval card marks each variable added, updated, unchanged, or removed, and the change waits for your
          approval like any deploy. Groundcrew never stores values, so the ones you keep stay exactly as they are on the
          target. Only the newest deploy of an app can be changed. Removing variables on a server needs agent 0.6.1 or
          later.
        </p>
      </DocSection>

      <DocSection id="targets" title="What gets created">
        <p>
          <strong>On AWS ECS</strong>, everything is named <Inline>groundcrew-*</Inline> and tagged{" "}
          <Inline>project=groundcrew</Inline>, inside the <Inline>groundcrew-deploys</Inline> cluster that the AWS
          template created:
        </p>
        <ul>
          <li>An ECR repository for the image.</li>
          <li>
            A log group under <Inline>/groundcrew/</Inline>, and a security group for the app&apos;s port.
          </li>
          <li>
            Secrets under <Inline>groundcrew/&lt;app&gt;/</Inline>, written but never read back.
          </li>
          <li>A task definition revision (0.25 vCPU, 0.5 GB) and a Fargate service with a public address.</li>
        </ul>
        <p>
          The live URL is the task&apos;s public address and port. Every task definition revision is kept, so a
          rollback always has somewhere to go back to.
        </p>
        <p>
          <strong>On Kubernetes</strong>, each app gets its own namespace <Inline>gc-&lt;app&gt;</Inline> labelled{" "}
          <Inline>project=groundcrew</Inline>, with a Secret for its secret env vars, a Deployment (250m CPU, 512Mi
          memory, a readiness probe), a Service, and an Ingress at <Inline>&lt;app&gt;.&lt;your app domain&gt;</Inline>.
          The image goes to the registry set in the cluster&apos;s <strong>Deploy settings</strong>.
        </p>
        <p>
          In both cases the deploy appears on the map, with a <em>deployed by</em> line to Groundcrew, and the incident
          watcher starts watching it.
        </p>
      </DocSection>

      <DocSection id="verify" title="Verify and rollback">
        <p>
          Verify waits for the rollout to finish, stopping early on errors such as an image that cannot be pulled or a
          container that keeps crashing. It then checks the health path, or <Inline>/</Inline> when the app has none.
        </p>
        <p>
          If verify fails, Deployer collects the app&apos;s logs and offers a rollback to the previous revision (or, for a
          first deploy, removing it) as an emergency approval: <strong>Approve rollback</strong>. Rejecting it leaves the
          failed deployment in place for you to look at. A rollback never runs on its own.
        </p>
      </DocSection>

      <DocSection id="builds" title="Isolated builds">
        <p>
          Builds and smoke tests of your code run in a sandbox on Groundcrew&apos;s servers: a rootless container engine,
          with each container in gVisor, so a build step never runs directly on the host. The sandbox has no route to
          cloud metadata services, private networks, or Groundcrew&apos;s own services, and it runs one build at a time
          with fixed memory and CPU. Everything a build created is deleted when the run ends.
        </p>
      </DocSection>

      <DocSection id="trouble" title="When a deploy fails">
        <ul>
          <li>
            <strong>The build kept failing.</strong> The deploy page shows each attempt with the failing step and the
            error lines. Builder stops early when the same error comes back after the same kind of fix (&quot;no
            progress&quot;). Fix the cause in the repository and deploy again.
          </li>
          <li>
            <strong>A build took too long.</strong> One build attempt may take at most 15 minutes.
          </li>
          <li>
            <strong>The daily build limit.</strong> A workspace can run 10 deploy builds a day, reset at 00:00 UTC.
          </li>
          <li>
            <strong>Smoke test failed.</strong> The app crashed or did not answer HTTP in the sandbox after every
            attempt. The deploy page shows the app&apos;s last lines. Check the port it listens on and that it starts without
            services it cannot reach from the sandbox.
          </li>
          <li>
            <strong>Verify failed on AWS.</strong> The stopped tasks&apos; reasons are on the deploy page. Common causes
            are a wrong port, a missing env var, or a default VPC without public subnets.
          </li>
        </ul>
        <p>
          You can stop a deploy at any point with <strong>Stop</strong> on its run. Pending approvals are rejected.
        </p>
      </DocSection>

      <DocSection id="cost" title="Cost and cleanup">
        <p>
          Apps run in your account, on your bill. The smallest ECS deploy costs about $12 a month on AWS list prices
          while it runs, mostly Fargate and its public address. Deleting a Groundcrew workspace or connection does not
          stop apps it deployed: remove them in your account. On ECS, delete the service in{" "}
          <Inline>groundcrew-deploys</Inline>, then its <Inline>groundcrew-*</Inline> ECR repository, log group,
          security group, and secrets. On Kubernetes,
          deleting the <Inline>gc-&lt;app&gt;</Inline> namespace removes everything the deploy created.
        </p>
      </DocSection>
    </DocsArticle>
  );
}
