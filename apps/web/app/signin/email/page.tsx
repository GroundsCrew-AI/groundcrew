import type { Metadata } from "next";
import Link from "next/link";

import { AuthShell } from "@/components/auth/auth-shell";
import { EmailLinkSignIn, FormMessage } from "@/components/auth/forms";

export const metadata: Metadata = { title: "Sign in by email" };

/** Where an emailed sign-in link lands. The link alone signs nobody in: the button does. */
export default async function EmailLinkPage({ searchParams }: { searchParams: Promise<{ token?: string }> }) {
  const { token } = await searchParams;
  return (
    <AuthShell title="Sign in to Groundcrew" description="You opened a sign-in link. Continue to sign in on this device.">
      {token ? (
        <EmailLinkSignIn token={token} />
      ) : (
        <FormMessage>
          This link is missing its code.{" "}
          <Link href="/signin" className="underline underline-offset-4">
            Ask for a new one
          </Link>
          .
        </FormMessage>
      )}
    </AuthShell>
  );
}
