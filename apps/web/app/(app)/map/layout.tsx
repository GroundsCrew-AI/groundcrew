import type { Metadata } from "next";
import type { ReactNode } from "react";

// The page is a client component, so its title comes from this segment layout.
export const metadata: Metadata = { title: "Map" };

export default function MapLayout({ children }: { children: ReactNode }) {
  return children;
}
