import type { Metadata } from "next";
import Link from "next/link";
import { redirect } from "next/navigation";

import { signInAsTestUser } from "@/app/actions";
import { auth } from "@/auth";
import { AuthShell } from "@/components/auth/auth-shell";
import { FormMessage, PasswordSignInForm } from "@/components/auth/forms";
import { OrDivider, ProviderButtons } from "@/components/auth/provider-buttons";
import { Button } from "@/components/ui/button";
import { authOptions } from "@/lib/auth-api";
import { safeNext, signInPageNotice } from "@/lib/auth-forms";
import { enabledOAuthProviders } from "@/lib/auth-providers";
import { testAuthEnabled } from "@/lib/test-auth";

export const metadata: Metadata = { title: "Sign in" };

/** Auth.js sends an absolute callbackUrl; keep only its path on this site. */
function callbackPath(callbackUrl: string | undefined): string {
  if (!callbackUrl) return "/";
  try {
    const url = new URL(callbackUrl, "http://local");
    return url.pathname + url.search;
  } catch {
    return "/";
  }
}

export default async function SignInPage({
  searchParams,
}: {
  searchParams: Promise<{ callbackUrl?: string; error?: string; code?: string; reason?: string; email?: string }>;
}) {
  const session = await auth();
  if (session?.user?.id) redirect("/");
  const params = await searchParams;
  const next = safeNext(callbackPath(params.callbackUrl));
  const notice = signInPageNotice(params);
  const providers = enabledOAuthProviders();
  const options = await authOptions();

  return (
    <AuthShell
      title="Sign in to Groundcrew"
      description="Agents that map your infrastructure, deploy your apps, and fix incidents. Every change waits for your approval."
      footer={
        options.password ? (
          <>
            New to Groundcrew?{" "}
            <Link href="/signup" className="font-medium text-foreground underline-offset-4 hover:underline">
              Create an account
            </Link>
          </>
        ) : undefined
      }
    >
      {notice && (
        <div className="mb-5">
          <FormMessage tone={notice.tone}>{notice.text}</FormMessage>
        </div>
      )}
      <ProviderButtons next={next} providers={providers} />
      {options.password && (
        <>
          {(providers.github || providers.google) && <OrDivider />}
          <PasswordSignInForm next={next} defaultEmail={params.email} />
        </>
      )}
      {!options.password && !providers.github && !providers.google && (
        <FormMessage>No way to sign in is configured on this server. Ask its administrator.</FormMessage>
      )}
      {testAuthEnabled() && (
        <form action={signInAsTestUser} className="mt-3">
          <Button type="submit" variant="outline" size="lg" className="h-10 w-full text-[14px]">
            Sign in as the test user (dev)
          </Button>
        </form>
      )}
    </AuthShell>
  );
}
