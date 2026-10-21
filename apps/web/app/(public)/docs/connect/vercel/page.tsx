import type { Metadata } from "next";

import { Article, Inline, Section } from "@/components/public/prose";

export const metadata: Metadata = {
  title: "Connect Vercel",
  description: "Add a Vercel team token. Groundcrew reads projects, domains, deployments, and environment variable names.",
};

export default function ConnectVercelPage() {
  return (
    <Article
      title="Connect Vercel"
      lead="Groundcrew reads your Vercel projects with a token you create and paste. It only ever calls Vercel's read endpoints."
    >
      <Section id="token" title="Create a token">
        <ol>
          <li>
            On vercel.com, open <strong>Account Settings</strong>, then <strong>Tokens</strong>.
          </li>
          <li>
            Create a token with the narrowest scope that covers your projects: your team, not your whole account, and an
            expiry date you are comfortable with.
          </li>
          <li>
            Find your Team ID in the team&apos;s <strong>Settings</strong>, <strong>General</strong>. It starts with{" "}
            <Inline>team_</Inline>.
          </li>
          <li>
            In Groundcrew, open <strong>Connections</strong>, choose <strong>Add token</strong> on the Vercel card, paste the
            token and the Team ID, and save. Then run discovery.
          </li>
        </ol>
        <p>The token is encrypted before it is stored and is never shown again.</p>
      </Section>

      <Section id="reads" title="What Groundcrew reads">
        <ul>
          <li>Projects and the Git repository each one is linked to.</li>
          <li>Production domains and recent production deployments.</li>
          <li>
            Environment variable names and types. Values are not kept, with one exception: a plain (not encrypted or
            sensitive) variable whose name ends in <Inline>_URL</Inline> or <Inline>_HOST</Inline>, which helps Groundcrew
            see which backend a frontend calls. Groundcrew never uses Vercel&apos;s decrypt option.
          </li>
        </ul>
      </Section>

      <Section id="disconnect" title="Disconnect">
        <p>
          Delete the Vercel connection on the Connections page, then delete the token in your Vercel account settings.
        </p>
      </Section>
    </Article>
  );
}
