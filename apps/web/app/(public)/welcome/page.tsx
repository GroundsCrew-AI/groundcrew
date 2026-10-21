import {
  ArrowRight,
  Boxes,
  Cloud,
  Container,
  FileCheck2,
  GitBranch,
  KeyRound,
  ListChecks,
  Network,
  ScrollText,
  ShieldCheck,
  Siren,
  Triangle,
  Users,
} from "lucide-react";
import type { Metadata } from "next";
import Link from "next/link";
import type { CSSProperties, ReactNode } from "react";

import { ApprovalMockup, DeployMockup, FixMockup, IncidentMockup, MapMockup } from "@/components/public/landing-mockups";
import { Button } from "@/components/ui/button";

export const metadata: Metadata = {
  title: "DevOps that asks first",
  description:
    "Groundcrew maps your AWS, Kubernetes, GitHub, and Vercel into one graph, deploys your apps, and fixes incidents. Every change waits for your approval.",
};

const FOCUS = "outline-none focus-visible:ring-3 focus-visible:ring-ring/50";

const INTEGRATIONS = [
  { icon: Cloud, name: "AWS" },
  { icon: Container, name: "Kubernetes" },
  { icon: GitBranch, name: "GitHub" },
  { icon: Triangle, name: "Vercel" },
  { icon: Cloud, name: "Azure", note: "Preview" },
] as const;

const FEATURES = [
  {
    id: "map",
    icon: Network,
    eyebrow: "Discovery",
    title: "One map of what runs where",
    body: "Connect a cloud account, a cluster, repositories, and Vercel projects. Groundcrew reads them and draws one graph of what calls what across providers.",
    points: [
      "Every link carries its evidence: an environment variable name, a security group rule, a DNS record.",
      "A short written summary of each system, kept up to date when you refresh.",
      "Environment variable names only. Groundcrew never reads secret values.",
    ],
    mockup: <MapMockup />,
  },
  {
    id: "deploy",
    icon: Boxes,
    eyebrow: "Deploys",
    title: "From a repository to a running service",
    body: "Pick a repository and a target. Scout reads the code, Builder writes the Dockerfile and builds it in an isolated sandbox, and Deployer shows the exact plan.",
    points: [
      "When a build fails, Builder reads the log, patches the Dockerfile, and tries again, up to five times.",
      "Each image is smoke-tested before anything is pushed.",
      "Targets: ECS on AWS or your own Kubernetes cluster. Nothing is applied until it is approved.",
    ],
    mockup: <DeployMockup />,
  },
  {
    id: "incidents",
    icon: Siren,
    eyebrow: "Incidents",
    title: "A root cause you can check",
    body: "The watcher looks at your linked services and opens an incident when a rollout fails, tasks or pods crash, health checks fail, errors spike in the logs, or a database slows down.",
    points: [
      "Medic reads logs, metrics, recent deploys, and commits, then names the cause.",
      "Every claim links to the log line, metric, or commit it came from.",
      "Past incidents with the same cause are found and reused.",
    ],
    mockup: <IncidentMockup />,
  },
  {
    id: "fix",
    icon: FileCheck2,
    eyebrow: "Fixes",
    title: "Fixes that are proven, not promised",
    body: "Medic proposes a pull request on your repository. When production is down now, it also proposes an emergency action, such as rolling an ECS service or a Lambda alias back to the previous version.",
    points: [
      "Pull requests go through your own review. Groundcrew never merges or pushes to main.",
      "Emergency actions show the exact change and run only after approval.",
      "After a fix, Groundcrew checks the signals that opened the incident until they recover.",
    ],
    mockup: <FixMockup />,
  },
] as const;

const SAFETY = [
  {
    icon: KeyRound,
    title: "A role you create, no stored keys",
    body: "AWS access is a role in your account with your own ExternalId. Observe can read configuration and logs; it cannot read secret values or your data.",
  },
  {
    icon: ListChecks,
    title: "Approvals on the exact plan",
    body: "A deploy, rollback, or restart runs only after an owner or approver approves it. If the plan changes after approval, the approval no longer counts.",
  },
  {
    icon: Container,
    title: "Builds in a sandbox",
    body: "Your code is built and smoke-tested in a gVisor sandbox with no route to the cloud metadata service or private networks.",
  },
  {
    icon: ScrollText,
    title: "An audit log",
    body: "Connections, approvals, deploys, invites, role changes, and deletions are recorded with who did them and when.",
  },
  {
    icon: Users,
    title: "Roles for your team",
    body: "Owners manage connections and members, approvers approve plans, viewers see everything and change nothing.",
  },
  {
    icon: ShieldCheck,
    title: "Delete it all",
    body: "An owner can delete a workspace and everything Groundcrew stored about it. Groundcrew cannot remove the role in your account, so it tells you how.",
  },
] as const;

const STEPS = [
  {
    title: "Sign in",
    body: "With GitHub or an email link. You get a workspace of your own; invite your team when you are ready.",
  },
  {
    title: "Connect",
    body: "Create the read-only AWS role from a template, install the GitHub App on the repositories you pick, or add a cluster or Vercel token.",
  },
  {
    title: "Discover",
    body: "Groundcrew builds the map and a summary of each system, so you see what runs where in a few minutes.",
  },
  {
    title: "Deploy and respond",
    body: "Start a deploy, or let the watcher open incidents. Approve the plans you agree with and watch each step live.",
  },
] as const;

const AGENTS = [
  { name: "Scout", model: "Nemotron Nano", color: "var(--tier-nano)", body: "Reads repositories and the map. Finds how an app builds and runs." },
  { name: "Builder", model: "Nemotron Super", color: "var(--tier-super)", body: "Writes the Dockerfile, builds in the sandbox, and fixes failed builds." },
  { name: "Deployer", model: "Nemotron Super", color: "var(--tier-super)", body: "Turns the app into a target plan, deploys it after approval, and checks it." },
  { name: "Medic", model: "Nemotron Ultra", color: "var(--tier-ultra)", body: "Finds the root cause of an incident, proposes a fix, and verifies recovery." },
] as const;

const FAQ = [
  {
    q: "Can Groundcrew change my infrastructure without asking?",
    a: "No. It reads freely within the access you grant. Every change to a resource is a plan that an owner or approver approves first, or a pull request that goes through your own review.",
  },
  {
    q: "What access does it need in AWS?",
    a: "A role you create in your account from Groundcrew's CloudFormation template. Observe is read-only. Deploy adds the rights to run services in a cluster Groundcrew creates for itself. You can delete the stack at any time.",
  },
  {
    q: "Does it read my secrets or customer data?",
    a: "No. It reads names and configuration: environment variable names, log lines, metrics, resource settings. The role is denied reading secret values, parameters, and S3 objects, and it has no permission to query your databases.",
  },
  {
    q: "What happens to my code during a build?",
    a: "Groundcrew fetches the repository with a short-lived GitHub App token, builds it in an isolated sandbox, smoke-tests it, and removes the containers, images, and build cache when the run ends.",
  },
  {
    q: "Which models run the agents?",
    a: "NVIDIA Nemotron Nano, Super, and Ultra through Nebius Token Factory. Each task goes to the smallest model that does it well, and each workspace has a daily spending cap.",
  },
  {
    q: "Can I run Groundcrew myself?",
    a: "Yes. The source is on GitHub with a self-hosting guide for a single server running Docker Compose.",
  },
] as const;

function SectionHeading({ id, eyebrow, title, children }: { id: string; eyebrow: string; title: string; children?: ReactNode }) {
  return (
    <div className="max-w-2xl">
      <p className="text-[13px] font-medium text-primary">{eyebrow}</p>
      <h2 id={id} className="mt-2 scroll-mt-24 text-[26px] leading-tight font-semibold tracking-tight sm:text-[30px]">
        {title}
      </h2>
      {children && <p className="mt-3 text-[15.5px] leading-relaxed text-muted-foreground">{children}</p>}
    </div>
  );
}

export default function WelcomePage() {
  return (
    <>
      <section aria-labelledby="hero" className="relative overflow-hidden">
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-0 [background-image:linear-gradient(to_right,var(--border)_1px,transparent_1px),linear-gradient(to_bottom,var(--border)_1px,transparent_1px)] [background-size:40px_40px] opacity-40 [mask-image:radial-gradient(ellipse_at_top,black,transparent_70%)]"
        />
        <div className="relative mx-auto grid w-full max-w-6xl grid-cols-1 gap-12 px-4 pt-14 pb-16 sm:px-6 sm:pt-20 lg:grid-cols-[1.05fr_1fr] lg:items-center lg:pb-24">
          <div>
            <p className="inline-flex items-center gap-2 rounded-full border bg-background px-3 py-1 text-[12.5px] font-medium text-muted-foreground">
              <span className="size-1.5 rounded-full bg-primary" />
              An AI DevOps team for small teams
            </p>
            <h1
              id="hero"
              className="mt-5 text-[36px] leading-[1.08] font-semibold tracking-tight text-balance sm:text-[48px]"
            >
              Deploys and incident fixes that wait for your approval.
            </h1>
            <p className="mt-5 max-w-xl text-[16.5px] leading-relaxed text-muted-foreground">
              Groundcrew connects to the infrastructure you already run, maps it into one graph, deploys your apps,
              and finds and fixes what breaks. It reads freely and changes nothing until a person says yes.
            </p>
            <div className="mt-8 flex flex-wrap items-center gap-3">
              <Button asChild size="lg" className="h-11 px-5 text-[14.5px]">
                <Link href="/signin">
                  Get started
                  <ArrowRight aria-hidden="true" />
                </Link>
              </Button>
              <Button asChild size="lg" variant="outline" className="h-11 px-5 text-[14.5px]">
                <Link href="/docs/getting-started">Read the quick start</Link>
              </Button>
            </div>
            <p className="mt-6 text-[12.5px] text-subtle">
              Sign in with GitHub or an email link. Read-only until you approve a change.
            </p>
          </div>
          <div className="min-w-0 lg:pl-4">
            <ApprovalMockup />
          </div>
        </div>
      </section>

      <section aria-label="Works with" className="border-y bg-card">
        <div className="mx-auto flex w-full max-w-6xl flex-wrap items-center gap-x-8 gap-y-3 px-4 py-6 sm:px-6">
          <p className="text-[12.5px] font-medium text-muted-foreground">Works with what you run</p>
          <ul className="flex flex-wrap items-center gap-x-7 gap-y-3">
            {INTEGRATIONS.map(({ icon: Icon, name, ...rest }) => (
              <li key={name} className="flex items-center gap-2 text-[14px] font-medium">
                <Icon aria-hidden="true" className="size-4 text-muted-foreground" />
                {name}
                {"note" in rest && <span className="text-[11.5px] font-normal text-subtle">{rest.note}</span>}
              </li>
            ))}
          </ul>
        </div>
      </section>

      {FEATURES.map((f, i) => (
        <section key={f.id} aria-labelledby={f.id} className={i % 2 === 1 ? "border-b bg-card" : "border-b"}>
          <div className="mx-auto grid w-full max-w-6xl grid-cols-1 gap-10 px-4 py-16 sm:px-6 sm:py-20 lg:grid-cols-2 lg:items-center lg:gap-16">
            <div className={i % 2 === 1 ? "min-w-0 lg:order-2" : "min-w-0"}>
              <f.icon aria-hidden="true" className="size-5 text-primary" />
              <div className="mt-3">
                <SectionHeading id={f.id} eyebrow={f.eyebrow} title={f.title}>
                  {f.body}
                </SectionHeading>
              </div>
              <ul className="mt-6 space-y-3">
                {f.points.map((p) => (
                  <li key={p} className="flex gap-3 text-[14.5px] leading-relaxed">
                    <span aria-hidden="true" className="mt-2.5 h-px w-3 shrink-0 bg-primary" />
                    {p}
                  </li>
                ))}
              </ul>
            </div>
            <div className={i % 2 === 1 ? "min-w-0 lg:order-1" : "min-w-0"}>{f.mockup}</div>
          </div>
        </section>
      ))}

      <section aria-labelledby="safety" className="border-b">
        <div className="mx-auto w-full max-w-6xl px-4 py-16 sm:px-6 sm:py-20">
          <SectionHeading id="safety" eyebrow="Safe by design" title="It asks before it touches anything">
            Groundcrew is built so that the safe path is the only path. These hold for every workspace.
          </SectionHeading>
          <ul className="mt-10 grid gap-px overflow-hidden rounded-xl border bg-border sm:grid-cols-2 lg:grid-cols-3">
            {SAFETY.map(({ icon: Icon, title, body }) => (
              <li key={title} className="bg-background p-6">
                <Icon aria-hidden="true" className="size-5 text-primary" />
                <h3 className="mt-3 text-[15px] font-semibold">{title}</h3>
                <p className="mt-2 text-[14px] leading-relaxed text-muted-foreground">{body}</p>
              </li>
            ))}
          </ul>
          <p className="mt-6 text-[14px]">
            <Link href="/security" className={`inline-flex items-center gap-1 rounded-sm font-medium text-primary hover:underline ${FOCUS}`}>
              Read the security model
              <ArrowRight aria-hidden="true" className="size-3.5" />
            </Link>
          </p>
        </div>
      </section>

      <section aria-labelledby="how" className="border-b bg-card">
        <div className="mx-auto w-full max-w-6xl px-4 py-16 sm:px-6 sm:py-20">
          <SectionHeading id="how" eyebrow="How it works" title="From sign-in to a running service" />
          <ol className="mt-10 grid gap-8 sm:grid-cols-2 lg:grid-cols-4">
            {STEPS.map((s, i) => (
              <li key={s.title}>
                <span
                  aria-hidden="true"
                  className="flex size-8 items-center justify-center rounded-full border bg-background text-[13px] font-semibold tabular"
                >
                  {i + 1}
                </span>
                <h3 className="mt-4 text-[15px] font-semibold">{s.title}</h3>
                <p className="mt-1.5 text-[14px] leading-relaxed text-muted-foreground">{s.body}</p>
              </li>
            ))}
          </ol>
        </div>
      </section>

      <section aria-labelledby="agents" className="border-b">
        <div className="mx-auto w-full max-w-6xl px-4 py-16 sm:px-6 sm:py-20">
          <SectionHeading id="agents" eyebrow="The crew" title="Four agents, each with one job">
            Each agent has its own tools and limits. They run on NVIDIA Nemotron models through Nebius Token Factory,
            and every step they take is shown live in the run.
          </SectionHeading>
          <ul className="mt-10 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {AGENTS.map((a) => (
              <li key={a.name} className="rounded-xl border bg-card p-5">
                <div className="flex items-center justify-between gap-2">
                  <h3 className="text-[15px] font-semibold">{a.name}</h3>
                  <span
                    className="tint inline-flex h-5 items-center rounded px-1.5 text-[11px] font-medium whitespace-nowrap"
                    style={{ "--tint": a.color } as CSSProperties}
                  >
                    {a.model}
                  </span>
                </div>
                <p className="mt-2 text-[14px] leading-relaxed text-muted-foreground">{a.body}</p>
              </li>
            ))}
          </ul>
        </div>
      </section>

      <section aria-labelledby="faq" className="border-b bg-card">
        <div className="mx-auto grid w-full max-w-6xl grid-cols-1 gap-10 px-4 py-16 sm:px-6 sm:py-20 lg:grid-cols-[1fr_1.6fr]">
          <SectionHeading id="faq" eyebrow="Questions" title="Common questions">
            More in the <Link href="/docs/faq" className={`rounded-sm text-primary hover:underline ${FOCUS}`}>FAQ</Link>.
          </SectionHeading>
          <div className="divide-y rounded-xl border bg-background">
            {FAQ.map((item) => (
              <details key={item.q} className="group">
                <summary
                  className={`flex min-h-11 cursor-pointer list-none items-center justify-between gap-4 px-5 py-4 text-[14.5px] font-medium [&::-webkit-details-marker]:hidden ${FOCUS} rounded-xl`}
                >
                  {item.q}
                  <span
                    aria-hidden="true"
                    className="text-[18px] leading-none text-muted-foreground transition-transform duration-200 group-open:rotate-45"
                  >
                    +
                  </span>
                </summary>
                <p className="px-5 pb-5 text-[14px] leading-relaxed text-muted-foreground">{item.a}</p>
              </details>
            ))}
          </div>
        </div>
      </section>

      <section aria-labelledby="start">
        <div className="mx-auto w-full max-w-6xl px-4 py-16 sm:px-6 sm:py-20">
          <div className="rounded-2xl border bg-card px-6 py-12 text-center sm:px-12">
            <h2 id="start" className="text-[26px] font-semibold tracking-tight text-balance sm:text-[30px]">
              Connect one account and see your map.
            </h2>
            <p className="mx-auto mt-3 max-w-xl text-[15.5px] leading-relaxed text-muted-foreground">
              Start read-only. Nothing changes until you approve it.
            </p>
            <div className="mt-8 flex flex-wrap justify-center gap-3">
              <Button asChild size="lg" className="h-11 px-5 text-[14.5px]">
                <Link href="/signin">
                  Get started
                  <ArrowRight aria-hidden="true" />
                </Link>
              </Button>
              <Button asChild size="lg" variant="outline" className="h-11 px-5 text-[14.5px]">
                <Link href="/docs">Read the docs</Link>
              </Button>
            </div>
          </div>
        </div>
      </section>
    </>
  );
}
