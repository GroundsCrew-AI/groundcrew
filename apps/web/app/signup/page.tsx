import type { Metadata } from "next";
import Link from "next/link";
import { redirect } from "next/navigation";

import { auth } from "@/auth";
import { AuthShell } from "@/components/auth/auth-shell";
import { FormMessage, SignUpForm } from "@/components/auth/forms";
import { OrDivider, ProviderButtons } from "@/components/auth/provider-buttons";
import { authOptions } from "@/lib/auth-api";
import { enabledOAuthProviders } from "@/lib/auth-providers";

export const metadata: Metadata = { title: "Create an account" };

export default async function SignUpPage({ searchParams }: { searchParams: Promise<{ email?: string }> }) {
  const session = await auth();
  if (session?.user?.id) redirect("/");
  const { email } = await searchParams;
  const providers = enabledOAuthProviders();
  const options = await authOptions();
  return (
    <AuthShell
      title="Create your Groundcrew account"
      description="Free to start. You get a workspace of your own; invite your team when you are ready."
      footer={
        <>
          Already have an account?{" "}
          <Link href="/signin" className="font-medium text-foreground underline-offset-4 hover:underline">
            Sign in
          </Link>
        </>
      }
    >
      <ProviderButtons next="/" providers={providers} />
      {options.password ? (
        <>
          {(providers.github || providers.google) && <OrDivider />}
          <SignUpForm defaultEmail={email} minLength={options.password_min_length} />
        </>
      ) : (
        !providers.github &&
        !providers.google && <FormMessage>Sign-up is not available on this server. Ask its administrator.</FormMessage>
      )}
    </AuthShell>
  );
}
