import type { ReactNode } from "react";

import { DocsNav } from "@/components/public/docs-nav";

/** Every docs page: the docs navigation beside (or, on phones, above) the page. */
export default function DocsLayout({ children }: { children: ReactNode }) {
  return (
    <div className="mx-auto w-full max-w-6xl px-4 py-8 sm:px-6 lg:grid lg:grid-cols-[220px_minmax(0,1fr)] lg:gap-12 lg:py-12">
      <DocsNav />
      <div className="mt-8 min-w-0 lg:mt-0">{children}</div>
    </div>
  );
}
