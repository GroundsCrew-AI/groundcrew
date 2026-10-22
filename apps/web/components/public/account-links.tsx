"use client";

import { ArrowRight } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";

const FOCUS = "outline-none focus-visible:ring-3 focus-visible:ring-ring/50";

/** Sign in and Get started, or Open Groundcrew for someone already signed in (reading the docs
 * from inside the app, for example). The public pages stay static; the session is asked for here. */
export function AccountLinks() {
  const [signedIn, setSignedIn] = useState(false);
  useEffect(() => {
    let cancelled = false;
    fetch("/api/auth/session")
      .then((r) => (r.ok ? r.json() : null))
      .then((session: { user?: unknown } | null) => {
        if (!cancelled) setSignedIn(Boolean(session?.user));
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);
  if (signedIn) {
    return (
      <Link
        href="/"
        className={`inline-flex h-9 items-center gap-1.5 rounded-lg bg-primary px-3.5 font-medium text-primary-foreground transition-opacity duration-150 hover:opacity-90 ${FOCUS}`}
      >
        Open Groundcrew
        <ArrowRight aria-hidden="true" className="size-4" />
      </Link>
    );
  }
  return (
    <>
      <Link
        href="/signin"
        className={`hidden rounded-md px-2.5 py-1.5 text-muted-foreground transition-colors duration-150 hover:text-foreground sm:inline-flex ${FOCUS}`}
      >
        Sign in
      </Link>
      <Link
        href="/signin"
        className={`inline-flex h-9 items-center rounded-lg bg-primary px-3.5 font-medium text-primary-foreground transition-opacity duration-150 hover:opacity-90 ${FOCUS}`}
      >
        Get started
      </Link>
    </>
  );
}
