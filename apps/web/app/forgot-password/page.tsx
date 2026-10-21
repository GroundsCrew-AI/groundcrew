import type { Metadata } from "next";
import Link from "next/link";
import { redirect } from "next/navigation";

import { AuthShell } from "@/components/auth/auth-shell";
import { ForgotPasswordForm } from "@/components/auth/forms";
import { authOptions } from "@/lib/auth-api";

export const metadata: Metadata = { title: "Reset your password" };

export default async function ForgotPasswordPage({ searchParams }: { searchParams: Promise<{ email?: string }> }) {
  // Email and password is off on this server (PASSWORD_AUTH_ENABLED): only the sign-in page.
  if (!(await authOptions()).password) redirect("/signin");
  const { email } = await searchParams;
  return (
    <AuthShell
      title="Reset your password"
      description="We will email you a link to choose a new one. Accounts that use Google or GitHub can add a password this way too."
      footer={
        <Link href="/signin" className="font-medium text-foreground underline-offset-4 hover:underline">
          Back to sign in
        </Link>
      }
    >
      <ForgotPasswordForm defaultEmail={email} />
    </AuthShell>
  );
}
