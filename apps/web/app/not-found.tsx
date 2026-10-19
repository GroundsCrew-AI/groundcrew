import type { Metadata } from "next";
import Link from "next/link";

import { Logo } from "@/components/logo";
import { Button } from "@/components/ui/button";

export const metadata: Metadata = { title: "Page not found" };

/** Any URL nothing matches, outside the signed-in app (the app has its own, with navigation). */
export default function NotFound() {
  return (
    <div className="flex min-h-dvh flex-col px-6 py-6 sm:px-12">
      <Logo />
      <main className="my-auto max-w-sm py-16">
        <p className="tabular text-[12px] text-subtle">404</p>
        <h1 className="mt-1 text-[22px] leading-tight font-semibold tracking-tight">Page not found</h1>
        <p className="mt-2 text-[14px] text-muted-foreground">
          This address does not lead anywhere. It may have been mistyped, or the page moved.
        </p>
        <Button size="sm" className="mt-6" asChild>
          <Link href="/">Go to the overview</Link>
        </Button>
      </main>
    </div>
  );
}
