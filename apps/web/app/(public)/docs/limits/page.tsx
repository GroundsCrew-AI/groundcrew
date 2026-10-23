import type { Metadata } from "next";
import Link from "next/link";

import { Callout, DocSection, DocsArticle, Table } from "@/components/public/docs-ui";

export const metadata: Metadata = {
  title: "Limits and usage",
  description:
    "The limits each Groundcrew workspace has, the daily model spend cap, where to see usage, and what happens when a limit is reached.",
};

const TOC = [
  { id: "why", title: "Why there are limits" },
  { id: "workspace", title: "Workspace limits" },
  { id: "spend", title: "Model spend" },
  { id: "usage", title: "Seeing your usage" },
  { id: "reached", title: "When a limit is reached" },
  { id: "requests", title: "Request limits" },
  { id: "more", title: "Need more?" },
];

export default function LimitsPage() {
  return (
    <DocsArticle
      path="/docs/limits"
      title="Limits and usage"
      lead="Every workspace has limits on what it can use in a day and at once. They keep one workspace from using up capacity that everyone shares, and they keep a runaway agent from running up costs."
      toc={TOC}
    >
      <DocSection id="why" title="Why there are limits">
        <p>
          Groundcrew&apos;s servers run builds, agents, and language model calls for every workspace. Limits make sure a
          busy or misbehaving workspace slows down on its own instead of slowing down everyone else. They also cap what
          an agent can spend before a person looks at it.
        </p>
      </DocSection>

      <DocSection id="workspace" title="Workspace limits">
        <Table
          head={["Limit", "Default", "Notes"]}
          rows={[
            ["Runs going at once", "2", "Runs a person started: deploys, discovery, and investigations of reported incidents. The watcher's investigations do not count."],
            ["Deploy builds", "10 a day", "Each deploy that reaches the build stage counts once, however many attempts Builder makes."],
            ["Longest build attempt", "15 minutes", "An attempt that takes longer is stopped, and its build sandbox with it."],
            ["Web research", "10 a day", "Medic's web searches during investigations."],
            ["Connections", "10", "All kinds together."],
            ["Resources per connection", "2,000", "Discovery keeps the first 2,000 and says so."],
          ]}
        />
        <p>Daily limits reset at 00:00 UTC.</p>
      </DocSection>

      <DocSection id="spend" title="Model spend">
        <p>
          Agents call NVIDIA Nemotron models through Nebius Token Factory: Scout on Nano, Builder and Deployer on Super,
          and Medic&apos;s root cause on Ultra. Every model call shows its model, tokens, and cost on the run, and each
          deploy and incident shows its total.
        </p>
        <p>
          Each workspace has a daily cap on model spend. By default it is the platform&apos;s daily cap. An owner can set
          a lower cap for the workspace in <strong>Settings</strong>, <strong>Approvals and limits</strong>, but cannot
          raise it above the platform&apos;s. Once the workspace reaches its cap, agents in it stop calling models for the
          rest of the UTC day and say why.
        </p>
        <Callout>
          <p>
            The platform also has overall caps. If one is reached, agents everywhere stop with a message saying so,
            until it resets.
          </p>
        </Callout>
      </DocSection>

      <DocSection id="usage" title="Seeing your usage">
        <p>
          <strong>Settings</strong>, <strong>Usage</strong> shows today&apos;s model spend against the cap, and each limit
          with what the workspace used of it. The Overview page shows model usage for the day as well.
        </p>
      </DocSection>

      <DocSection id="reached" title="When a limit is reached">
        <p>
          Groundcrew refuses the new run or connection with a message that names the limit and when it resets, for
          example: <em>This workspace reached its limit of 10 deploy builds today. It resets at 00:00 UTC.</em>
        </p>
        <ul>
          <li>
            A deploy that reaches its build stage after the build limit is used up stops there with that message.
            Nothing is built or pushed.
          </li>
          <li>The incident watcher keeps opening incidents, so you still hear about outages.</li>
          <li>
            When web research is used up, Medic investigates without it and the incident says research was skipped.
          </li>
        </ul>
      </DocSection>

      <DocSection id="requests" title="Request limits">
        <p>Per person, the service also limits:</p>
        <ul>
          <li>300 requests a minute.</li>
          <li>10 live run streams open at once.</li>
          <li>Request bodies of 1 MB. A kubeconfig or env var list larger than that is refused.</li>
        </ul>
        <p>
          New sign-ups are limited per hour and per address, and can be paused for a while if needed. People invited to
          a workspace can always join.
        </p>
      </DocSection>

      <DocSection id="more" title="Need more?">
        <p>
          Limits can be raised for one workspace by the people who run Groundcrew. Ask through the contact on the{" "}
          <Link href="/security">Security</Link> page, with your workspace name and which limit you need raised. When you
          run your own Groundcrew server, every limit is a setting; see the repository&apos;s self-hosting guide.
        </p>
      </DocSection>
    </DocsArticle>
  );
}
