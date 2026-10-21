import type { Metadata } from "next";
import Link from "next/link";

import { DocSection, DocsArticle, Faq } from "@/components/public/docs-ui";

export const metadata: Metadata = {
  title: "FAQ",
  description: "Answers to common questions about Groundcrew: access, secrets, approvals, models, clouds, and removing access.",
};

const TOC = [
  { id: "general", title: "General" },
  { id: "access", title: "Access and safety" },
  { id: "agents", title: "Agents and models" },
  { id: "setup", title: "Setup" },
];

export default function FaqPage() {
  return (
    <DocsArticle
      path="/docs/faq"
      title="Frequently asked questions"
      lead="Short answers, with links to the page that explains each one in full."
      toc={TOC}
    >
      <DocSection id="general" title="General">
        <div className="space-y-3">
          <Faq question="What is Groundcrew?">
            <p>
              A DevOps team made of AI agents, for teams without a dedicated DevOps engineer. It connects to the
              infrastructure you already run on AWS, Kubernetes, Vercel, and GitHub, maps it, deploys apps from your
              repositories, and investigates and fixes incidents. Every change waits for an approval from your team.
            </p>
          </Faq>
          <Faq question="Who is it for?">
            <p>
              Small teams and agencies running apps across one or more clouds, who want deploys and incident response
              handled with the same care a DevOps engineer would take, without giving an automated tool free rein.
            </p>
          </Faq>
          <Faq question="Does it replace my CI/CD pipeline or my infrastructure code?">
            <p>
              No. Groundcrew works with your existing tools. Fixes arrive as pull requests on your repositories, so your
              reviews, checks, and deploy workflows still run. It reads your Terraform and workflows to understand your
              system; it does not take them over.
            </p>
          </Faq>
          <Faq question="Can I use it only to watch, without deploys?">
            <p>
              Yes. Every connection starts read-only. You get the map, incident detection, and Medic&apos;s root cause
              analysis with fix pull requests. Deploys and emergency fixes are separate switches you can leave off.
            </p>
          </Faq>
        </div>
      </DocSection>

      <DocSection id="access" title="Access and safety">
        <div className="space-y-3">
          <Faq question="Does Groundcrew store my AWS keys?">
            <p>
              No. You create an IAM role that only Groundcrew can assume, and only with your workspace&apos;s ExternalId.
              Groundcrew stores the role ARN, the ExternalId, and the account ID. Sessions last at most an hour.{" "}
              <Link href="/docs/connect/aws">Connect AWS</Link>.
            </p>
          </Faq>
          <Faq question="Can it read my secrets or my data?">
            <p>
              No. The AWS role has an explicit deny on reading secret values, SSM parameters, KMS decryption, and S3
              objects. The Kubernetes account cannot read Secrets. Vercel values are not kept, with one narrow exception
              described in the Vercel guide. Groundcrew reads configuration, metadata, logs, and metrics, never rows in
              your databases or objects in your buckets.
            </p>
          </Faq>
          <Faq question="Can an agent change production on its own?">
            <p>
              No. Agents read freely, but every write is a plan that an owner or approver approves, tied to that exact
              plan and usable once. <Link href="/docs/approvals">Approvals</Link>.
            </p>
          </Faq>
          <Faq question="Will it merge pull requests or push to my main branch?">
            <p>
              Never. It opens pull requests on new <code>groundcrew/fix-*</code> branches. Merging is up to you. We
              recommend branch protection with a required review on your default branch.
            </p>
          </Faq>
          <Faq question="What if the system changed between my approval and the fix?">
            <p>
              An emergency action checks the current state again just before it writes. If it no longer matches the
              plan you approved, nothing is written and the fix is marked failed with the reason.
            </p>
          </Faq>
          <Faq question="Where does my code get built?">
            <p>
              In an isolated sandbox on Groundcrew&apos;s servers: a rootless container engine with every container in
              gVisor, no route to private networks or cloud metadata, and fixed memory and CPU. Everything is deleted
              when the run ends. <Link href="/docs/deploys#builds">Isolated builds</Link>.
            </p>
          </Faq>
          <Faq question="How do I remove Groundcrew's access?">
            <p>
              Delete the connection in Groundcrew, then remove the access in your account: delete the CloudFormation
              stack, uninstall the GitHub App, delete the Vercel token, or delete the Kubernetes service account.{" "}
              <Link href="/docs/data">Delete data</Link>.
            </p>
          </Faq>
        </div>
      </DocSection>

      <DocSection id="agents" title="Agents and models">
        <div className="space-y-3">
          <Faq question="Which agents are there?">
            <ul>
              <li>
                <strong>Scout</strong> reads repositories and the map, and summarizes your system.
              </li>
              <li>
                <strong>Builder</strong> writes and fixes Dockerfiles and builds in the sandbox, up to 5 attempts.
              </li>
              <li>
                <strong>Deployer</strong> plans, deploys after approval, and verifies.
              </li>
              <li>
                <strong>Medic</strong> investigates incidents, finds the root cause, and proposes fixes.
              </li>
            </ul>
          </Faq>
          <Faq question="Which models do they use?">
            <p>
              NVIDIA Nemotron models through Nebius Token Factory: Nano for Scout, Super for Builder and Deployer, and
              Ultra for Medic&apos;s root cause and fix proposals. Each run shows the model, tokens, and cost of every
              call.
            </p>
          </Faq>
          <Faq question="Is my data used to train models?">
            <p>
              No. See the <Link href="/privacy">Privacy Policy</Link> for what is sent to the model provider and why.
            </p>
          </Faq>
          <Faq question="What if Medic gets the root cause wrong?">
            <p>
              Every claim in a root cause links to the evidence lines it rests on, and claims without real evidence are
              dropped. You review the diagnosis and the fix before approving. After a fix, Medic checks the signals
              again, and the incident only resolves when they are healthy.
            </p>
          </Faq>
          <Faq question="How quickly are incidents noticed?">
            <p>
              The incident watcher checks linked resources every minute. Most rules look at the last 5 minutes, so an
              outage is usually noticed within a few minutes. <Link href="/docs/incidents#rules">What it checks</Link>.
            </p>
          </Faq>
        </div>
      </DocSection>

      <DocSection id="setup" title="Setup">
        <div className="space-y-3">
          <Faq question="Which providers are supported?">
            <p>
              AWS, GitHub, Vercel, and Kubernetes. Azure is in preview: a service principal with Reader on one resource
              group. Direct VM connections are not available yet.
            </p>
          </Faq>
          <Faq question="Which AWS regions?">
            <p>
              Each AWS connection has its own list of up to 10 regions to discover, watch, and act in. An owner changes it
              with <strong>Regions</strong> on the connection. <Link href="/docs/discovery#regions">AWS regions</Link>.
            </p>
          </Faq>
          <Faq question="Can I connect a private Kubernetes cluster?">
            <p>
              Not yet. Groundcrew reaches clusters over the internet, so the API server needs a public address. It also
              refuses kubeconfigs with exec plugins or auth providers; use a service account token.{" "}
              <Link href="/docs/connect/kubernetes">Connect Kubernetes</Link>.
            </p>
          </Faq>
          <Faq question="How do I sign in?">
            <p>
              With GitHub, or with a sign-in link sent to your email address. Both reach the same account when the
              address is the same and verified.
            </p>
          </Faq>
          <Faq question="Why does the GitHub App ask for my email addresses?">
            <p>
              To know which of your addresses GitHub verified at sign-in, so invitations and accounts with the same
              address are matched to you.
            </p>
          </Faq>
          <Faq question="Can I run Groundcrew on my own servers?">
            <p>
              Yes. The repository has a self-hosting guide that covers the platform identity, the GitHub App, email,
              sign-in, limits, and every required setting.
            </p>
          </Faq>
        </div>
      </DocSection>
    </DocsArticle>
  );
}
