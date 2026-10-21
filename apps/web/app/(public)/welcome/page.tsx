import { ArrowRight, Boxes, Network, ShieldCheck, Siren } from "lucide-react";
import type { Metadata } from "next";
import Link from "next/link";

import { Button } from "@/components/ui/button";

export const metadata: Metadata = {
  title: "DevOps that asks first",
  description:
    "Groundcrew maps your AWS, Kubernetes, GitHub, and Vercel into one graph, deploys your apps, and fixes incidents. Every change waits for your approval.",
};

const WHAT = [
  {
    icon: Network,
    title: "One map of everything",
    body: "Connect AWS, Kubernetes, GitHub, and Vercel. Groundcrew reads your resources, repositories, and pipelines and draws one graph of what calls what, with the evidence for each link.",
  },
  {
    icon: Boxes,
    title: "Deploys from a repository",
    body: "Pick a repository and a target. Groundcrew writes the Dockerfile, builds and smoke-tests the image in an isolated sandbox, fixes the build when it fails, and shows you the exact plan before anything is pushed.",
  },
  {
    icon: Siren,
    title: "Incidents with a root cause",
    body: "It watches your services, opens an incident when signals go bad, and finds the cause from logs, metrics, and recent changes. Every claim links to the line it came from. Then it proposes a fix pull request or a rollback.",
  },
] as const;

const PROMISES = [
  { title: "Read-only by default", body: "The role you create can read configuration and logs. It cannot read secret values or your data." },
  { title: "You approve every change", body: "Deploys, rollbacks, and restarts run only after an owner or approver approves the exact plan." },
  { title: "Every fix is verified", body: "After a fix, Groundcrew checks the signals that opened the incident until the service is healthy." },
  { title: "Works with your pipeline", body: "Code fixes arrive as pull requests on your repository. Groundcrew does not merge or push to main." },
] as const;

const STEPS = [
  { title: "Connect", body: "Sign in with GitHub or email and connect a cloud account, a cluster, or a repository. Each connection says what Groundcrew can see." },
  { title: "Discover", body: "Groundcrew builds the resource graph and a short summary of your system, so you see what runs where." },
  { title: "Deploy and respond", body: "Start a deploy, or let the watcher open incidents. Approve the plans you agree with and watch each step live." },
] as const;

export default function WelcomePage() {
  return (
    <>
      <section aria-labelledby="hero" className="mx-auto w-full max-w-5xl px-4 pt-16 pb-14 sm:px-6 sm:pt-24">
        <p className="text-[13px] font-medium text-muted-foreground">An AI DevOps team for small teams</p>
        <h1 id="hero" className="mt-3 max-w-3xl text-[34px] leading-[1.1] font-semibold tracking-tight sm:text-[44px]">
          Deploys and incident fixes that wait for your approval.
        </h1>
        <p className="mt-5 max-w-2xl text-[16px] leading-relaxed text-muted-foreground">
          Groundcrew connects to the infrastructure you already run, maps it into one graph, deploys your apps, and
          finds and fixes what breaks. It reads freely and changes nothing until a person says yes.
        </p>
        <div className="mt-8 flex flex-wrap items-center gap-3">
          <Button asChild size="lg" className="h-10 px-4 text-[14px]">
            <Link href="/signin">
              Get started
              <ArrowRight aria-hidden="true" />
            </Link>
          </Button>
          <Button asChild size="lg" variant="outline" className="h-10 px-4 text-[14px]">
            <Link href="/security">How access works</Link>
          </Button>
        </div>
        <p className="mt-6 text-[12.5px] text-subtle">
          Agents run on NVIDIA Nemotron models through Nebius Token Factory.
        </p>
      </section>

      <section aria-labelledby="what" className="border-t bg-card">
        <div className="mx-auto w-full max-w-5xl px-4 py-14 sm:px-6">
          <h2 id="what" className="text-[20px] font-semibold tracking-tight">
            What Groundcrew does
          </h2>
          <ul className="mt-8 grid gap-6 md:grid-cols-3">
            {WHAT.map(({ icon: Icon, title, body }) => (
              <li key={title} className="rounded-xl border bg-background p-5">
                <Icon aria-hidden="true" className="size-5 text-primary" />
                <h3 className="mt-3 text-[15px] font-semibold">{title}</h3>
                <p className="mt-2 text-[13.5px] leading-relaxed text-muted-foreground">{body}</p>
              </li>
            ))}
          </ul>
        </div>
      </section>

      <section aria-labelledby="promise" className="border-t">
        <div className="mx-auto w-full max-w-5xl px-4 py-14 sm:px-6">
          <div className="flex items-center gap-2">
            <ShieldCheck aria-hidden="true" className="size-5 text-primary" />
            <h2 id="promise" className="text-[20px] font-semibold tracking-tight">
              What we promise
            </h2>
          </div>
          <dl className="mt-8 grid gap-x-10 gap-y-6 sm:grid-cols-2">
            {PROMISES.map((p) => (
              <div key={p.title}>
                <dt className="text-[14px] font-semibold">{p.title}</dt>
                <dd className="mt-1 text-[13.5px] leading-relaxed text-muted-foreground">{p.body}</dd>
              </div>
            ))}
          </dl>
        </div>
      </section>

      <section aria-labelledby="how" className="border-t bg-card">
        <div className="mx-auto w-full max-w-5xl px-4 py-14 sm:px-6">
          <h2 id="how" className="text-[20px] font-semibold tracking-tight">
            How it works
          </h2>
          <ol className="mt-8 grid gap-6 md:grid-cols-3">
            {STEPS.map((s, i) => (
              <li key={s.title} className="flex gap-3">
                <span
                  aria-hidden="true"
                  className="flex size-7 shrink-0 items-center justify-center rounded-full border bg-background text-[13px] font-semibold"
                >
                  {i + 1}
                </span>
                <div>
                  <h3 className="text-[15px] font-semibold">{s.title}</h3>
                  <p className="mt-1 text-[13.5px] leading-relaxed text-muted-foreground">{s.body}</p>
                </div>
              </li>
            ))}
          </ol>
          <div className="mt-10 flex flex-wrap gap-3">
            <Button asChild size="lg" className="h-10 px-4 text-[14px]">
              <Link href="/signin">Get started</Link>
            </Button>
            <Button asChild size="lg" variant="ghost" className="h-10 px-4 text-[14px]">
              <Link href="/docs">Read the docs</Link>
            </Button>
          </div>
        </div>
      </section>
    </>
  );
}
