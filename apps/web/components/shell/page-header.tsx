"use client";

import { cn } from "cn";
import { BookOpen, Menu } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import type { CSSProperties, ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import { areaColor, areaFor } from "@/lib/areas";

import { useShell } from "./shell-context";
import { SidebarContent } from "./sidebar";

/** The top bar: the area's icon, the page title (with optional parent), a link to the area's
 * docs, and the page's own actions on the right. The area comes from the route (lib/areas). */
export function PageHeader({
  title,
  parent,
  actions,
  className,
  docs,
}: {
  title: ReactNode;
  parent?: ReactNode;
  actions?: ReactNode;
  className?: string;
  /** A more specific docs page than the area's own. */
  docs?: string;
}) {
  const { mobileOpen, setMobileOpen } = useShell();
  const area = areaFor(usePathname());
  const Icon = area?.icon;
  const help = docs ?? area?.docs;
  return (
    <header
      className={cn(
        "sticky top-0 z-20 flex h-12 shrink-0 items-center gap-2 border-b bg-background/90 px-4 backdrop-blur-sm md:px-6",
        className,
      )}
    >
      <Button
        variant="ghost"
        size="icon-sm"
        className="-ml-1.5 md:hidden"
        aria-label="Open navigation"
        onClick={() => setMobileOpen(true)}
      >
        <Menu />
      </Button>
      <Sheet open={mobileOpen} onOpenChange={setMobileOpen}>
        <SheetContent side="left" className="w-[260px] bg-sidebar p-0">
          <SheetTitle className="sr-only">Navigation</SheetTitle>
          <SidebarContent collapsed={false} onNavigate={() => setMobileOpen(false)} />
        </SheetContent>
      </Sheet>
      <div className="flex min-w-0 flex-1 items-center gap-2 text-[13px]">
        {area && Icon && (
          <span
            aria-hidden="true"
            title={area.hint}
            style={{ "--area": areaColor(area.key) } as CSSProperties}
            className="area-chip flex size-6 shrink-0 items-center justify-center rounded-md"
          >
            <Icon className="size-3.5" strokeWidth={2} />
          </span>
        )}
        {parent && (
          <>
            <span className="truncate text-muted-foreground">{parent}</span>
            <span aria-hidden="true" className="text-subtle">
              /
            </span>
          </>
        )}
        <h1 className="truncate font-medium">{title}</h1>
      </div>
      {(actions || help) && (
        <div className="flex shrink-0 items-center gap-2">
          {actions}
          {help && (
            <Button variant="ghost" size="sm" asChild className="text-muted-foreground">
              <Link href={help} target="_blank" rel="noreferrer" aria-label={`Docs: ${area?.label ?? "this page"} (opens in a new tab)`}>
                <BookOpen className="text-[var(--area-docs)]" />
                <span className="hidden sm:inline">Docs</span>
              </Link>
            </Button>
          )}
        </div>
      )}
    </header>
  );
}

/** Page body with consistent gutters. */
export function PageBody({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cn("px-4 py-6 md:px-6", className)}>{children}</div>;
}
