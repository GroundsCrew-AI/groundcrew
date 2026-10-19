import type { Metadata } from "next";
import Link from "next/link";

import { AuthShell } from "@/components/auth/auth-shell";
import { FormMessage, ResetPasswordForm } from "@/components/auth/forms";
import { authOptions } from "@/lib/auth-api";

export const metadata: Metadata = { title: "Choose a new password" };

export default async function ResetPasswordPage({ searchParams }: { searchParams: Promise<{ token?: string }> }) {
  const { token } = await searchParams;
  const options = await authOptions();
  return (
    <AuthShell title="Choose a new password">
      {token ? (
        <ResetPasswordForm token={token} minLength={options.password_min_length} />
      ) : (
        <FormMessage>
          This link is missing its code.{" "}
          <Link href="/forgot-password" className="underline underline-offset-4">
            Send a new link
          </Link>
          .
        </FormMessage>
      )}
    </AuthShell>
  );
}
