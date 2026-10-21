import Link from "next/link";
import type { ReactNode } from "react";

import { Logo } from "@/components/logo";
import { REPOSITORY_URL } from "@/components/public/contact";

const NAV = [
  { href: "/security", label: "Security" },
  { href: "/docs", label: "Docs" },
] as const;

const FOOTER = [
  { href: "/terms", label: "Terms" },
  { href: "/privacy", label: "Privacy" },
  { href: "/security", label: "Security" },
  { href: "/docs", label: "Docs" },
] as const;

const FOCUS = "outline-none focus-visible:ring-3 focus-visible:ring-ring/50";

/** The frame of the public pages: wordmark, a few links, and Sign in; the policy links below. */
export function PublicShell({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-dvh flex-col">
      <header className="border-b">
        <div className="mx-auto flex h-14 w-full max-w-5xl items-center gap-3 px-4 sm:px-6">
          <Link href="/" aria-label="Groundcrew home" className={`rounded-md ${FOCUS}`}>
            <Logo />
          </Link>
          <nav aria-label="Main" className="ml-auto flex items-center gap-0.5 text-[13px]">
            {NAV.map((item) => (
              <Link
                key={item.href}
                href={item.href}
                className={`rounded-md px-2 py-1.5 text-muted-foreground hover:text-foreground sm:px-2.5 ${FOCUS}`}
              >
                {item.label}
              </Link>
            ))}
            <Link href="/signin" className={`ml-1 rounded-lg border px-3 py-1.5 font-medium hover:bg-muted ${FOCUS}`}>
              Sign in
            </Link>
          </nav>
        </div>
      </header>
      <main id="main" tabIndex={-1} className="flex-1 outline-none">
        {children}
      </main>
      <footer className="border-t">
        <div className="mx-auto flex w-full max-w-5xl flex-wrap items-center gap-x-5 gap-y-2 px-4 py-6 text-[12.5px] text-muted-foreground sm:px-6">
          <nav aria-label="Footer" className="flex flex-wrap gap-x-5 gap-y-2">
            {FOOTER.map((item) => (
              <Link key={item.label} href={item.href} className={`rounded-sm hover:text-foreground ${FOCUS}`}>
                {item.label}
              </Link>
            ))}
            <a href={REPOSITORY_URL} rel="noreferrer" className={`rounded-sm hover:text-foreground ${FOCUS}`}>
              GitHub
            </a>
          </nav>
          <p className="sm:ml-auto">© 2026 Groundcrew</p>
        </div>
      </footer>
    </div>
  );
}
