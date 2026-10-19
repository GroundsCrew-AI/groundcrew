import type { Metadata } from "next";
import Link from "next/link";

import { AuthShell } from "@/components/auth/auth-shell";
import { FormMessage, VerifyEmailForm } from "@/components/auth/forms";

export const metadata: Metadata = { title: "Confirm your email" };

export default async function VerifyEmailPage({ searchParams }: { searchParams: Promise<{ token?: string }> }) {
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
