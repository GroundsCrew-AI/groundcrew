import type { Metadata } from "next";
import Link from "next/link";

import { Article, Section } from "@/components/public/prose";

export const metadata: Metadata = {
  title: "Docs",
  description: "How to connect AWS, GitHub, Vercel, and Kubernetes to Groundcrew, and what each connection can do.",
};

const GUIDES = [
  {
    href: "/docs/connect/aws",
    title: "Connect AWS",
    body: "Create a read-only role from our CloudFormation template, then turn on deploys or emergency fixes if you want them.",
  },
  {
    href: "/docs/connect/github",
    title: "Connect GitHub",
    body: "Install the GitHub App on the repositories Groundcrew should read, build, and open fix pull requests on.",
  },
  {
    href: "/docs/connect/vercel",
    title: "Connect Vercel",
    body: "Paste a team token. Groundcrew reads projects, domains, deployments, and environment variable names.",
  },
  {
    href: "/docs/connect/kubernetes",
    title: "Connect Kubernetes",
    body: "Apply a read-only service account, paste its kubeconfig, and optionally allow deploys and emergency fixes.",
  },
] as const;

export default function DocsPage() {
  return (
    <Article
      title="Docs"
      lead="Groundcrew starts read-only. Each guide shows exactly what a connection can see and do, and how to remove it."
    >
      <section aria-labelledby="connect-title">
        <h2 id="connect-title" className="text-[17px] font-semibold tracking-tight">
          Connect your infrastructure
        </h2>
        <ul className="mt-3 grid gap-3 sm:grid-cols-2">
          {GUIDES.map((g) => (
            <li key={g.href}>
              <Link
                href={g.href}
                className="block h-full rounded-xl border p-4 outline-none hover:bg-muted focus-visible:ring-3 focus-visible:ring-ring/50"
              >
                <span className="text-[14px] font-semibold">{g.title}</span>
                <span className="mt-1 block text-[13px] text-muted-foreground">{g.body}</span>
              </Link>
            </li>
          ))}
        </ul>
      </section>
      <Section id="next" title="After connecting">
        <p>
          Run discovery from the Connections page. Groundcrew streams what it finds and builds the map of your resources
          and how they connect. From there you can start a deploy, report an incident, or let the watcher open incidents
          when signals go bad. Every change waits for an owner or approver. How that works is on the{" "}
          <Link href="/security">Security</Link> page.
        </p>
      </Section>
    </Article>
  );
}
