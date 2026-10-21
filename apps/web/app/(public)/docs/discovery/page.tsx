import type { Metadata } from "next";
import Link from "next/link";

import { Callout, DocSection, DocsArticle, Inline, SubHeading, Table } from "@/components/public/docs-ui";

export const metadata: Metadata = {
  title: "Map and discovery",
  description:
    "How Groundcrew discovers resources across AWS, GitHub, Vercel, and Kubernetes, links them into one map, and keeps it current.",
};

const TOC = [
  { id: "run", title: "Running discovery" },
  { id: "reads", title: "What each connection reads" },
  { id: "links", title: "How resources are linked" },
  { id: "coverage", title: "Coverage" },
  { id: "map", title: "Reading the map" },
  { id: "regions", title: "AWS regions" },
  { id: "stale", title: "Stale and missing resources" },
  { id: "limits", title: "Limits" },
];

export default function DiscoveryPage() {
  return (
    <DocsArticle
      path="/docs/discovery"
      title="Map and discovery"
      lead="Discovery reads every connection in your workspace and builds one map: what you run, where it runs, and how the pieces depend on each other. Every later step, from deploys to incident investigations, starts from this map."
      toc={TOC}
    >
      <DocSection id="run" title="Running discovery">
        <p>
          On the Connections page, choose <strong>Run discovery</strong> to read every connection at once, or{" "}
          <strong>Refresh</strong> on one connection to read only that one. Owners and approvers can start discovery;
          viewers see the results.
        </p>
        <p>
          Discovery is a run like any other, so you can watch it live under <strong>Runs</strong>: one step per
          connection, then Scout&apos;s summary, then a result line with what changed. A connection that fails is
          marked with the reason on the Connections page, and the others still finish. Run discovery again after you fix
          it.
        </p>
        <Callout>
          <p>
            Discovery only reads. On AWS it uses the read-only role, on GitHub short-lived installation tokens, on
            Vercel your token through read endpoints, and on Kubernetes your service account with get, list, and watch.
          </p>
        </Callout>
      </DocSection>

      <DocSection id="reads" title="What each connection reads">
        <SubHeading>AWS</SubHeading>
        <p>
          A Resource Explorer search in each of the connection&apos;s regions finds what exists, then read calls fill in
          the configuration: ECS clusters and services with their task definitions, ECR repositories, RDS databases and
          Aurora clusters, Lambda functions, load balancers, EC2 instances, VPCs and security groups, IAM roles, log
          groups, and Secrets Manager secrets (name, tags, and when they changed, never the value). SQS queues, SNS
          topics, DynamoDB tables, ElastiCache clusters, API Gateway APIs, CloudFront distributions, and EventBridge rules
          appear on the map too.
        </p>
        <SubHeading>GitHub</SubHeading>
        <p>
          For each linked repository: its metadata, recent commits, workflows and recent workflow runs. Groundcrew also
          takes a shallow copy of the repository, reads what it says about your system, and deletes the copy:
        </p>
        <ul>
          <li>Terraform resources and what each one references.</li>
          <li>GitHub Actions workflows: triggers, roles they assume, images they push, ECS and Vercel deploy steps.</li>
          <li>Dockerfiles, compose files, and the names of env vars the code reads.</li>
        </ul>
        <p>
          Only names and structure are kept. <Inline>.env</Inline> files are skipped, and nothing in your repository is
          run during discovery.
        </p>
        <SubHeading>Vercel</SubHeading>
        <p>
          Projects and the repository each one is linked to, production domains, recent production deployments, and
          env var names and types. <Link href="/docs/connect/vercel">Connect Vercel</Link> lists the one kind of value
          that is kept.
        </p>
        <SubHeading>Kubernetes</SubHeading>
        <p>
          Namespaces, Deployments, StatefulSets, Services, and Ingresses. Pods, events, and pod logs are read when an
          incident is checked or investigated. Secrets and ConfigMaps are not listed.
        </p>
      </DocSection>

      <DocSection id="links" title="How resources are linked">
        <p>
          After every discovery Groundcrew works out the lines between resources from what it read. Each line has a kind
          and keeps the evidence it came from, so you can see why two things are connected.
        </p>
        <Table
          head={["Line", "Means", "Example"]}
          rows={[
            [<strong key="c">calls</strong>, "Sends requests to", "A Vercel frontend calls an API on ECS"],
            [<strong key="r">reads</strong>, "Reads data from", "A service reads an RDS database"],
            [<strong key="u">uses</strong>, "Gets a secret or config from", "A task definition uses a secret"],
            [<strong key="p">pulls from</strong>, "Pulls its image from", "A service pulls from an ECR repository"],
            [<strong key="l">logs to</strong>, "Sends its logs to", "A service logs to a CloudWatch log group"],
            [<strong key="d">deployed by</strong>, "Is deployed by", "A service is deployed by a GitHub workflow"],
            [<strong key="f">defined in</strong>, "Is defined in", "A database is defined in a Terraform file"],
          ]}
        />
        <p>
          Lines across providers are the point of the map. When an incident starts on a service, Medic follows these
          lines to the database it reads, the secret it uses, the log group it writes to, and the commit that deployed
          it.
        </p>
      </DocSection>

      <DocSection id="coverage" title="Coverage">
        <p>Every resource has one of three coverage levels, shown on its details panel:</p>
        <ul>
          <li>
            <strong>Observed</strong>: Groundcrew knows it exists and how it is configured.
          </li>
          <li>
            <strong>Diagnosable</strong>: Groundcrew can also read its logs or metrics, so Medic can investigate it.
            A service usually becomes diagnosable through its <em>logs to</em> line.
          </li>
          <li>
            <strong>Actionable</strong>: a deploy target or an emergency fix can change it, after an approval.
          </li>
        </ul>
      </DocSection>

      <DocSection id="map" title="Reading the map">
        <p>
          The map groups resources by provider. Use{" "}
          <strong>Providers</strong> and <strong>Show</strong> above the map to filter what you see. Select a resource to
          open its panel: its type, region, tags, configuration, coverage, and every line to and from it, with the
          evidence for each.
        </p>
        <p>
          At the top, Scout&apos;s summary describes the system in a few sentences: what the main apps are, what they
          depend on, and what looks unusual. It is rewritten after each discovery.
        </p>
      </DocSection>

      <DocSection id="regions" title="AWS regions">
        <p>
          Each AWS connection has a list of regions Groundcrew discovers, watches for incidents, and acts in. An owner
          changes it with <strong>Regions</strong> on the connection, up to 10 regions. The roles are global, so no stack
          update is needed; Groundcrew refreshes the map after you save.
        </p>
        <p>
          The Resource Explorer index the template creates covers the region you created the stack in. In a region
          without an index, Groundcrew finds resources through the AWS tagging API instead and says so in the discovery
          run.
        </p>
      </DocSection>

      <DocSection id="stale" title="Stale and missing resources">
        <p>
          A resource that the latest complete discovery of its connection did not see is marked stale: it stays in the
          workspace, is hidden from the map by default, and its panel says &quot;Not seen in the last discovery&quot;.
          To see them, tick <strong>Not seen in the last discovery</strong> under <strong>Show</strong>. Lines are worked out again on every discovery
          from the resources that are not stale.
        </p>
        <p>If something you expect is missing:</p>
        <ul>
          <li>Check it is in one of the connection&apos;s regions.</li>
          <li>For a repository, check it is linked on the GitHub connection, not only shared with the app on GitHub.</li>
          <li>For Kubernetes, check the service account can list that kind in that namespace.</li>
          <li>Look at the discovery run&apos;s steps under Runs. Each connection&apos;s step lists notes about what it could not read.</li>
        </ul>
      </DocSection>

      <DocSection id="limits" title="Limits">
        <p>
          Discovery keeps up to 2,000 resources per connection, and says so in the run when it found more. Each
          connection has about 100 seconds; one that takes longer is marked as failed with the reason, and the others
          still finish. A workspace can have up to 10 connections. <Link href="/docs/limits">All limits</Link>.
        </p>
      </DocSection>
    </DocsArticle>
  );
}
