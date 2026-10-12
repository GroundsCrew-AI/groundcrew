import type { Metadata } from "next";
import { redirect } from "next/navigation";

import { signInAsTestUser, signInWithGitHub } from "@/app/actions";
import { auth } from "@/auth";
import { Logo } from "@/components/logo";
import { Button } from "@/components/ui/button";
import { testAuthEnabled } from "@/lib/test-auth";

import { TimelinePreview } from "./timeline-preview";

export const metadata: Metadata = { title: "Sign in" };

function GitHubIcon() {
  return (
    <svg viewBox="0 0 16 16" aria-hidden="true" className="size-4" fill="currentColor">
      <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0 0 16 8c0-4.42-3.58-8-8-8Z" />
    </svg>
  );
}

const ERRORS: Record<string, string> = {
  Configuration: "Sign-in is not configured on this server. Check AUTH_SECRET and the GitHub OAuth settings.",
  AccessDenied: "GitHub sign-in was cancelled.",
  Verification: "The sign-in link expired. Try again.",
};

export default async function SignInPage({
  searchParams,
}: {
  searchParams: Promise<{ callbackUrl?: string; error?: string }>;
}) {
  const session = await auth();
  if (session?.user?.githubId) redirect("/");
  const { callbackUrl, error } = await searchParams;
  let next = "/";
  if (callbackUrl) {
    try {
      const url = new URL(callbackUrl, "http://local");
      next = url.pathname + url.search;
    } catch {
      next = "/";
    }
  }

  return (
    <div className="grid min-h-dvh lg:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)]">
      <section className="flex flex-col px-6 py-6 sm:px-12">
        <Logo />
        <div className="my-auto max-w-sm py-16">
          <h1 className="text-[22px] leading-tight font-semibold tracking-tight">Sign in to Groundcrew</h1>
          <p className="mt-2 text-[14px] text-muted-foreground">
            Agents that map your infrastructure, deploy your apps, and fix incidents. Every change
            waits for your approval.
          </p>
          {error && (
            <p role="alert" className="mt-5 rounded-md border px-3 py-2 text-[13px] text-[var(--status-failed)]">
              {ERRORS[error] ?? "Sign-in failed. Try again."}
            </p>
          )}
          <form action={signInWithGitHub} className="mt-7">
            <input type="hidden" name="next" value={next} />
            <Button type="submit" size="lg" className="h-10 w-full gap-2 text-[14px]">
              <GitHubIcon />
              Continue with GitHub
            </Button>
          </form>
          {testAuthEnabled() && (
            <form action={signInAsTestUser} className="mt-3">
              <Button type="submit" variant="outline" size="lg" className="h-10 w-full text-[14px]">
                Sign in as the test user (dev)
              </Button>
            </form>
          )}
          <p className="mt-4 text-[12px] text-muted-foreground">
            We read your GitHub name, login, avatar, and email. Nothing else, and nothing is posted to
            your account.
          </p>
        </div>
        <p className="text-[12px] text-subtle">Built with NVIDIA Nemotron on Nebius Token Factory.</p>
      </section>
      <section aria-hidden="true" className="hidden border-l bg-card lg:flex lg:items-center lg:justify-center">
        <TimelinePreview />
      </section>
    </div>
  );
}
