import type { Metadata } from "next";
import Link from "next/link";

import { Callout, Code, DocSection, DocsArticle, Inline, Step, Steps, Table } from "@/components/public/docs-ui";

export const metadata: Metadata = {
  title: "Use Groundcrew from your assistant",
  description:
    "Connect Claude Code, Cursor, or another assistant to Groundcrew over MCP: ask about incidents, deploys, and your infrastructure from your editor.",
};

const TOC = [
  { id: "what", title: "What it does" },
  { id: "token", title: "Make a token" },
  { id: "connect", title: "Connect your assistant" },
  { id: "tools", title: "What it can do" },
  { id: "safety", title: "Safety" },
];

export default function AssistantsPage() {
  return (
    <DocsArticle
      path="/docs/assistants"
      title="Use Groundcrew from your assistant"
      lead="Groundcrew is an MCP server. Connect Claude Code, Cursor, or any assistant that speaks MCP, and ask it why a service is failing, what Medic found, or how a deploy went, without leaving your editor."
      toc={TOC}
    >
      <DocSection id="what" title="What it does">
        <p>
          Your assistant gets tools that read one Groundcrew workspace: open incidents with Medic&apos;s diagnosis, deploys
          with their plans and errors, agent runs, what is waiting for approval, and the Map. It answers with links back
          to Groundcrew, where the details and every approval live.
        </p>
      </DocSection>

      <DocSection id="token" title="Make a token">
        <Steps>
          <Step title="Open Settings">
            <p>
              In <Link href="/settings">Settings</Link>, under <strong>Assistant access (MCP)</strong>, name the token, choose
              what it can do, and when it expires (30 days to a year). Only owners make tokens.
            </p>
          </Step>
          <Step title="Copy it">
            <p>
              It is shown once, with the setup for Claude Code and Cursor ready to copy. Groundcrew keeps only a hash of it.
            </p>
          </Step>
        </Steps>
      </DocSection>

      <DocSection id="connect" title="Connect your assistant">
        <p>Claude Code:</p>
        <Code>{'claude mcp add --transport http groundcrew https://api.groundkrew.com/mcp --header "Authorization: Bearer gct_..."'}</Code>
        <p>
          Cursor, in <Inline>.cursor/mcp.json</Inline> (or any client that takes a URL and headers):
        </p>
        <Code>{`{
  "mcpServers": {
    "groundcrew": {
      "url": "https://api.groundkrew.com/mcp",
      "headers": { "Authorization": "Bearer gct_..." }
    }
  }
}`}</Code>
        <p>Then ask, for example: &quot;What incidents are open in Groundcrew, and what did Medic find?&quot;</p>
      </DocSection>

      <DocSection id="tools" title="What it can do">
        <Table
          head={["Tool", "What it reads"]}
          rows={[
            [<Inline key="o">overview</Inline>, "Open incidents, approvals waiting, runs and model spend today."],
            [<Inline key="i">list_incidents, get_incident</Inline>, "Incidents, Medic's root cause, evidence, proposed fix, timeline."],
            [<Inline key="d">list_deploys, get_deploy</Inline>, "Deploys: stages, build attempts, the plan with secrets masked, env var names."],
            [<Inline key="r">list_runs, get_run</Inline>, "Agent runs and their steps."],
            [<Inline key="a">list_approvals</Inline>, "What waits for a person, with links."],
            [<Inline key="m">list_resources, get_resource</Inline>, "The Map: what runs where and how it connects."],
          ]}
        />
        <p>
          With a <strong>Read and start work</strong> token, it can also start work. Each of these ends at a plan that a
          person approves in Groundcrew, and the assistant gets the link:
        </p>
        <Table
          head={["Tool", "What it starts"]}
          rows={[
            [<Inline key="t">list_targets</Inline>, "Where deploys can go, and the repositories they can come from."],
            [<Inline key="s">start_deploy</Inline>, "A deploy of a repository (or one service folder of it) to a target."],
            [<Inline key="c">change_settings</Inline>, "Plain env var changes on a live deploy, without a rebuild."],
            [<Inline key="r">report_incident</Inline>, "An incident for resources that look broken; Medic investigates."],
            [<Inline key="f">retry_fix</Inline>, "Another try of a failed fix."],
          ]}
        />
        <p>
          Secrets never pass through an assistant: deploys started from one take no env values, and secret values are set
          on the approval card or with Change settings in Groundcrew.
        </p>
      </DocSection>

      <DocSection id="safety" title="Safety">
        <ul className="list-disc space-y-1 pl-5">
          <li>A token acts as the owner who made it, in that workspace only. If they leave the workspace, it stops.</li>
          <li>No tool approves anything. Changes to your infrastructure still wait for a person in Groundcrew.</li>
          <li>Secret values never appear: env vars are listed by name, secrets are masked.</li>
          <li>
            Revoke a token in Settings at any time; it stops at once. Every token use is limited to 120 calls a minute.
            The activity log in Settings records making and revoking tokens, every request an assistant makes to start
            work (refused ones too), and marks what it started as &quot;via assistant&quot; with the token&apos;s name.
          </li>
        </ul>
        <Callout kind="safety">
          <p>
            What the tools return is data: incident titles, logs, and repository text can contain text written by anyone.
            Every result tells the assistant not to follow instructions found inside it.
          </p>
        </Callout>
      </DocSection>
    </DocsArticle>
  );
}
