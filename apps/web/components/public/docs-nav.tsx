"use client";

import { ChevronDown } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { DOCS_GROUPS } from "./docs-pages";

const FOCUS = "outline-none focus-visible:ring-3 focus-visible:ring-ring/50";

function Groups() {
  const pathname = usePathname();
  return (
    <div className="space-y-6">
      {DOCS_GROUPS.map((group) => (
        <div key={group.title}>
          <p className="px-3 text-[12px] font-medium tracking-wide text-subtle uppercase">{group.title}</p>
          <ul className="mt-1.5 space-y-0.5">
            {group.pages.map((page) => {
              const current = pathname === page.href;
              return (
                <li key={page.href}>
                  <Link
                    href={page.href}
                    aria-current={current ? "page" : undefined}
                    className={`flex min-h-11 items-center rounded-md px-3 text-[14px] transition-colors duration-150 lg:min-h-8 ${FOCUS} ${
                      current
                        ? "bg-muted font-medium text-foreground"
                        : "text-muted-foreground hover:bg-muted/60 hover:text-foreground"
                    }`}
                  >
                    {page.title}
                  </Link>
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </div>
  );
}

/** The docs' navigation: a sticky sidebar on wide screens, a collapsible menu above the page on
 * narrow ones (it closes again after a page is picked). */
export function DocsNav() {
  const pathname = usePathname();
  return (
    <>
      <details key={pathname} className="group rounded-lg border lg:hidden">
        <summary
          className={`flex min-h-11 cursor-pointer list-none items-center justify-between rounded-lg px-4 text-[14px] font-medium ${FOCUS}`}
        >
          Docs menu
          <ChevronDown aria-hidden="true" className="size-4 transition-transform duration-200 group-open:rotate-180" />
        </summary>
        <nav aria-label="Docs" className="border-t px-1 py-4">
          <Groups />
        </nav>
      </details>
      <nav aria-label="Docs" className="sticky top-20 hidden max-h-[calc(100dvh-6rem)] overflow-y-auto pb-8 lg:block">
        <Groups />
      </nav>
    </>
  );
}
