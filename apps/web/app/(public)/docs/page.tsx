import { ArrowRight, BookOpen, Cable, LifeBuoy, Rocket, Users } from "lucide-react";
import type { Metadata } from "next";
import Link from "next/link";

import { DocsArticle } from "@/components/public/docs-ui";

export const metadata: Metadata = {
  title: "Docs",
  description:
    "Connect AWS, GitHub, Vercel, and Kubernetes to Groundcrew, then map, deploy, and fix incidents with approvals.",
};

const FOCUS = "outline-none focus-visible:ring-3 focus-visible:ring-ring/50";

const QUICK_START = [
  { title: "Sign in", body: "With GitHub or an emailed link. You get a workspace of your own." },
  { title: "Connect", body: "AWS with a read-only role, GitHub with the app, Vercel or Kubernetes with a token." },
  { title: "Discover", body: "Groundcrew maps your resources and how they connect." },
  { title: "Deploy and fix", body: "Approve deploy plans and incident fixes before anything changes." },
] as const;

const GROUPS = [
  {
    icon: Cable,
    title: "Connect",
    body: "What each connection can see and do, how to create it, and how to remove it.",
    links: [
      { href: "/docs/connect/aws", title: "AWS" },
      { href: "/docs/connect/github", title: "GitHub" },
      { href: "/docs/connect/vercel", title: "Vercel" },
      { href: "/docs/connect/kubernetes", title: "Kubernetes" },
    ],
  },
  {
    icon: Rocket,
    title: "Use Groundcrew",
    body: "The map, deploys from a repository to a live URL, incidents, and who hears about them.",
    links: [
      { href: "/docs/discovery", title: "Map and discovery" },
      { href: "/docs/deploys", title: "Deploys" },
      { href: "/docs/incidents", title: "Incidents and fixes" },
      { href: "/docs/notifications", title: "Notifications" },
    ],
  },
  {
    icon: Users,
    title: "Workspaces",
    body: "Members and roles, how approvals work, the activity log, limits, and deleting data.",
    links: [
      { href: "/docs/roles", title: "Members and roles" },
      { href: "/docs/approvals", title: "Approvals and audit log" },
      { href: "/docs/limits", title: "Limits and usage" },
      { href: "/docs/data", title: "Delete data" },
    ],
  },
  {
    icon: LifeBuoy,
    title: "Reference",
    body: "The full permission model, and answers to the questions people ask most.",
    links: [
      { href: "/security", title: "Security model" },
      { href: "/docs/faq", title: "FAQ" },
    ],
  },
] as const;

export default function DocsPage() {
  return (
    <DocsArticle
      path="/docs"
      title="Groundcrew docs"
      lead="Groundcrew connects to the infrastructure you already run, maps it, deploys your apps, and investigates incidents. It reads freely with scoped access and changes nothing without an approval from your team."
    >
      <section aria-labelledby="quick-start-title">
        <div className="flex items-baseline justify-between gap-4">
          <h2 id="quick-start-title" className="text-[21px] font-semibold tracking-tight">
            Quick start
          </h2>
          <Link
            href="/docs/getting-started"
            className={`inline-flex min-h-11 items-center gap-1 rounded-md text-[14px] font-medium text-muted-foreground transition-colors duration-150 hover:text-foreground ${FOCUS}`}
          >
            Full guide
            <ArrowRight aria-hidden="true" className="size-3.5" />
          </Link>
        </div>
        <ol className="mt-4 grid gap-px overflow-hidden rounded-xl border bg-border sm:grid-cols-2">
          {QUICK_START.map((step, i) => (
            <li key={step.title} className="flex gap-3 bg-background p-4">
              <span
                aria-hidden="true"
                className="flex size-7 shrink-0 items-center justify-center rounded-full border font-mono text-[12px] text-muted-foreground"
              >
                {i + 1}
              </span>
              <div>
                <p className="text-[15px] font-semibold">{step.title}</p>
                <p className="mt-1 text-[14.5px] leading-[1.55] text-muted-foreground">{step.body}</p>
              </div>
            </li>
          ))}
        </ol>
      </section>

      <section aria-labelledby="guides-title">
        <h2 id="guides-title" className="text-[21px] font-semibold tracking-tight">
          Guides
        </h2>
        <ul className="mt-4 grid gap-4 sm:grid-cols-2">
          {GROUPS.map((group) => (
            <li key={group.title} className="flex flex-col rounded-xl border p-5">
              <group.icon aria-hidden="true" className="size-5 text-primary" />
              <h3 className="mt-3 text-[16px] font-semibold">{group.title}</h3>
              <p className="mt-1 text-[14.5px] leading-[1.55] text-muted-foreground">{group.body}</p>
              <ul className="mt-3 space-y-0.5">
                {group.links.map((link) => (
                  <li key={link.href}>
                    <Link
                      href={link.href}
                      className={`-mx-2 flex min-h-11 items-center justify-between rounded-md px-2 text-[14.5px] transition-colors duration-150 hover:bg-muted sm:min-h-9 ${FOCUS}`}
                    >
                      {link.title}
                      <ArrowRight aria-hidden="true" className="size-3.5 text-muted-foreground" />
                    </Link>
                  </li>
                ))}
              </ul>
            </li>
          ))}
        </ul>
      </section>

      <section aria-labelledby="principles-title" className="rounded-xl border bg-card p-5">
        <div className="flex items-center gap-2">
          <BookOpen aria-hidden="true" className="size-4 text-muted-foreground" />
          <h2 id="principles-title" className="text-[16px] font-semibold">
            Four rules Groundcrew keeps
          </h2>
        </div>
        <ul className="mt-3 list-disc space-y-2 pl-5 text-[15px] leading-[1.6] text-foreground/90">
          <li>It reads with scoped access and never reads secret values or your data.</li>
          <li>Nothing changes your infrastructure until an owner or approver approves the exact plan.</li>
          <li>Fixes go through pull requests on your repositories. It never pushes to your default branch or merges.</li>
          <li>A fix is only called done after Groundcrew checks the signals that showed the problem.</li>
        </ul>
      </section>
    </DocsArticle>
  );
}
