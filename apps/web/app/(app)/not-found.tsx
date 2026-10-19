import type { Metadata } from "next";
import Link from "next/link";

import { PageBody, PageHeader } from "@/components/shell/page-header";
import { MapIllustration } from "@/components/states/illustrations";
import { EmptyState } from "@/components/states/states";
import { Button } from "@/components/ui/button";

export const metadata: Metadata = { title: "Page not found" };

const PLACES = [
  { href: "/incidents", label: "Incidents" },
  { href: "/runs", label: "Runs" },
  { href: "/map", label: "Map" },
  { href: "/connections", label: "Connections" },
];

/** A URL inside the app that leads nowhere. The sidebar stays, so nobody is stranded. */
export default function AppNotFound() {
  return (
    <>
      <PageHeader title="Page not found" />
      <PageBody className="max-w-3xl">
        <EmptyState
          illustration={<MapIllustration />}
          title="This page does not exist"
          action={
            <Button size="sm" asChild>
              <Link href="/">Go to the overview</Link>
            </Button>
          }
        >
          <p>The address may have been mistyped, or what it pointed to was removed.</p>
          <p className="mt-2">
            Or go to{" "}
            {PLACES.map((place, i) => (
              <span key={place.href}>
                {i > 0 && (i === PLACES.length - 1 ? ", or " : ", ")}
                <Link href={place.href} className="text-foreground underline-offset-2 hover:underline">
                  {place.label}
                </Link>
              </span>
            ))}
            .
          </p>
        </EmptyState>
      </PageBody>
    </>
  );
}
