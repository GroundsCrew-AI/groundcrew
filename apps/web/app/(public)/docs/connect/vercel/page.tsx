import type { Metadata } from "next";
import Link from "next/link";

import { Callout, DocSection, DocsArticle, Inline, Step, Steps } from "@/components/public/docs-ui";

export const metadata: Metadata = {
  title: "Connect Vercel",
  description: "Add a Vercel team token. Groundcrew reads projects, domains, deployments, and environment variable names.",
};

const TOC = [
  { id: "before", title: "Before you start" },
  { id: "token", title: "Create a token and connect" },
  { id: "reads", title: "What Groundcrew reads" },
  { id: "map", title: "How Vercel shows on the map" },
  { id: "trouble", title: "Troubleshooting" },
  { id: "change", title: "Change or remove" },
];

export default function ConnectVercelPage() {
  return (
    <DocsArticle
      path="/docs/connect/vercel"
      title="Connect Vercel"
      lead="Groundcrew reads your Vercel projects with a token you create and paste. It only ever calls Vercel's read endpoints, and it never asks Vercel to decrypt a value."
      toc={TOC}
    >
      <DocSection id="before" title="Before you start">
        <ul>
          <li>You are an owner of the Groundcrew workspace.</li>
          <li>You are a member of the Vercel team whose projects Groundcrew should read, or it is your personal account.</li>
          <li>
            For the map to show which repository each project deploys from, connect{" "}
            <Link href="/docs/connect/github">GitHub</Link> too.
          </li>
        </ul>
      </DocSection>

      <DocSection id="token" title="Create a token and connect">
        <Steps>
          <Step title="Create the token">
            <p>
              On vercel.com, open <strong>Account Settings</strong>, then <strong>Tokens</strong> (
              <Inline>vercel.com/account/tokens</Inline>). Create a token with your team as its scope, not your whole
              account, and an expiry date you are comfortable with.
            </p>
          </Step>
          <Step title="Find the Team ID">
            <p>
              In the team&apos;s <strong>Settings</strong>, <strong>General</strong>, copy the Team ID. It starts with{" "}
              <Inline>team_</Inline>. For a personal account, leave it empty.
            </p>
          </Step>
          <Step title="Connect">
            <p>
              In Groundcrew, open <strong>Connections</strong> and choose <strong>Add token</strong> on the Vercel card.
              Paste the token and the Team ID and choose <strong>Connect</strong>. Groundcrew checks the token can read
              the team before it saves anything. Then run discovery.
            </p>
          </Step>
        </Steps>
        <p>The token is encrypted before it is stored and is never shown again.</p>
      </DocSection>

      <DocSection id="reads" title="What Groundcrew reads">
        <ul>
          <li>Projects and the Git repository each one is linked to.</li>
          <li>Production domains and recent production deployments.</li>
          <li>
            Env var names and types. Values are not kept, with one exception: a plain variable (not encrypted, sensitive,
            or secret) whose name ends in <Inline>_URL</Inline> or <Inline>_HOST</Inline>. That value shows which backend
            a frontend calls, so the map can draw the line between them.
          </li>
        </ul>
        <Callout kind="safety">
          <p>
            Groundcrew only calls a fixed list of Vercel read endpoints and refuses any other path. It never uses
            Vercel&apos;s decrypt option, so encrypted and sensitive values stay unreadable to it.
          </p>
        </Callout>
      </DocSection>

      <DocSection id="map" title="How Vercel shows on the map">
        <p>
          Each project and its latest production deployment appear on the map. A project linked to a repository gets a
          line to it, and a project whose <Inline>API_URL</Inline>-style variable points at one of your AWS or
          Kubernetes services gets a <em>calls</em> line to that service. When that backend has an incident, Medic
          follows the line and reads the frontend&apos;s recent deployments as evidence too.
        </p>
      </DocSection>

      <DocSection id="trouble" title="Troubleshooting">
        <ul>
          <li>
            <strong>Vercel returned 403.</strong> The token&apos;s scope does not include the team you entered. Create a
            token scoped to that team, or check the Team ID.
          </li>
          <li>
            <strong>Vercel returned 401 or 404.</strong> The token expired or was deleted, or the Team ID is wrong.
            Create a new token and connect again.
          </li>
          <li>
            <strong>No projects on the map.</strong> The Team ID was left empty, so Groundcrew read your personal
            account. Remove the connection and add it again with the Team ID.
          </li>
          <li>
            <strong>A project has no line to its backend.</strong> Its variable is encrypted or sensitive, or its name
            does not end in <Inline>_URL</Inline> or <Inline>_HOST</Inline>. That is expected: Groundcrew does not read
            protected values.
          </li>
        </ul>
      </DocSection>

      <DocSection id="change" title="Change or remove">
        <p>
          When a token expires, the connection shows an error. Remove the connection and add it again with a new token.
        </p>
        <p>
          To disconnect, remove the Vercel connection on the Connections page, which deletes everything Groundcrew
          discovered through it, then delete the token in your Vercel account settings.
        </p>
      </DocSection>
    </DocsArticle>
  );
}
