import { Menu } from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";

import { Logo } from "@/components/logo";
import { REPOSITORY_URL } from "@/components/public/contact";

const NAV = [
  { href: "/#deploy", label: "Product" },
  { href: "/security", label: "Security" },
  { href: "/docs", label: "Docs" },
] as const;

const FOOTER = [
  {
    title: "Product",
    links: [
      { href: "/#map", label: "Map" },
      { href: "/#deploy", label: "Deploys" },
      { href: "/#incidents", label: "Incidents" },
      { href: "/#fix", label: "Fixes" },
    ],
  },
  {
    title: "Docs",
    links: [
      { href: "/docs/getting-started", label: "Quick start" },
      { href: "/docs/connect/aws", label: "Connect AWS" },
      { href: "/docs/connect/github", label: "Connect GitHub" },
      { href: "/docs/faq", label: "FAQ" },
    ],
  },
  {
    title: "Trust",
    links: [
      { href: "/security", label: "Security" },
      { href: "/privacy", label: "Privacy" },
      { href: "/terms", label: "Terms" },
    ],
  },
] as const;

const FOCUS = "outline-none focus-visible:ring-3 focus-visible:ring-ring/50";
const LINK = `rounded-md text-muted-foreground transition-colors duration-150 hover:text-foreground ${FOCUS}`;

/** The frame of the public pages: a sticky header with the main links, and a footer of site links. */
export function PublicShell({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-dvh flex-col">
      <header className="sticky top-0 z-40 border-b bg-background/85 backdrop-blur supports-[backdrop-filter]:bg-background/70">
        <div className="mx-auto flex h-14 w-full max-w-6xl items-center gap-3 px-4 sm:px-6">
          <Link href="/" aria-label="Groundcrew home" className={`rounded-md ${FOCUS}`}>
            <Logo />
          </Link>
          <nav aria-label="Main" className="ml-6 hidden items-center gap-1 text-[13.5px] sm:flex">
            {NAV.map((item) => (
              <Link key={item.href} href={item.href} className={`px-2.5 py-1.5 ${LINK}`}>
                {item.label}
              </Link>
            ))}
          </nav>
          <div className="ml-auto flex items-center gap-2 text-[13.5px]">
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
            <details className="relative sm:hidden">
              <summary
                aria-label="Menu"
                className={`flex size-10 cursor-pointer list-none items-center justify-center rounded-lg border [&::-webkit-details-marker]:hidden ${FOCUS}`}
              >
                <Menu aria-hidden="true" className="size-4" />
              </summary>
              <nav
                aria-label="Main"
                className="absolute right-0 mt-2 flex w-48 flex-col rounded-lg border bg-popover p-1 text-[14px] shadow-md"
              >
                {[...NAV, { href: "/signin", label: "Sign in" }].map((item) => (
                  <Link key={item.label} href={item.href} className={`flex min-h-11 items-center px-3 ${LINK}`}>
                    {item.label}
                  </Link>
                ))}
              </nav>
            </details>
          </div>
        </div>
      </header>
      <main id="main" tabIndex={-1} className="flex-1 outline-none">
        {children}
      </main>
      <footer className="border-t">
        <div className="mx-auto grid w-full max-w-6xl gap-10 px-4 py-12 sm:px-6 md:grid-cols-[1.4fr_repeat(3,1fr)]">
          <div>
            <Logo />
            <p className="mt-3 max-w-xs text-[13px] leading-relaxed text-muted-foreground">
              Deploys and incident fixes that wait for your approval.
            </p>
          </div>
          {FOOTER.map((group) => (
            <nav key={group.title} aria-label={group.title}>
              <h2 className="text-[12.5px] font-semibold">{group.title}</h2>
              <ul className="mt-3 space-y-2 text-[13px]">
                {group.links.map((item) => (
                  <li key={item.href}>
                    <Link href={item.href} className={LINK}>
                      {item.label}
                    </Link>
                  </li>
                ))}
              </ul>
            </nav>
          ))}
        </div>
        <div className="border-t">
          <div className="mx-auto flex w-full max-w-6xl flex-wrap items-center gap-x-5 gap-y-2 px-4 py-5 text-[12.5px] text-muted-foreground sm:px-6">
            <p>© 2026 Groundcrew</p>
            <a href={REPOSITORY_URL} rel="noreferrer" className={`sm:ml-auto ${LINK}`}>
              Source on GitHub
            </a>
          </div>
        </div>
      </footer>
    </div>
  );
}
