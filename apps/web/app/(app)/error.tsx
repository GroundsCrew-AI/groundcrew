"use client";

import { RotateCw } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { startTransition, useEffect } from "react";

import { PageBody, PageHeader } from "@/components/shell/page-header";
import { Button } from "@/components/ui/button";

/**
 * A page that crashed while rendering. The sidebar stays, so people can go elsewhere; "Try again"
 * refreshes the server data and renders the page again.
 */
export default function AppError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  const router = useRouter();

  useEffect(() => {
    console.error(error);
  }, [error]);

  function retry() {
    startTransition(() => {
      router.refresh();
      reset();
    });
  }

  return (
    <>
      <PageHeader title="Something went wrong" />
      <PageBody className="max-w-3xl">
        <div
          role="alert"
          className="space-y-3 rounded-lg border border-[color-mix(in_oklab,var(--status-failed)_35%,var(--border))] px-5 py-4"
        >
          <div className="space-y-1">
            <h2 className="text-[14px] font-medium">This page could not be shown</h2>
            <p className="max-w-[60ch] text-muted-foreground">
              Something went wrong while loading it. Nothing changed in your connected accounts. Try again,
              or go back to the overview.
            </p>
          </div>
          {(error.message || error.digest) && (
            <details className="text-[12px] text-muted-foreground">
              <summary className="cursor-pointer select-none hover:text-foreground">Details</summary>
              <p className="mt-1.5 font-mono break-words">
                {error.message}
                {error.digest && <span className="block">Reference {error.digest}</span>}
              </p>
            </details>
          )}
          <div className="flex gap-2 pt-1">
            <Button size="sm" onClick={retry}>
              <RotateCw aria-hidden="true" />
              Try again
            </Button>
            <Button size="sm" variant="outline" asChild>
              <Link href="/">Go to the overview</Link>
            </Button>
          </div>
        </div>
      </PageBody>
    </>
  );
}
