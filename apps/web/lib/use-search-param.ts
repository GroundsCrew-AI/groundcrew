"use client";

import { usePathname, useSearchParams } from "next/navigation";
import { useCallback } from "react";

/**
 * One query parameter as state. Setting it writes the URL with the History API, which the App
 * Router keeps in sync with useSearchParams (as the incident filters do): each change adds a
 * history entry, so Back undoes it and a link to the page carries it. `null` removes the
 * parameter; the others stay. Callers need a Suspense boundary above them (useSearchParams).
 */
export function useSearchParam(name: string): [string | null, (value: string | null) => void] {
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const value = searchParams.get(name);
  const set = useCallback(
    (next: string | null) => {
      const params = new URLSearchParams(window.location.search);
      if (params.get(name) === next) return;
      if (next === null) params.delete(name);
      else params.set(name, next);
      const search = params.toString();
      window.history.pushState(null, "", `${pathname}${search ? `?${search}` : ""}`);
    },
    [name, pathname],
  );
  return [value, set];
}
