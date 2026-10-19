import type { Metadata } from "next";
import type { ReactNode } from "react";

// The list page is a client component, so its title comes from this segment layout. A run's
// own page (runs/[id]) sets its own title.
export const metadata: Metadata = { title: "Runs" };

export default function RunsLayout({ children }: { children: ReactNode }) {
  return children;
}
