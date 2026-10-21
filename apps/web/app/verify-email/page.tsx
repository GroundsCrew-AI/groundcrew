import type { Metadata } from "next";
import Link from "next/link";
import { redirect } from "next/navigation";

import { AuthShell } from "@/components/auth/auth-shell";
import { FormMessage, VerifyEmailForm } from "@/components/auth/forms";
import { authOptions } from "@/lib/auth-api";

export const metadata: Metadata = { title: "Confirm your email" };

export default async function VerifyEmailPage({ searchParams }: { searchParams: Promise<{ token?: string }> }) {
  // Email and password is off on this server (PASSWORD_AUTH_ENABLED): only the sign-in page.
  if (!(await authOptions()).password) redirect("/signin");
  const { token } = await searchParams;
  return (
    <AuthShell
      title="Confirm your email"
      description="Enter the password you chose when you signed up. That shows the account is yours, then you are signed in."
    >
      {token ? (
        <VerifyEmailForm token={token} />
      ) : (
        <FormMessage>
          This link is missing its code. Open the link from the email again, or{" "}
          <Link href="/signin" className="underline underline-offset-4">
            sign in
          </Link>{" "}
          to get a new one.
        </FormMessage>
      )}
    </AuthShell>
  );
}
